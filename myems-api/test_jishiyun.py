"""HTTP/transaction behavior with synthetic data; not a MySQL locking test."""

from pathlib import Path
import re
import sqlite3
import unittest
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch
from decimal import Decimal

import falcon
from falcon import testing
from core import jishiyun


class Cursor:
    @property
    def lastrowid(self):
        return self.cursor.lastrowid

    def __init__(self, connection):
        self.connection = connection
        self.cursor = connection.db.cursor()

    def execute(self, sql, params=()):
        self.connection.statements.append(sql)
        if self.connection.fail_on and self.connection.fail_on in sql:
            raise jishiyun.mysql.connector.DatabaseError("synthetic sensitive driver details")
        if "information_schema.TABLES" in sql:
            self.cursor.execute("SELECT name, 'InnoDB' FROM sqlite_master WHERE name IN (" +
                                ",".join("?" for _ in params) + ")", params)
        else:
            self.cursor.execute(sql.replace("%s", "?").replace(" FOR UPDATE", "").replace(
                "UTC_TIMESTAMP()", "'2026-10-15 00:00:00'"), params)

    def fetchone(self):
        return self.cursor.fetchone()

    def fetchall(self):
        return self.cursor.fetchall()

    def close(self):
        self.cursor.close()


class Connection:
    def __init__(self):
        self.db = sqlite3.connect(":memory:")
        self.db.create_function("JSON_UNQUOTE", 1, lambda value: value)
        self.statements = []
        self.fail_on = None
        self.rollbacks = 0
        self.closed = False
        self.db.execute("CREATE TABLE tbl_data_sources (id INTEGER PRIMARY KEY)")
        ddl = (Path(__file__).resolve().parent.parent / "database" / "install" / "myems_jishiyun.sql").read_text("utf-8")
        # Derive column names from the actual migration so misspelled API fields fail.
        for table, body in re.findall(r"CREATE TABLE IF NOT EXISTS `myems_system_db`\.`(\w+)` \((.*?)\) ENGINE", ddl, re.S):
            names = re.findall(r"^  `(\w+)` ", body, re.M)
            self.db.execute("CREATE TABLE " + table + " (" + ",".join(
                name + (" INTEGER PRIMARY KEY" if name == "id" else "") for name in names) + ")")
        self.db.executescript("""
            INSERT INTO tbl_data_sources VALUES (1), (2);
            INSERT INTO tbl_jishiyun_tasks (id,data_source_id,name,is_enabled,task_type)
                VALUES (1,1,'synthetic task',1,'energy_collect'), (2,2,'other source',1,'energy_collect');
            INSERT INTO tbl_jishiyun_runs (id,data_source_id,status,lease_token,lease_expires_datetime_utc,
                missing_count,expected_count,valid_count,parameters_json)
                VALUES (1,1,'running','old-token','2099-01-01',16,132,116,'{"secret":"synthetic"}'),
                       (2,2,'queued',NULL,NULL,0,0,0,'{}'), (3,1,'done',NULL,NULL,0,0,0,'{}');
            INSERT INTO tbl_jishiyun_alerts (id,data_source_id,status,external_address,alert_type,
                reading_datetime_utc,acknowledged_by_user_id)
                VALUES (1,1,'active','0001','missing','2026-10-01 00:15:00',NULL),
                       (2,1,'recovered','0002','missing','2026-10-01 00:15:00',NULL),
                       (3,2,'active','0003','missing','2026-10-01 00:15:00',NULL);
            INSERT INTO tbl_jishiyun_run_items (id,run_id,external_address,external_id,status)
                VALUES (1,1,'0001',1,'created'), (2,2,'0002',2,'preview');
        """)
        self.db.commit()

    def start_transaction(self):
        self.db.execute("BEGIN")

    def cursor(self):
        return Cursor(self)

    def commit(self):
        self.db.commit()

    def rollback(self):
        self.rollbacks += 1
        self.db.rollback()

    def close(self):
        self.closed = True


class WorkbenchTests(unittest.TestCase):
    def setUp(self):
        self.connection = Connection()
        self.addCleanup(self.connection.db.close)
        self.connect = patch.object(jishiyun.mysql.connector, "connect", return_value=self.connection).start()
        self.addCleanup(patch.stopall)
        self.authorization = patch.object(jishiyun, "admin_control", side_effect=self.authorize).start()
        api = falcon.App()
        api.add_route("/datasources/{id_}", object())
        api.add_route("/datasources/{id_}/acquisition/{collection}", jishiyun.Workbench())
        api.add_route("/datasources/{id_}/acquisition/runs/{run_id}/items", jishiyun.RunItems())
        api.add_route("/datasources/{id_}/acquisition/{collection}/{item_id}/control", jishiyun.Control())
        api.add_route("/datasources/{id_}/acquisition/tasks/{item_id}", jishiyun.TaskConfiguration())
        self.client = testing.TestClient(api)

    @staticmethod
    def authorize(req):
        req.context.user_id = 42

    def get(self, collection, source=1, params=None):
        return self.client.simulate_get(f"/datasources/{source}/acquisition/{collection}", params=params)

    def control(self, collection, action, item=1, source=1, extra=None):
        return self.client.simulate_post(
            f"/datasources/{source}/acquisition/{collection}/{item}/control",
            json=dict({"action": action}, **(extra or {})),
        )

    def test_all_collections_use_real_ddl_column_names_and_exclude_payloads(self):
        for collection in jishiyun.COLLECTIONS:
            with self.subTest(collection=collection):
                result = self.get(collection)
                self.assertEqual(result.status_code, 200, result.text)
                self.assertNotIn("parameters_json", result.text)
                self.assertNotIn("details_json", result.text)
                self.assertNotIn("synthetic", result.text if collection != "tasks" else "")
        self.assertTrue(self.connection.closed)

    def test_source_isolation_and_exact_gap_counts(self):
        result = self.get("runs")
        self.assertEqual([row["id"] for row in result.json["items"]], [3, 1])
        row = result.json["items"][1]
        self.assertEqual((row["expected_count"], row["valid_count"], row["missing_count"]), (132, 116, 16))

    def test_execution_timestamps_and_task_latest_failure_do_not_hide_last_success(self):
        db = self.connection.db
        db.executescript("""
            UPDATE tbl_jishiyun_runs SET task_id=1,started_datetime_utc='2026-10-17 10:40:01',
                finished_datetime_utc='2026-10-17 10:40:03' WHERE id=3;
            INSERT INTO tbl_jishiyun_runs
                (id,data_source_id,task_id,status,started_datetime_utc,finished_datetime_utc)
                VALUES (4,1,1,'error','2026-10-17 11:40:01','2026-10-17 11:40:02'),
                       (5,2,1,'done','2099-01-01 00:00:00','2099-01-01 00:00:01'),
                       (6,1,1,'partial','2026-10-17 12:40:01','2026-10-17 12:40:02');
        """)
        db.commit()
        task = self.get("tasks").json["items"][0]
        self.assertEqual(task["last_success_finished_datetime_utc"], "2026-10-17 10:40:03")
        self.assertEqual((task["latest_run_id"], task["latest_run_status"]), (6, "partial"))
        self.assertEqual(task["latest_run_finished_datetime_utc"], "2026-10-17 12:40:02")
        self.assertIsNone(self.get("tasks", source=2).json["items"][0]["last_success_finished_datetime_utc"])
        rows = self.get("runs").json["items"]
        failed = next(row for row in rows if row["id"] == 4)
        self.assertEqual(failed["started_datetime_utc"], "2026-10-17 11:40:01")
        self.assertEqual(failed["finished_datetime_utc"], "2026-10-17 11:40:02")
        db.execute("INSERT INTO tbl_jishiyun_runs (id,data_source_id,task_id,status) VALUES (7,1,1,'queued')")
        db.commit()
        task = self.get("tasks").json["items"][0]
        self.assertEqual(task["latest_run_status"], "queued")
        self.assertIsNone(task["latest_run_finished_datetime_utc"])
        self.assertEqual(task["last_success_finished_datetime_utc"], "2026-10-17 10:40:03")
        db.execute("INSERT INTO tbl_jishiyun_tasks (id,data_source_id,name) VALUES (8,1,'never run')")
        db.commit()
        empty = self.get("tasks", params={"limit": 1}).json["items"][0]
        self.assertIsNone(empty["latest_run_id"])
        self.assertIsNone(empty["last_success_finished_datetime_utc"])

    def test_business_jobs_are_source_scoped_filtered_and_exclude_lease(self):
        self.connection.db.executescript("""
            INSERT INTO tbl_jishiyun_business_jobs
                (id,data_source_id,run_id,status,gate_code,lease_token,actor_user_id)
                VALUES (1,1,3,'blocked','five_quarter_hour_readings_including_end_boundary_required','hidden',42),
                       (2,2,2,'done',NULL,'other-hidden',43);
        """)
        self.connection.db.commit()
        result = self.get("business", params={"status": "blocked"})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual([r["id"] for r in result.json["items"]], [1])
        self.assertIn("five_quarter_hour", result.json["items"][0]["gate_code"])
        self.assertNotIn("lease_token", result.text)
        self.assertNotIn("hidden", result.text)
        self.assertEqual(self.get("business", params={"status": "cancelled"}).status_code, 400)

    def test_business_retry_is_audited_idempotent_and_does_not_bypass_gates(self):
        self.connection.db.execute(
            "INSERT INTO tbl_jishiyun_business_jobs (id,data_source_id,run_id,status,gate_code) "
            "VALUES (1,1,3,'blocked','boundary_required')",
        )
        self.connection.db.commit()
        for _ in range(2):
            response = self.control("business", "retry_business")
            self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.connection.db.execute(
            "SELECT status,gate_code FROM tbl_jishiyun_business_jobs",
        ).fetchone(), ("queued", "boundary_required"))
        self.assertEqual(self.connection.db.execute(
            "SELECT COUNT(*) FROM tbl_jishiyun_events WHERE event_type='admin_business_retry'",
        ).fetchone()[0], 1)
        self.assertEqual(self.control("business", "retry_business", source=2).status_code, 404)
        for status in ("running", "done"):
            self.connection.db.execute("UPDATE tbl_jishiyun_business_jobs SET status=?", (status,))
            self.connection.db.commit()
            self.assertEqual(self.control("business", "retry_business").status_code, 409)

    def test_energy_detail_is_current_window_raw_values_not_archive_items(self):
        db = self.connection.db
        db.execute("ATTACH DATABASE ':memory:' AS myems_historical_db")
        db.execute("""CREATE TABLE myems_historical_db.tbl_jishiyun_energy_readings
            (id INTEGER PRIMARY KEY,data_source_id INTEGER,external_address TEXT,external_id INTEGER,
             reading_datetime_utc TEXT,forward_active_register TEXT,quality TEXT,revision INTEGER,
             first_run_id INTEGER,last_run_id INTEGER)""")
        db.execute("""UPDATE tbl_jishiyun_runs SET window_start_utc='2026-10-16 02:00:00',
            window_end_utc='2026-10-16 03:00:00' WHERE id=1""")
        for id_, source, timestamp in (
            (1, 1, "2026-10-16 02:00:00"), (2, 1, "2026-10-16 02:15:00"),
            (3, 2, "2026-10-16 02:00:00"), (4, 1, "2026-10-16 03:00:00"),
        ):
            db.execute("INSERT INTO myems_historical_db.tbl_jishiyun_energy_readings VALUES(?,?,"
                       "'0001',7,?,'123.456789','valid',1,1,1)", (id_, source, timestamp))
        db.commit()
        path = "/datasources/1/acquisition/runs/1/items"
        first = self.client.simulate_get(path, params={"limit": 1})
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(first.json["detail_kind"], "energy_current_window")
        self.assertEqual(first.json["items"][0]["id"], 2)
        self.assertEqual(first.json["items"][0]["forward_active_register"], "123.456789")
        self.assertNotIn("meter_id", first.json["items"][0])
        self.assertTrue(first.json["has_more"])
        second = self.client.simulate_get(path, params={"before_id": 2}).json
        self.assertEqual([item["id"] for item in second["items"]], [1])
        self.assertFalse(second["has_more"])
        self.assertEqual(self.client.simulate_get(
            "/datasources/2/acquisition/runs/1/items").status_code, 404)

    def test_readings_span_runs_and_windows_include_names_preserve_unmapped_and_isolate_source(self):
        db = self.connection.db
        db.executescript("""
            ATTACH DATABASE ':memory:' AS myems_historical_db;
            CREATE TABLE myems_historical_db.tbl_jishiyun_energy_readings
                (id INTEGER PRIMARY KEY,data_source_id INTEGER,external_address TEXT,external_id INTEGER,
                 reading_datetime_utc TEXT,forward_active_register TEXT,quality TEXT,revision INTEGER,
                 first_run_id INTEGER,last_run_id INTEGER);
            CREATE TABLE tbl_meters (id INTEGER PRIMARY KEY,name TEXT);
            INSERT INTO tbl_meters VALUES (7,'Pilot meter');
            INSERT INTO tbl_jishiyun_meter_mappings
                (id,data_source_id,external_address,external_id,meter_id,is_enabled)
                VALUES (1,1,'0001',11,7,0),(2,2,'0001',11,7,1);
            INSERT INTO myems_historical_db.tbl_jishiyun_energy_readings VALUES
                (1,1,'0001',11,'2026-10-01 00:00:00','1.123456','valid',1,10,30),
                (2,1,'unmapped',12,'2026-10-17 00:00:00',NULL,'invalid',2,20,31),
                (3,2,'0001',11,'2026-10-17 00:00:00','2','valid',1,40,40);
        """)
        db.commit()
        status = {"energy_status": "pending", "settlement_status": "pending",
                  "dimensions_status": "pending", "processing_details": []}
        with patch.object(jishiyun, "reading_processing", side_effect=lambda cursor, source, rows: [status] * len(rows)) as inspect:
            first = self.get("readings", params={"limit": 1})
            self.assertEqual(first.status_code, 200, first.text)
            self.assertTrue(first.json["has_more"])
            self.assertEqual(first.json["items"][0]["id"], 2)
            self.assertIsNone(first.json["items"][0]["meter_name"])
            self.assertEqual(first.json["items"][0]["quality"], "invalid")
            self.assertEqual(first.json["items"][0]["energy_status"], "pending")
            self.assertEqual(inspect.call_args.args[1], 1)
            second = self.get("readings", params={"before_id": 2})
            self.assertEqual(second.status_code, 200, second.text)
            row = second.json["items"][0]
            self.assertEqual(row["meter_name"], "Pilot meter")
            self.assertEqual((row["first_run_id"], row["last_run_id"]), (10, 30))
            self.assertEqual(row["forward_active_register"], "1.123456")
            self.assertFalse(second.json["has_more"])
            other = self.get("readings", source=2)
            self.assertEqual([item["id"] for item in other.json["items"]], [3])
            self.assertEqual(self.get("readings", params={"limit": 201}).status_code, 400)
            self.assertEqual(self.get("readings", params={"before_id": "invalid"}).status_code, 400)
            self.assertEqual(self.get("readings", params={"status": "done"}).status_code, 400)
        self.authorization.side_effect = falcon.HTTPForbidden()
        self.assertEqual(self.get("readings").status_code, 403)

    def test_readings_audit_verification_error_is_explicit_not_success_shaped(self):
        db = self.connection.db
        db.executescript("""
            ATTACH DATABASE ':memory:' AS myems_historical_db;
            CREATE TABLE myems_historical_db.tbl_jishiyun_energy_readings
                (id INTEGER PRIMARY KEY,data_source_id INTEGER,external_address TEXT,external_id INTEGER,
                 reading_datetime_utc TEXT,forward_active_register TEXT,quality TEXT,revision INTEGER,
                 first_run_id INTEGER,last_run_id INTEGER);
            CREATE TABLE tbl_meters (id INTEGER PRIMARY KEY,name TEXT);
        """)
        db.commit()
        with patch.object(jishiyun, "reading_processing", side_effect=jishiyun.PersistenceError("synthetic hidden")):
            with self.assertLogs(jishiyun.logger, level="ERROR"):
                response = self.get("readings")
        self.assertEqual(response.status_code, 409)
        self.assertNotIn("synthetic hidden", response.text)
        self.assertNotIn("items", response.json)

    def test_keyset_pagination(self):
        first = self.get("runs", params={"limit": 1}).json
        self.assertTrue(first["has_more"])
        second = self.get("runs", params={"limit": 1, "before_id": first["next_before_id"]}).json
        self.assertEqual([row["id"] for row in second["items"]], [1])
        self.assertFalse(second["has_more"])

    def test_status_filter(self):
        result = self.get("alerts", params={"status": "active"})
        self.assertEqual([row["id"] for row in result.json["items"]], [1])

    def test_invalid_pagination_and_filters(self):
        for params in ({"limit": "0"}, {"limit": "201"}, {"before_id": "-1"}, {"limit": "1 OR 1=1"},
                       {"limit": "９"}, {"before_id": "9" * 20}, {"status": "invented"}):
            with self.subTest(params=params):
                self.assertEqual(self.get("runs", params=params).status_code, 400)
        self.assertEqual(self.get("tasks", params={"status": "active"}).status_code, 400)

    def test_missing_source_and_unknown_collection(self):
        self.assertEqual(self.get("tasks", source=999).status_code, 404)
        self.assertEqual(self.get("raw").status_code, 404)

    def test_run_items_source_guard_and_pagination(self):
        result = self.get("runs/1/items", params={"limit": 1})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json["items"][0]["external_address"], "0001")
        self.assertEqual(self.get("runs/2/items").status_code, 404)
        self.assertEqual(self.get("runs/1/items", params={"limit": 201}).status_code, 400)
        self.assertEqual(self.get("runs/1/items", params={"before_id": 1}).json["items"], [])

    def test_task_pause_resume_idempotent_and_audited(self):
        for action in ("pause", "pause", "enable"):
            self.assertEqual(self.control("tasks", action).status_code, 200)
        self.assertEqual(self.connection.db.execute("SELECT is_enabled, updated_by_user_id FROM tbl_jishiyun_tasks WHERE id=1").fetchone(), (1, 42))
        self.assertEqual(self.connection.db.execute("SELECT COUNT(*) FROM tbl_jishiyun_events").fetchone()[0], 2)

    def test_cancellation_fences_token_and_retains_results(self):
        self.assertEqual(self.control("runs", "cancel").status_code, 200)
        self.assertEqual(self.connection.db.execute(
            "SELECT status,lease_token,lease_expires_datetime_utc,missing_count FROM tbl_jishiyun_runs WHERE id=1"
        ).fetchone(), ("cancelled", None, None, 16))
        self.assertEqual(self.connection.db.execute("SELECT COUNT(*) FROM tbl_jishiyun_run_items WHERE run_id=1").fetchone()[0], 1)
        self.assertEqual(self.control("runs", "cancel").status_code, 200)
        self.assertEqual(self.connection.db.execute("SELECT COUNT(*) FROM tbl_jishiyun_events").fetchone()[0], 1)

    def test_cannot_cancel_completed_run(self):
        self.assertEqual(self.control("runs", "cancel", item=3).status_code, 409)

    def test_acknowledgement_never_recovers_alert_and_is_idempotent(self):
        for _ in range(2):
            self.assertEqual(self.control("alerts", "acknowledge").status_code, 200)
        self.assertEqual(self.connection.db.execute(
            "SELECT status,acknowledged_by_user_id,recovered_datetime_utc FROM tbl_jishiyun_alerts WHERE id=1"
        ).fetchone(), ("active", 42, None))
        self.assertEqual(self.connection.db.execute("SELECT COUNT(*) FROM tbl_jishiyun_events").fetchone()[0], 1)
        self.assertEqual(self.control("alerts", "acknowledge", item=2).status_code, 409)

    def test_mutations_cannot_cross_source(self):
        for collection, action, item in (("tasks", "pause", 2), ("runs", "cancel", 2), ("alerts", "acknowledge", 3)):
            self.assertEqual(self.control(collection, action, item=item).status_code, 404)
        self.assertEqual(self.connection.db.execute("SELECT COUNT(*) FROM tbl_jishiyun_events").fetchone()[0], 0)

    def test_audit_failure_rolls_back_state_and_redacts_error(self):
        self.connection.fail_on = "INSERT INTO tbl_jishiyun_events"
        with self.assertLogs(jishiyun.logger, level="ERROR"):
            result = self.control("tasks", "pause")
        self.assertEqual(result.status_code, 500)
        self.assertNotIn("sensitive", result.text)
        self.assertEqual(self.connection.db.execute("SELECT is_enabled FROM tbl_jishiyun_tasks WHERE id=1").fetchone()[0], 1)
        self.assertEqual(self.connection.rollbacks, 1)

    def test_write_rejects_missing_transactional_extension(self):
        self.connection.db.execute("DROP TABLE tbl_jishiyun_events")
        self.connection.db.commit()
        self.assertEqual(self.control("tasks", "pause").status_code, 409)
        self.assertEqual(self.connection.db.execute("SELECT is_enabled FROM tbl_jishiyun_tasks WHERE id=1").fetchone()[0], 1)

    def test_rejects_actor_override_and_unknown_action(self):
        for collection, action in (("tasks", "delete"), ("raw", "pause"), ("events", "enable")):
            self.assertEqual(self.control(collection, action).status_code, 400)
        self.assertEqual(self.control("tasks", "pause", extra={"actor_user_id": 999}).status_code, 400)
        self.assertEqual(self.client.simulate_post("/datasources/1/acquisition/tasks/1/control", json=[]).status_code, 400)

    def test_missing_authenticated_actor_fails_before_connecting(self):
        self.authorization.side_effect = lambda req: None
        self.assertEqual(self.control("tasks", "pause").status_code, 400)
        self.connect.assert_not_called()

    def test_authentication_failure_protects_every_route(self):
        self.authorization.side_effect = falcon.HTTPForbidden(title="Synthetic denied")
        for result in (self.get("tasks"), self.get("runs/1/items"), self.control("tasks", "pause")):
            self.assertEqual(result.status_code, 403)
        self.connect.assert_not_called()

    def test_real_auth_rejects_missing_headers(self):
        from core.useractivity import admin_control
        self.authorization.side_effect = admin_control
        self.assertEqual(self.get("tasks").status_code, 400)
        self.connect.assert_not_called()

    def test_real_auth_rejects_non_admin_and_read_only(self):
        from core.useractivity import admin_control
        self.authorization.side_effect = admin_control
        auth_connection = MagicMock()
        auth_connection.cursor.return_value.fetchone.return_value = (datetime.utcnow() + timedelta(hours=1),)
        self.connect.return_value = auth_connection
        for is_admin, read_only in ((False, False), (True, True)):
            with patch("core.useractivity.get_user_permission_context", return_value={
                "user_id": 42, "is_admin": is_admin, "is_read_only": read_only,
            }):
                result = self.client.simulate_get("/datasources/1/acquisition/tasks", headers={
                    "User-UUID": "synthetic-user", "Token": "synthetic-token",
                })
                self.assertEqual(result.status_code, 400)
        auth_connection.start_transaction.assert_not_called()

    def test_options_has_no_database_effect(self):
        self.assertEqual(self.client.simulate_options("/datasources/1/acquisition/tasks").status_code, 200)
        self.connect.assert_not_called()


class PendingPointCompatibilityTests(unittest.TestCase):
    def test_pending_register_has_no_business_latest_value(self):
        from core import datasource
        for types in (["JISHIYUN_REGISTER"], ["ENERGY_VALUE", "JISHIYUN_REGISTER"]):
            with self.subTest(types=types):
                system, historical = MagicMock(), MagicMock()
                system.cursor.return_value.fetchone.return_value = ("synthetic",)
                system.cursor.return_value.fetchall.return_value = [
                    (index, "synthetic", kind, "kWh", 999, 0, None, None,
                     Decimal("1"), Decimal("0"), 1, 0, "{}", None, None, None)
                    for index, kind in enumerate(types, 1)
                ]
                historical.cursor.return_value.fetchall.side_effect = [[], [], [(1, Decimal("12.5"))], []]
                api = falcon.App()
                api.add_route("/datasources/{id_}/points", datasource.DataSourcePointCollection())
                with patch.object(datasource, "admin_control"), patch.object(
                    datasource.mysql.connector, "connect", side_effect=[system, historical],
                ):
                    result = testing.TestClient(api).simulate_get("/datasources/1/points")
                self.assertEqual(result.status_code, 200, result.text)
                self.assertIsNone(result.json[-1]["latest_value"])
                if len(types) == 2:
                    self.assertEqual(result.json[0]["latest_value"], 12.5)


if __name__ == "__main__":
    unittest.main()
