"""HTTP authorization and shared transaction delegation; no provider network."""

import unittest
from unittest.mock import patch

import falcon
from falcon import testing
from core import jishiyun


class InitializationHTTPTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(patch.stopall)
        self.auth = patch.object(jishiyun, "admin_control", side_effect=self.authorize).start()
        self.queue_class = patch.object(jishiyun, "RunQueue").start()
        self.service = patch.object(jishiyun, "ArchiveInitializer").start().return_value
        self.service.queue.enqueue_archive.return_value = 8
        self.service.preview_page.return_value = {"run_id": 8, "status": "preview", "items": []}
        self.connection = patch.object(jishiyun.mysql.connector, "connect").start()
        api = falcon.App()
        api.add_route("/datasources/{id_}", object())
        api.add_route("/datasources/{id_}/acquisition/{collection}", jishiyun.Workbench())
        api.add_route("/datasources/{id_}/acquisition/{collection}/{item_id}/control", jishiyun.Control())
        api.add_route("/datasources/{id_}/acquisition/runs/{run_id}/items", jishiyun.RunItems())
        api.add_route("/datasources/{id_}/acquisition/initialization", jishiyun.InitializationFetch())
        api.add_route("/datasources/{id_}/acquisition/initialization/{run_id}", jishiyun.InitializationPreview())
        api.add_route("/datasources/{id_}/acquisition/initialization/{run_id}/approve", jishiyun.InitializationApproval())
        api.add_route("/datasources/{id_}/acquisition/initialization/{run_id}/submit", jishiyun.InitializationSubmit())
        self.client = testing.TestClient(api)
        self.base = "/datasources/1/acquisition/initialization"
        self.config = {
            "name": "Approved", "energy_category_id": 1, "cost_center_id": 2, "is_counted": False,
            "hourly_low_limit": "0", "hourly_high_limit": "100", "units": "kWh", "point_high_limit": "999999",
        }

    @staticmethod
    def authorize(req):
        req.context.user_id = 42

    def test_fetch_uses_authenticated_actor_and_shared_queue_only(self):
        body = {"task_id": 3, "request_key": "126f669b-50d0-493c-8262-004f276e2f59"}
        result = self.client.simulate_post(self.base, json=body)
        self.assertEqual(result.status_code, 202, result.text)
        self.assertEqual(result.json, {"run_id": 8})
        self.service.queue.enqueue_archive.assert_called_once_with(3, body["request_key"], 42)
        self.assertEqual(self.queue_class.call_args.args[1], 1)
        self.connection.assert_not_called()
        self.assertEqual(self.client.simulate_post(self.base, json=dict(body, actor=99)).status_code, 400)

    def test_preview_pagination_delegates_and_never_accepts_posted_archives(self):
        result = self.client.simulate_get(self.base + "/8", params={"limit": "20", "before_id": "100"})
        self.assertEqual(result.status_code, 200)
        self.service.preview_page.assert_called_once_with(8, limit=20, before_id=100)
        self.assertEqual(self.client.simulate_post(self.base, json={"archives": []}).status_code, 400)

    def test_approval_parses_exact_configuration_and_server_actor(self):
        result = self.client.simulate_post(self.base + "/8/approve", json={
            "items": [{"address": "0001", "configuration": self.config}],
        })
        self.assertEqual(result.status_code, 200, result.text)
        run, items, actor = self.service.approve_many.call_args.args
        self.assertEqual((run, items[0][0], actor), (8, "0001", 42))
        self.assertFalse(items[0][1].is_counted)

    def test_invalid_later_configuration_never_calls_batch_approval(self):
        result = self.client.simulate_post(self.base + "/8/approve", json={"items": [
            {"address": "0001", "configuration": self.config},
            {"address": "0002", "configuration": dict(self.config, hourly_high_limit="-1")},
        ]})
        self.assertEqual(result.status_code, 400)
        self.service.approve_many.assert_not_called()

    def test_submit_has_fixed_selection_and_authenticated_actor(self):
        result = self.client.simulate_post(self.base + "/8/submit", json={"addresses": ["0001", "0002"]})
        self.assertEqual(result.status_code, 202)
        self.service.submit_approved.assert_called_once_with(8, ("0001", "0002"), 42)

    def test_payload_bounds_and_types(self):
        for path, body in (
            ("", {"task_id": True, "request_key": "x"}),
            ("", {"task_id": 1, "request_key": []}),
            ("/8/submit", {"addresses": []}),
            ("/8/submit", {"addresses": ["0001"] * 201}),
            ("/8/submit", {"addresses": [{}]}),
            ("/8/approve", {"items": [None]}),
            ("/8/approve", {"items": []}),
        ):
            with self.subTest(path=path, body=body):
                self.assertEqual(self.client.simulate_post(self.base + path, json=body).status_code, 400)
        result = self.client.simulate_post(self.base, json={"padding": "x" * 262144})
        self.assertEqual(result.status_code, 413)

    def test_all_routes_enforce_administrator_before_service_access(self):
        self.auth.side_effect = falcon.HTTPForbidden(title="Administrator required")
        for path, method in (("", "POST"), ("/8", "GET"), ("/8/approve", "POST"), ("/8/submit", "POST")):
            result = self.client.simulate_request(method=method, path=self.base + path, json={})
            self.assertEqual(result.status_code, 403)
        self.queue_class.assert_not_called()

    def test_conflicts_and_database_failures_are_explicit_and_sanitized(self):
        for error, expected in (
            (jishiyun.PersistenceError("Wrong source or state"), 409),
            (jishiyun.mysql.connector.DatabaseError("sensitive synthetic driver details"), 500),
        ):
            self.service.submit_approved.side_effect = error
            result = self.client.simulate_post(self.base + "/8/submit", json={"addresses": ["0001"]})
            self.assertEqual(result.status_code, expected)
            self.assertNotIn("sensitive", result.text)


if __name__ == "__main__":
    unittest.main()
