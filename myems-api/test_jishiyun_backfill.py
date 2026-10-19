"""Backfill HTTP contracts; execution semantics remain in the shared queue tests."""

import unittest
from datetime import datetime, timezone
from unittest.mock import patch

import falcon
from core import jishiyun
import test_jishiyun as fixtures


KEY = "126f669b-50d0-493c-8262-004f276e2f59"


class BackfillHTTPTests(unittest.TestCase):
    authorize = staticmethod(fixtures.WorkbenchTests.authorize)

    def setUp(self):
        fixtures.WorkbenchTests.setUp(self)
        api = self.client.app
        api.add_route("/datasources/{id_}/acquisition/backfill", jishiyun.BackfillSubmit())
        api.add_route("/datasources/{id_}/acquisition/batches/{item_id}", jishiyun.BackfillDetail())
        api.add_route("/datasources/{id_}/acquisition/batches/{item_id}/{operation}", jishiyun.BackfillControl())
        self.base = "/datasources/1/acquisition/"
        self.queue_class = patch.object(jishiyun, "RunQueue").start()
        self.enqueue = patch.object(jishiyun, "enqueue_backfill", return_value=iter((8, 9))).start()
        self.status = patch.object(jishiyun, "batch_status", return_value={
            "batch_id": 1, "request_key": KEY, "complete": False, "unsubmitted_children": 1,
            "child_status_counts": {"partial": 1},
        }).start()
        self.body = {"task_id": 3, "request_key": KEY, "window_start_utc": "2026-10-01T08:00:00+08:00",
                     "window_end_utc": "2026-10-03T00:00:00Z"}
        self.connection.db.execute(
            "INSERT INTO tbl_jishiyun_backfill_batches "
            "(id,request_key,data_source_id,task_id,requested_by_user_id,window_start_utc,window_end_utc,"
            "child_count,is_cancelled) VALUES(1,?,1,3,42,'2026-10-01T00:00:00','2026-10-03T00:00:00',2,0)",
            (KEY,),
        )
        self.connection.db.commit()

    def test_submit_uses_authenticated_actor_normalized_window_and_shared_queue(self):
        result = self.client.simulate_post(self.base + "backfill", json=self.body)
        self.assertEqual(result.status_code, 202, result.text)
        self.assertFalse(result.json["complete"])
        args = self.enqueue.call_args.args
        self.assertEqual(args[1:3], (3, KEY))
        self.assertEqual(args[3], datetime(2026, 10, 1, tzinfo=timezone.utc))
        self.assertEqual(args[5], 42)
        self.assertEqual(self.queue_class.call_args.args[1], 1)
        self.connect.assert_not_called()  # No network/DB until the shared queue needs it.

    def test_bounded_list_and_scoped_detail_keep_incomplete_progress_visible(self):
        listing = self.client.simulate_get(self.base + "batches").json
        self.assertEqual(listing["items"][0]["id"], 1)
        self.assertNotIn("request_key", listing["items"][0])
        detail = self.client.simulate_get(self.base + "batches/1")
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.json["unsubmitted_children"], 1)
        self.assertEqual(detail.json["requested_by_user_id"], 42)
        self.assertEqual(self.client.simulate_get("/datasources/2/acquisition/batches/1").status_code, 404)
        self.assertEqual(self.client.simulate_get(self.base + "batches/99").status_code, 404)

    def test_resume_reuses_stored_identity_window_and_original_actor(self):
        result = self.client.simulate_post(self.base + "batches/1/resume", json={})
        self.assertEqual(result.status_code, 202, result.text)
        self.assertEqual(self.enqueue.call_args.args[1:3], (3, KEY))
        self.assertEqual(self.enqueue.call_args.args[5], 42)
        self.authorization.side_effect = lambda req: setattr(req.context, "user_id", 43)
        self.assertEqual(self.client.simulate_post(self.base + "batches/1/resume", json={}).status_code, 409)
        self.assertEqual(self.enqueue.call_count, 1)

    def test_cancel_delegates_fencing_with_authenticated_actor(self):
        result = self.client.simulate_post(self.base + "batches/1/cancel", json={})
        self.assertEqual(result.status_code, 200)
        self.status.assert_called_once_with(self.queue_class.return_value, KEY, cancel_by=42)
        self.enqueue.assert_not_called()

    def test_payload_and_time_validation_never_submits(self):
        for field, value in (("task_id", True), ("request_key", []),
                             ("request_key", "invalid-uuid"),
                             ("window_start_utc", None), ("window_end_utc", None),
                             ("window_start_utc", "2026-10-01T00:00:00"),
                             ("window_start_utc", "2026-10-03T00:00:00Z"),
                             ("window_end_utc", "2026-11-02T00:00:00Z"),
                             ("window_end_utc", "2026-10-03T00:01:00Z")):
            body = dict(self.body, **{field: value})
            self.assertEqual(self.client.simulate_post(self.base + "backfill", json=body).status_code, 400)
        self.assertEqual(self.client.simulate_post(self.base + "backfill", json=dict(self.body, actor=99)).status_code, 400)
        self.assertEqual(self.client.simulate_post(self.base + "batches/1/cancel", json={"actor": 99}).status_code, 400)
        self.assertEqual(self.client.simulate_post(self.base + "batches/1/delete", json={}).status_code, 400)
        self.enqueue.assert_not_called()

    def test_all_routes_require_administrator_before_any_read_or_submit(self):
        self.authorization.side_effect = falcon.HTTPForbidden(title="Administrator required")
        for path, method in (("backfill", "POST"), ("batches", "GET"), ("batches/1", "GET"),
                             ("batches/1/resume", "POST"), ("batches/1/cancel", "POST")):
            result = self.client.simulate_request(path=self.base + path, method=method, json={})
            self.assertEqual(result.status_code, 403)
        self.queue_class.assert_not_called()
        self.connect.assert_not_called()

    def test_partial_submission_failure_remains_error_not_success(self):
        def partial(*args):
            yield 8
            raise jishiyun.PersistenceError("Submission interrupted; same UUID resumes prefix")
        self.enqueue.side_effect = partial
        result = self.client.simulate_post(self.base + "backfill", json=self.body)
        self.assertEqual(result.status_code, 409)
        self.status.assert_not_called()
        self.enqueue.side_effect = jishiyun.mysql.connector.DatabaseError("sensitive driver content")
        result = self.client.simulate_post(self.base + "backfill", json=self.body)
        self.assertEqual(result.status_code, 500)
        self.assertNotIn("sensitive", result.text)
        self.enqueue.side_effect = jishiyun.ContractError("Empty configured scope after committing a parent")
        self.assertEqual(self.client.simulate_post(self.base + "backfill", json=self.body).status_code, 409)

    def test_parent_without_children_freezes_task_settings(self):
        self.connection.db.execute(
            "INSERT INTO tbl_jishiyun_tasks(id,data_source_id,name,is_enabled,task_type,schedule_minute,"
            "delay_minutes,lookback_hours,provider_utc_offset_minutes,parameters_json) "
            "VALUES(3,1,'backfill',0,'energy_backfill',40,40,3,480,'{\"encoding\":\"json\"}')",
        )
        self.connection.db.commit()
        detail = self.client.simulate_get(self.base + "tasks/3")
        self.assertTrue(detail.json["has_runs"])
        old = detail.json["configuration"]
        result = self.client.simulate_post(self.base + "tasks/3", json={
            "expected_configuration": old, "configuration": dict(old, lookback_hours=2),
        })
        self.assertEqual(result.status_code, 409)


if __name__ == "__main__":
    unittest.main()
