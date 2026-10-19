"""Mapping scope/ownership protection; SQLite behavior is not a MySQL locking test."""

import json
import unittest
from datetime import datetime

import falcon
from core import jishiyun
import test_jishiyun as fixtures


class MappingCursor(fixtures.Cursor):
    def fetchone(self):
        row = super().fetchone()
        if row is None:
            return None
        return tuple(datetime.fromisoformat(value) if column[0] in (
            "collection_start_utc", "collection_end_utc") and value is not None else value
                     for column, value in zip(self.cursor.description, row))


class MappingTests(unittest.TestCase):
    authorize = staticmethod(fixtures.WorkbenchTests.authorize)

    def setUp(self):
        fixtures.WorkbenchTests.setUp(self)
        self.connection.cursor = lambda: MappingCursor(self.connection)
        self.connection.db.executescript("""
            CREATE TABLE tbl_meters (id INTEGER PRIMARY KEY,name TEXT);
            CREATE TABLE tbl_points (id INTEGER PRIMARY KEY,name TEXT,data_source_id INTEGER,
                object_type TEXT,address TEXT,ratio NUMERIC,offset_constant NUMERIC,is_virtual INTEGER,is_trend INTEGER);
            CREATE TABLE tbl_meters_points (id INTEGER PRIMARY KEY,meter_id INTEGER,point_id INTEGER);
            INSERT INTO tbl_meters VALUES(10,'Synthetic meter');
            INSERT INTO tbl_points VALUES(20,'Synthetic register',1,'JISHIYUN_REGISTER',
                '{"provider":"jishiyun","address":"0001","field":"ZYGDNSZ"}',1,0,0,1);
            INSERT INTO tbl_meters_points VALUES(1,10,20);
            INSERT INTO tbl_jishiyun_meter_mappings
                (id,data_source_id,external_address,external_id,meter_id,point_id,configuration_status,
                 is_enabled,archive_json) VALUES(1,1,'0001',7,10,20,'pending',0,'{"ID":7,"Address":"0001"}');
        """)
        self.connection.db.commit()
        # Retain the same existing route tree when registering the new resource.
        self.client.app.add_route("/datasources/{id_}/acquisition/mappings/{item_id}", jishiyun.MappingConfiguration())
        self.client.app.add_route("/datasources/{id_}/acquisition/mappings/{item_id}/raw-collection",
                                  jishiyun.MappingCollection())
        self.client.app.add_route("/datasources/{id_}/acquisition/mapping-batch", jishiyun.MappingBatch())
        self.base = "/datasources/1/acquisition/mappings/1"

    def snapshot(self):
        result = self.client.simulate_get(self.base)
        self.assertEqual(result.status_code, 200, result.text)
        return result.json["configuration"]

    def update(self, expected=None, **extra):
        body = {"collection_start_utc": "2026-10-01T08:00:00+08:00",
                "collection_end_utc": "2026-10-02T00:00:00Z",
                "expected_configuration": expected if expected is not None else self.snapshot()}
        body.update(extra)
        return self.client.simulate_post(self.base, json=body)

    def execute(self, sql):
        self.connection.db.execute(sql)
        self.connection.db.commit()

    def test_listing_and_safe_verified_snapshot(self):
        result = self.client.simulate_get("/datasources/1/acquisition/mappings").json["items"][0]
        self.assertEqual(result["external_address"], "0001")
        self.assertNotIn("archive_json", result)
        detail = self.client.simulate_get(self.base).json
        self.assertTrue(detail["ownership_verified"])
        self.assertFalse(detail["activation_available"])
        self.assertEqual(detail["meter_name"], "Synthetic meter")
        self.assertNotIn("archive_json", detail["configuration"])

    def test_range_normalization_audited_and_replay_is_idempotent_without_activation(self):
        self.execute("DELETE FROM tbl_jishiyun_runs WHERE data_source_id=1")
        previous = self.snapshot()
        result = self.update(previous)
        self.assertEqual(result.status_code, 200, result.text)
        self.assertTrue(result.json["changed"])
        self.assertFalse(self.update(previous).json["changed"])
        snapshot = self.snapshot()
        self.assertEqual(snapshot["collection_start_utc"], "2026-10-01T00:00:00")
        self.assertEqual((snapshot["configuration_status"], snapshot["is_enabled"]), ("pending", 0))
        row = self.connection.db.execute(
            "SELECT mapping_id,actor_user_id,details_json FROM tbl_jishiyun_events "
            "WHERE event_type='admin_mapping_range_updated'",
        ).fetchall()
        self.assertEqual(len(row), 1)
        self.assertEqual(row[0][:2], (1, 42))
        self.assertEqual(json.loads(row[0][2])["before"], previous)

    def test_invalid_times_payloads_and_timezone_are_rejected(self):
        previous = self.snapshot()
        for value in (None, "", "2026-10-01T00:00:00", "2026-10-01T00:01:00Z",
                      "2026-10-01T00:00:01Z", "2026-10-01T00:00:00.000001Z", 0, {},
                      "0999-01-01T00:00:00Z"):
            self.assertEqual(self.update(previous, collection_start_utc=value).status_code, 400, value)
        self.assertEqual(self.update(previous, collection_end_utc="2026-10-01T00:00:00Z").status_code, 400)
        self.assertEqual(self.update(previous, is_enabled=True).status_code, 400)
        self.assertEqual(self.update(previous, meter_id=99).status_code, 400)
        self.assertEqual(self.update({}).status_code, 400)

    def test_stale_and_source_energy_history_freeze(self):
        previous = self.snapshot()
        self.assertEqual(self.update(previous).status_code, 409)  # Open source runs.
        self.execute("UPDATE tbl_jishiyun_runs SET status='done',task_id=1 WHERE data_source_id=1")
        self.assertEqual(self.update(previous).status_code, 409)  # Any retained energy history.
        self.execute("DELETE FROM tbl_jishiyun_runs WHERE data_source_id=1")
        self.assertEqual(self.update(dict(previous, external_id=99)).status_code, 409)
        self.assertEqual(self.update(previous).status_code, 200)

    def test_enabled_and_conflicted_mappings_cannot_be_configured(self):
        previous = self.snapshot()
        self.execute("UPDATE tbl_jishiyun_meter_mappings SET is_enabled=1")
        self.assertEqual(self.update(previous).status_code, 409)
        self.execute("UPDATE tbl_jishiyun_meter_mappings SET is_enabled=0,configuration_status='conflict'")
        self.assertEqual(self.update(previous).status_code, 409)

    def test_broken_or_shared_associations_are_not_implicitly_repaired(self):
        for sql in ("DELETE FROM tbl_meters_points",
                    "INSERT INTO tbl_meters_points VALUES(2,11,20)",
                    "INSERT INTO tbl_meters_points VALUES(2,10,21)"):
            with self.subTest(sql=sql):
                self.execute(sql)
                self.assertEqual(self.client.simulate_get(self.base).status_code, 409)
                self.execute("DELETE FROM tbl_meters_points")
                self.execute("INSERT INTO tbl_meters_points VALUES(1,10,20)")

    def test_point_or_external_identity_changes_block_read_and_write(self):
        previous = self.snapshot()
        for field, value in (("data_source_id", "2"), ("object_type", "'ENERGY_VALUE'"),
                             ("ratio", "120"), ("offset_constant", "1"), ("is_virtual", "1"),
                             ("is_trend", "0"), ("address", "'{}'")):
            old = self.connection.db.execute(f"SELECT {field} FROM tbl_points").fetchone()[0]
            self.execute(f"UPDATE tbl_points SET {field}={value}")
            self.assertEqual(self.client.simulate_get(self.base).status_code, 409)
            self.assertEqual(self.update(previous).status_code, 409)
            self.connection.db.execute(f"UPDATE tbl_points SET {field}=?", (old,))
            self.connection.db.commit()
        self.execute("UPDATE tbl_jishiyun_meter_mappings SET archive_json='{\"ID\":8,\"Address\":\"0001\"}'")
        self.assertEqual(self.client.simulate_get(self.base).status_code, 409)

    def test_permissions_cross_source_missing_entities_and_audit_rollback(self):
        self.assertEqual(self.client.simulate_get("/datasources/2/acquisition/mappings/1").status_code, 404)
        previous = self.snapshot()
        self.execute("DELETE FROM tbl_jishiyun_runs WHERE data_source_id=1")
        self.connection.fail_on = "INSERT INTO tbl_jishiyun_events"
        self.assertEqual(self.update(previous).status_code, 500)
        self.connection.fail_on = None
        self.assertEqual(self.snapshot(), previous)
        self.authorization.side_effect = falcon.HTTPForbidden(title="Administrator required")
        self.assertEqual(self.client.simulate_get(self.base).status_code, 403)
        self.assertEqual(self.update(previous).status_code, 403)

    def test_missing_local_objects_and_nontransactional_schema_are_explicit_conflicts(self):
        for table in ("tbl_points", "tbl_meters"):
            row = self.connection.db.execute("SELECT * FROM " + table).fetchone()
            self.execute("DELETE FROM " + table)
            self.assertEqual(self.client.simulate_get(self.base).status_code, 409)
            self.connection.db.execute("INSERT INTO " + table + " VALUES(" + ",".join("?" for _ in row) + ")", row)
            self.connection.db.commit()
        self.execute("DROP TABLE tbl_meters_points")
        self.assertEqual(self.client.simulate_get(self.base).status_code, 409)

    def test_parent_batch_without_children_freezes_range(self):
        self.execute("DELETE FROM tbl_jishiyun_runs WHERE data_source_id=1")
        self.execute("INSERT INTO tbl_jishiyun_backfill_batches(id,data_source_id,task_id) VALUES(1,1,1)")
        self.assertEqual(self.update().status_code, 409)


class RawCollectionTests(unittest.TestCase):
    authorize = staticmethod(fixtures.WorkbenchTests.authorize)
    snapshot = MappingTests.snapshot
    update = MappingTests.update
    execute = MappingTests.execute

    def setUp(self):
        MappingTests.setUp(self)
        self.execute("ALTER TABLE tbl_points ADD COLUMN units TEXT DEFAULT 'kWh'")
        self.execute("DELETE FROM tbl_jishiyun_runs WHERE data_source_id=1")

    def control(self, action="enable_raw", expected=None, **changes):
        body = {"action": action, "confirm_kwh_direct": True,
                "expected_configuration": self.snapshot() if expected is None else expected}
        body.update(changes)
        return self.client.simulate_post(self.base + "/raw-collection", json=body)

    def prepare_range(self):
        self.assertEqual(self.update().status_code, 200)

    def test_enable_pause_replay_and_staged_type_preserved(self):
        self.prepare_range()
        previous = self.snapshot()
        result = self.control(expected=previous)
        self.assertEqual(result.status_code, 200, result.text)
        self.assertFalse(result.json["business_activation"])
        self.assertFalse(self.control(expected=previous).json["changed"])
        current = self.snapshot()
        self.assertEqual((current["configuration_status"], current["is_enabled"]), ("ready", 1))
        self.assertEqual(self.control("pause_raw").status_code, 200)
        paused = self.snapshot()
        self.assertEqual((paused["configuration_status"], paused["is_enabled"]), ("ready", 0))
        self.assertEqual(paused["collection_end_utc"], current["collection_end_utc"])
        self.assertEqual(self.connection.db.execute("SELECT object_type,ratio,offset_constant FROM tbl_points").fetchone(),
                         ("JISHIYUN_REGISTER", 1, 0))
        events = self.connection.db.execute(
            "SELECT actor_user_id,details_json FROM tbl_jishiyun_events "
            "WHERE event_type IN ('admin_mapping_enable_raw','admin_mapping_pause_raw')",
        ).fetchall()
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0][0], 42)
        self.assertTrue(json.loads(events[0][1])["confirm_kwh_direct"])
        self.assertFalse(json.loads(events[0][1])["business_activation"])

    def test_required_confirmation_scope_unit_and_stale_configuration(self):
        self.assertEqual(self.control().status_code, 409)
        self.prepare_range()
        for value in (False, None, 1, "true"):
            self.assertEqual(self.control(confirm_kwh_direct=value).status_code, 400)
        self.assertEqual(self.control(action="activate_business").status_code, 400)
        self.assertEqual(self.control(expected={}).status_code, 400)
        self.assertEqual(self.control(expected=dict(self.snapshot(), external_id=99)).status_code, 409)
        self.execute("UPDATE tbl_points SET units='Wh'")
        self.assertEqual(self.control().status_code, 409)
        self.execute("UPDATE tbl_points SET units='kWh'")
        self.execute("UPDATE tbl_jishiyun_meter_mappings SET configuration_status='conflict'")
        # Ownership still verifies, but conflicted configurations may not be enabled.
        self.assertEqual(self.control().status_code, 409)

    def test_open_runs_and_uncancelled_batches_block_scope_changes(self):
        self.prepare_range()
        self.execute("INSERT INTO tbl_jishiyun_runs(id,data_source_id,task_id,status) VALUES(90,1,1,'queued')")
        self.assertEqual(self.control().status_code, 409)
        self.execute("UPDATE tbl_jishiyun_runs SET status='cancelled'")
        self.execute("INSERT INTO tbl_jishiyun_backfill_batches(id,data_source_id,task_id,is_cancelled) VALUES(90,1,1,0)")
        self.assertEqual(self.control().status_code, 409)
        self.execute("UPDATE tbl_jishiyun_backfill_batches SET is_cancelled=1")
        self.assertEqual(self.control().status_code, 200)
        self.execute("UPDATE tbl_jishiyun_runs SET status='running'")
        self.assertEqual(self.control("pause_raw").status_code, 409)

    def test_control_permissions_cross_source_ownership_and_audit_rollback(self):
        self.prepare_range()
        before = self.snapshot()
        body = {"action": "enable_raw", "confirm_kwh_direct": True, "expected_configuration": before}
        self.assertEqual(self.client.simulate_post(
            "/datasources/2/acquisition/mappings/1/raw-collection", json=body).status_code, 404)
        self.connection.fail_on = "INSERT INTO tbl_jishiyun_events"
        self.assertEqual(self.control(expected=before).status_code, 500)
        self.connection.fail_on = None
        self.assertEqual(self.snapshot(), before)
        self.execute("UPDATE tbl_points SET ratio=2")
        self.assertEqual(self.control(expected=before).status_code, 409)
        self.authorization.side_effect = falcon.HTTPForbidden(title="Administrator required")
        self.assertEqual(self.control(expected=before).status_code, 403)


class MappingBatchTests(unittest.TestCase):
    authorize = staticmethod(fixtures.WorkbenchTests.authorize)
    execute = MappingTests.execute

    def setUp(self):
        RawCollectionTests.setUp(self)
        self.execute("INSERT INTO tbl_meters VALUES(11,'Second meter')")
        self.execute("""INSERT INTO tbl_points VALUES(21,'Second point',1,'JISHIYUN_REGISTER',
            '{"provider":"jishiyun","address":"0002","field":"ZYGDNSZ"}',1,0,0,1,'kWh')""")
        self.execute("INSERT INTO tbl_meters_points VALUES(2,11,21)")
        self.execute("""INSERT INTO tbl_jishiyun_meter_mappings
            (id,data_source_id,external_address,external_id,meter_id,point_id,configuration_status,is_enabled,archive_json)
            VALUES(2,1,'0002',8,11,21,'pending',0,'{"ID":8,"Address":"0002"}')""")

    def batch(self, action="edit_range", **changes):
        items = [{"id": id_, "expected_configuration": self.client.simulate_get(
            "/datasources/1/acquisition/mappings/" + str(id_)).json["configuration"]} for id_ in (1, 2)]
        body = {"action": action, "items": items, "confirm_kwh_direct": action in ("enable_raw", "pause_raw"),
                "collection_start_utc": "2026-10-01T00:00:00+08:00" if action == "edit_range" else None,
                "collection_end_utc": None}
        body.update(changes)
        return self.client.simulate_post("/datasources/1/acquisition/mapping-batch", json=body)

    def test_batch_ranges_enable_and_replay(self):
        result = self.batch()
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(len(result.json["items"]), 2)
        self.assertFalse(self.batch().json["items"][0]["changed"])
        self.assertEqual(self.batch("enable_raw").status_code, 200)
        self.assertEqual(self.connection.db.execute(
            "SELECT COUNT(*) FROM tbl_jishiyun_meter_mappings WHERE is_enabled=1 AND configuration_status='ready'"
        ).fetchone()[0], 2)
        self.assertFalse(self.batch("enable_raw").json["items"][0]["changed"])

    def test_batch_invalid_second_unit_rolls_back_first_and_audit(self):
        self.assertEqual(self.batch().status_code, 200)
        self.execute("UPDATE tbl_points SET units='Wh' WHERE id=21")
        before = self.connection.db.execute("SELECT COUNT(*) FROM tbl_jishiyun_events").fetchone()[0]
        response = self.batch("enable_raw")
        self.assertEqual(response.status_code, 409, response.text)
        self.assertIn("Mapping 2", response.json["description"])
        self.assertEqual(self.connection.db.execute(
            "SELECT SUM(is_enabled) FROM tbl_jishiyun_meter_mappings").fetchone()[0], 0)
        self.assertEqual(self.connection.db.execute("SELECT COUNT(*) FROM tbl_jishiyun_events").fetchone()[0], before)

    def test_batch_stale_missing_duplicate_limits_and_payload(self):
        expected = self.client.simulate_get("/datasources/1/acquisition/mappings/2").json["configuration"]
        self.assertEqual(self.batch(items=[]).status_code, 400)
        self.assertEqual(self.batch(items=[{"id": 2, "expected_configuration": expected}] * 201).status_code, 400)
        self.assertEqual(self.batch(items=[{"id": 2, "expected_configuration": expected}] * 2).status_code, 400)
        self.assertEqual(self.batch(items=[{"id": 99, "expected_configuration": expected}]).status_code, 409)
        self.assertEqual(self.batch(items=[{"id": 2, "expected_configuration": dict(expected, external_id=99)}]).status_code, 409)
        self.assertEqual(self.batch("unsupported").status_code, 400)
        self.assertEqual(self.batch("enable_raw", confirm_kwh_direct=1).status_code, 400)
        self.assertEqual(self.batch(collection_start_utc="2026-10-01T00:00:00").status_code, 400)

    def test_batch_pause_is_atomic_audited_and_idempotent_without_range_changes(self):
        self.assertEqual(self.batch().status_code, 200)
        self.assertEqual(self.batch("enable_raw").status_code, 200)
        previous = self.connection.db.execute(
            "SELECT id,collection_start_utc,collection_end_utc,configuration_status "
            "FROM tbl_jishiyun_meter_mappings ORDER BY id",
        ).fetchall()
        response = self.batch("pause_raw")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(all(item["changed"] for item in response.json["items"]))
        self.assertEqual(self.connection.db.execute(
            "SELECT SUM(is_enabled) FROM tbl_jishiyun_meter_mappings",
        ).fetchone()[0], 0)
        self.assertEqual(self.connection.db.execute(
            "SELECT id,collection_start_utc,collection_end_utc,configuration_status "
            "FROM tbl_jishiyun_meter_mappings ORDER BY id",
        ).fetchall(), previous)
        self.assertTrue(all(not item["changed"] for item in self.batch("pause_raw").json["items"]))
        self.assertEqual(self.connection.db.execute(
            "SELECT COUNT(*) FROM tbl_jishiyun_events WHERE event_type='admin_mapping_pause_raw'",
        ).fetchone()[0], 2)

    def test_batch_pause_stale_second_rolls_back_first_and_audits(self):
        self.assertEqual(self.batch().status_code, 200)
        self.assertEqual(self.batch("enable_raw").status_code, 200)
        items = [{"id": id_, "expected_configuration": self.client.simulate_get(
            "/datasources/1/acquisition/mappings/" + str(id_)).json["configuration"]} for id_ in (1, 2)]
        items[1]["expected_configuration"]["external_id"] = 99
        response = self.batch("pause_raw", items=items)
        self.assertEqual(response.status_code, 409, response.text)
        self.assertIn("Mapping 2", response.json["description"])
        self.assertEqual(self.connection.db.execute(
            "SELECT SUM(is_enabled) FROM tbl_jishiyun_meter_mappings",
        ).fetchone()[0], 2)
        self.assertEqual(self.connection.db.execute(
            "SELECT COUNT(*) FROM tbl_jishiyun_events WHERE event_type='admin_mapping_pause_raw'",
        ).fetchone()[0], 0)

    def test_batch_pause_keeps_open_run_and_retained_batch_guards(self):
        self.batch()
        self.batch("enable_raw")
        self.execute("INSERT INTO tbl_jishiyun_runs(id,data_source_id,status) VALUES(90,1,'running')")
        self.assertEqual(self.batch("pause_raw").status_code, 409)
        self.execute("UPDATE tbl_jishiyun_runs SET status='done' WHERE id=90")
        self.execute("INSERT INTO tbl_jishiyun_backfill_batches(id,data_source_id,task_id,is_cancelled) VALUES(90,1,1,0)")
        self.assertEqual(self.batch("pause_raw").status_code, 409)
        self.execute("UPDATE tbl_jishiyun_backfill_batches SET is_cancelled=1")
        self.assertEqual(self.batch("pause_raw", confirm_kwh_direct=False).status_code, 400)
        self.assertEqual(self.batch("pause_raw", collection_start_utc="2026-10-01T00:00:00Z").status_code, 400)
        self.assertEqual(self.batch("pause_raw").status_code, 200)

    def test_batch_range_stale_second_rolls_back_first_and_admin_is_required(self):
        items = [{"id": id_, "expected_configuration": self.client.simulate_get(
            "/datasources/1/acquisition/mappings/" + str(id_)).json["configuration"]} for id_ in (1, 2)]
        items[1]["expected_configuration"]["external_id"] = 99
        self.assertEqual(self.batch(items=items).status_code, 409)
        self.assertEqual(self.connection.db.execute(
            "SELECT COUNT(*) FROM tbl_jishiyun_meter_mappings WHERE collection_start_utc IS NOT NULL"
        ).fetchone()[0], 0)
        self.assertEqual(self.connection.db.execute("SELECT COUNT(*) FROM tbl_jishiyun_events").fetchone()[0], 0)
        self.authorization.side_effect = falcon.HTTPForbidden(title="Administrator required")
        # Build request directly because authorization also blocks the fixture's detail reads.
        self.assertEqual(self.client.simulate_post(
            "/datasources/1/acquisition/mapping-batch", json={}).status_code, 403)

    def test_exactly_200_mappings_can_be_configured_atomically(self):
        db = self.connection.db
        for id_ in range(3, 201):
            db.execute("INSERT INTO tbl_meters VALUES(?,?)", (id_ + 100, "Meter " + str(id_)))
            address = str(id_).zfill(4)
            db.execute("INSERT INTO tbl_points VALUES(?, ?,1,'JISHIYUN_REGISTER',?,1,0,0,1,'kWh')",
                       (id_ + 100, "Point " + str(id_), json.dumps(
                           {"provider": "jishiyun", "address": address, "field": "ZYGDNSZ"})))
            db.execute("INSERT INTO tbl_meters_points VALUES(?,?,?)", (id_, id_ + 100, id_ + 100))
            db.execute(
                "INSERT INTO tbl_jishiyun_meter_mappings "
                "(id,data_source_id,external_address,external_id,meter_id,point_id,configuration_status,"
                "is_enabled,archive_json) VALUES(?,1,?,?,?,?,'pending',0,?)",
                (id_, address, id_ + 100, id_ + 100, id_ + 100,
                 json.dumps({"ID": id_ + 100, "Address": address})),
            )
        db.commit()
        columns = jishiyun.MAPPING_COLUMNS
        items = [{"id": row[0], "expected_configuration": dict(zip(columns, row))}
                 for row in db.execute("SELECT " + ",".join(columns) + " FROM tbl_jishiyun_meter_mappings")]
        self.assertEqual(len(items), 200)
        result = self.batch(items=items)
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(len(result.json["items"]), 200)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM tbl_jishiyun_events").fetchone()[0], 200)


if __name__ == "__main__":
    unittest.main()
