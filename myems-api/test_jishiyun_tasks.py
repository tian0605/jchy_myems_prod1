"""Task configuration transactions against a schema-derived SQLite behavior adapter."""

import json
import unittest
from uuid import uuid4

import falcon
from core import jishiyun
import test_jishiyun as workbench_tests


class TaskTests(unittest.TestCase):
    def setUp(self):
        workbench_tests.WorkbenchTests.setUp(self)
        self.base = "/datasources/1/acquisition/tasks"
        self.configuration = {
            "name": "Synthetic new task", "task_type": "energy_collect", "schedule_minute": 40,
            "delay_minutes": 40, "lookback_hours": 3, "provider_utc_offset_minutes": 480, "encoding": "json",
        }
        self.key = str(uuid4())

    authorize = staticmethod(workbench_tests.WorkbenchTests.authorize)

    def create(self, configuration=None, key=None):
        return self.client.simulate_post(self.base, json={
            "request_key": key or self.key, "configuration": configuration or self.configuration,
        })

    def edit(self, task_id, configuration, expected=None, source=1):
        return self.client.simulate_post(f"/datasources/{source}/acquisition/tasks/{task_id}", json={
            "configuration": configuration, "expected_configuration": expected or self.configuration,
        })

    def test_create_paused_with_authenticated_actor_and_exact_configuration(self):
        result = self.create()
        self.assertEqual(result.status_code, 201, result.text)
        task_id = result.json["id"]
        row = self.connection.db.execute(
            "SELECT is_enabled,created_by_user_id,updated_by_user_id,parameters_json "
            "FROM tbl_jishiyun_tasks WHERE id=?", (task_id,),
        ).fetchone()
        self.assertEqual(row, (0, 42, 42, '{"encoding": "json"}'))
        snapshot = self.client.simulate_get(self.base + "/" + str(task_id))
        self.assertEqual(snapshot.status_code, 200)
        self.assertEqual(snapshot.json["configuration"], self.configuration)
        self.assertFalse(snapshot.json["has_runs"])

    def test_request_replay_idempotent_and_changed_or_other_actor_rejected(self):
        first = self.create().json["id"]
        self.assertEqual(self.create().json["id"], first)
        self.assertEqual(self.create(dict(self.configuration, name="Changed")).status_code, 409)
        self.authorization.side_effect = lambda req: setattr(req.context, "user_id", 43)
        self.assertEqual(self.create().status_code, 409)
        self.assertEqual(self.connection.db.execute(
            "SELECT COUNT(*) FROM tbl_jishiyun_events WHERE event_type='admin_task_created'",
        ).fetchone()[0], 1)

    def test_all_supported_types_and_invalid_scalar_settings(self):
        for kind in ("meter_initialize", "energy_collect", "energy_backfill"):
            self.assertEqual(self.create(dict(self.configuration, task_type=kind), str(uuid4())).status_code, 201)
        for field, values in {
            "name": ["", " padded ", "x" * 256, None],
            "task_type": ["archive_sync", "recalculate", {}, None],
            "encoding": ["xml", {}, None],
            "schedule_minute": [-1, 60, True, "40"],
            "delay_minutes": [-1, 65536, True],
            "lookback_hours": [0, 25, 1.5],
            "provider_utc_offset_minutes": [1, 841, -841, False],
        }.items():
            for value in values:
                with self.subTest(field=field, value=value):
                    self.assertEqual(self.create(dict(self.configuration, **{field: value})).status_code, 400)
        self.assertEqual(self.client.simulate_post(self.base, json={
            "request_key": self.key, "configuration": dict(self.configuration, api_key="not-accepted"),
        }).status_code, 400)
        self.assertEqual(self.client.simulate_post(self.base, json={
            "request_key": "bad", "configuration": self.configuration,
        }).status_code, 400)

    def test_edit_paused_unused_task_and_audit_idempotent_replay(self):
        task_id = self.create().json["id"]
        changed = dict(self.configuration, lookback_hours=2, encoding="form")
        self.assertEqual(self.edit(task_id, changed).json["changed"], True)
        self.assertEqual(self.edit(task_id, changed).json["changed"], False)
        event = self.connection.db.execute(
            "SELECT actor_user_id,details_json FROM tbl_jishiyun_events WHERE event_type='admin_task_updated'",
        ).fetchall()
        self.assertEqual(len(event), 1)
        self.assertEqual(event[0][0], 42)
        self.assertEqual(json.loads(event[0][1])["before"], self.configuration)
        self.assertEqual(json.loads(event[0][1])["after"], changed)

    def test_edit_stale_enabled_type_change_and_cross_source_are_rejected(self):
        task_id = self.create().json["id"]
        changed = dict(self.configuration, name="Changed")
        self.assertEqual(self.edit(task_id, changed, expected=dict(self.configuration, name="Stale")).status_code, 409)
        self.assertEqual(self.edit(task_id, dict(self.configuration, task_type="meter_initialize")).status_code, 409)
        self.assertEqual(self.edit(task_id, changed, source=2).status_code, 404)
        self.connection.db.execute("UPDATE tbl_jishiyun_tasks SET is_enabled=1 WHERE id=?", (task_id,))
        self.connection.db.commit()
        self.assertEqual(self.edit(task_id, changed).status_code, 409)

    def test_used_execution_settings_and_open_runs_protected(self):
        task_id = self.create().json["id"]
        self.connection.db.execute(
            "INSERT INTO tbl_jishiyun_runs(id,data_source_id,task_id,status) VALUES (99,1,?,'queued')", (task_id,),
        )
        self.connection.db.commit()
        changed = dict(self.configuration, name="Changed")
        self.assertEqual(self.edit(task_id, changed).status_code, 409)
        self.connection.db.execute("UPDATE tbl_jishiyun_runs SET status='done' WHERE id=99")
        self.connection.db.commit()
        self.assertEqual(self.edit(task_id, dict(self.configuration, delay_minutes=50)).status_code, 409)
        self.assertEqual(self.edit(task_id, changed).status_code, 200)
        self.assertEqual(self.client.simulate_get(self.base + "/" + str(task_id)).json["has_runs"], True)

    def test_audit_failure_rolls_back_create_and_edit(self):
        self.connection.fail_on = "INSERT INTO tbl_jishiyun_events"
        self.assertEqual(self.create().status_code, 500)
        self.assertEqual(self.connection.db.execute("SELECT COUNT(*) FROM tbl_jishiyun_tasks").fetchone()[0], 2)
        self.connection.fail_on = None
        task_id = self.create().json["id"]
        self.connection.fail_on = "INSERT INTO tbl_jishiyun_events"
        self.assertEqual(self.edit(task_id, dict(self.configuration, name="Changed")).status_code, 500)
        self.assertEqual(self.connection.db.execute(
            "SELECT name FROM tbl_jishiyun_tasks WHERE id=?", (task_id,),
        ).fetchone()[0], self.configuration["name"])

    def test_permissions_sources_and_safe_snapshot(self):
        self.assertEqual(self.client.simulate_post("/datasources/999/acquisition/tasks", json={
            "request_key": self.key, "configuration": self.configuration,
        }).status_code, 404)
        task_id = self.create().json["id"]
        self.connection.db.execute("UPDATE tbl_jishiyun_tasks SET parameters_json=? WHERE id=?",
                                   ('{"encoding":"json","secret":"must-not-leak"}', task_id))
        self.connection.db.commit()
        result = self.client.simulate_get(self.base + "/" + str(task_id))
        self.assertEqual(result.status_code, 409)
        self.assertNotIn("must-not-leak", result.text)
        self.authorization.side_effect = falcon.HTTPForbidden(title="Administrator required")
        self.assertEqual(self.create().status_code, 403)
        self.assertEqual(self.client.simulate_get(self.base + "/" + str(task_id)).status_code, 403)
        self.assertEqual(self.edit(task_id, self.configuration).status_code, 403)


if __name__ == "__main__":
    unittest.main()
