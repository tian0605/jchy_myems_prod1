import json
import sqlite3
import unittest
import io
import tempfile
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4
from datetime import timezone
from decimal import Decimal

from myems_jishiyun.contracts import ContractError, parse_archives
from myems_jishiyun.execution import RunQueue
from myems_jishiyun.initialization import ArchiveInitializer, InitializationConflict, MeterConfiguration, PENDING_OBJECT_TYPE
from myems_jishiyun.persistence import PersistenceError
from myems_jishiyun.initialization import configuration_from_json
from myems_jishiyun.worker import main, bounded_text
from test_execution import ExecutionConnection


def archive(address: str = "0001", external_id: int = 1, name: str = "External meter") -> dict[str, object]:
    return {
        "ID": external_id, "Address": address, "AmmeterName": name, "AreaName": "Synthetic area",
        "TimesV": 1, "TimesA": 120, "SingleRate": False, "OnlineState": True,
        "AddDate": "2026-10-01 00:00:00",
    }


def configuration(name: str = "Approved meter") -> MeterConfiguration:
    return MeterConfiguration(name, 1, 1, True, Decimal("0"), Decimal("100"), "kWh", Decimal("999999999"))


class InitializationFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = ExecutionConnection()
        self.addCleanup(self.connection.db.close)
        self.queue = RunQueue(lambda: self.connection, 1)
        self.service = ArchiveInitializer(self.queue)
        self.connection.db.executescript("""
            DELETE FROM myems_system_db.tbl_jishiyun_meter_mappings;
            ALTER TABLE myems_system_db.tbl_jishiyun_meter_mappings ADD COLUMN meter_id INTEGER;
            ALTER TABLE myems_system_db.tbl_jishiyun_meter_mappings ADD COLUMN point_id INTEGER;
            ALTER TABLE myems_system_db.tbl_jishiyun_meter_mappings ADD COLUMN initialization_run_id INTEGER;
            ALTER TABLE myems_system_db.tbl_jishiyun_meter_mappings ADD COLUMN archive_json TEXT;
            ALTER TABLE myems_system_db.tbl_jishiyun_meter_mappings ADD COLUMN last_archive_sync_datetime_utc TEXT;
            CREATE UNIQUE INDEX myems_system_db.mapping_address
                ON tbl_jishiyun_meter_mappings(data_source_id, external_address);
            CREATE UNIQUE INDEX myems_system_db.mapping_external_id
                ON tbl_jishiyun_meter_mappings(data_source_id, external_id);
            CREATE UNIQUE INDEX myems_system_db.mapping_point ON tbl_jishiyun_meter_mappings(point_id);
            CREATE UNIQUE INDEX myems_system_db.mapping_meter ON tbl_jishiyun_meter_mappings(meter_id);
            CREATE TABLE myems_system_db.tbl_energy_categories (id INTEGER PRIMARY KEY);
            CREATE TABLE myems_system_db.tbl_cost_centers (id INTEGER PRIMARY KEY);
            INSERT INTO myems_system_db.tbl_energy_categories VALUES (1);
            INSERT INTO myems_system_db.tbl_cost_centers VALUES (1);
            CREATE TABLE myems_system_db.tbl_meters (
                id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, uuid TEXT,
                energy_category_id INTEGER, is_counted INTEGER, hourly_low_limit TEXT,
                hourly_high_limit TEXT, cost_center_id INTEGER
            );
            CREATE TABLE myems_system_db.tbl_points (
                id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, data_source_id INTEGER,
                object_type TEXT, units TEXT, high_limit TEXT, low_limit TEXT, ratio TEXT,
                offset_constant TEXT, is_trend INTEGER, is_virtual INTEGER, address TEXT,
                faults TEXT, definitions TEXT
            );
            CREATE TABLE myems_system_db.tbl_meters_points (
                id INTEGER PRIMARY KEY AUTOINCREMENT, meter_id INTEGER, point_id INTEGER
            );
            CREATE TABLE myems_system_db.tbl_jishiyun_run_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER, external_address TEXT, external_id INTEGER,
                archive_json TEXT, approved_configuration_json TEXT, status TEXT, meter_id INTEGER, point_id INTEGER,
                error_message TEXT, UNIQUE(run_id, external_address), UNIQUE(run_id, external_id)
            );
            INSERT INTO information_schema.TABLES VALUES
                ('myems_system_db','tbl_meters','InnoDB'),
                ('myems_system_db','tbl_points','InnoDB'),
                ('myems_system_db','tbl_meters_points','InnoDB'),
                ('myems_system_db','tbl_energy_categories','InnoDB'),
                ('myems_system_db','tbl_cost_centers','InnoDB'),
                ('myems_system_db','tbl_jishiyun_run_items','InnoDB');
            INSERT INTO myems_system_db.tbl_jishiyun_tasks
                (id,data_source_id,task_type,is_enabled,delay_minutes,provider_utc_offset_minutes,parameters_json)
                VALUES (3,1,'meter_initialize',1,40,480,'{"encoding":"json"}');
            INSERT INTO myems_system_db.tbl_jishiyun_runs
                (id,task_id,data_source_id,status) VALUES (10,3,1,'preview');
        """)

    def preview(self, *rows: dict[str, object], run_id: int = 10) -> int:
        body = json.dumps({"IsSuccess": True, "Code": "0000", "Count": len(rows), "Data": list(rows)})
        return self.service.preview(run_id, parse_archives(body, timezone.utc))

    def count(self, table: str) -> int:
        return self.connection.db.execute(f"SELECT COUNT(*) FROM myems_system_db.{table}").fetchone()[0]

    def approved(self, address: str = "0001", name: str = "Approved meter") -> None:
        self.preview(archive(address))
        self.service.approve(10, address, configuration(name), 7)


class InitializationTests(InitializationFixture):
    def test_atomic_batch_approval_and_actor(self) -> None:
        self.preview(archive(), archive("0002", 2))
        with self.assertRaises(PersistenceError):
            self.service.approve_many(10, (
                ("0001", configuration()), ("missing", configuration("Second")),
            ), 7)
        self.assertEqual(self.connection.db.execute(
            "SELECT COUNT(*) FROM myems_system_db.tbl_jishiyun_run_items WHERE status='pending'",
        ).fetchone()[0], 0)
        self.service.approve_many(10, (("0001", configuration()), ("0002", configuration("Second"))), 7)
        self.service.approve_many(10, (("0001", configuration()), ("0002", configuration("Second"))), 7)
        rows = self.connection.db.execute(
            "SELECT actor_user_id FROM myems_system_db.tbl_jishiyun_events WHERE event_type='archive_item_approved'",
        ).fetchall()
        self.assertEqual(rows, [(7,), (7,)])

    def test_approval_rejects_duplicate_addresses_and_invalid_late_item(self) -> None:
        self.preview(archive())
        for items in ((), (("0001", configuration()), ("0001", configuration("Changed")))):
            with self.assertRaises(ContractError):
                self.service.approve_many(10, items, 7)
        invalid = MeterConfiguration("Bad", 1, 1, True, Decimal("10"), Decimal("1"), "kWh", Decimal("100"))
        with self.assertRaises(ContractError):
            self.service.approve_many(10, (("0001", configuration()), ("0002", invalid)), 7)
        self.assertEqual(self.connection.db.execute(
            "SELECT status FROM myems_system_db.tbl_jishiyun_run_items",
        ).fetchone()[0], "preview")

    def test_preview_page_is_bounded_safe_and_source_scoped(self) -> None:
        self.preview(archive(), archive("0002", 2))
        self.service.approve(10, "0001", configuration(), 7)
        first = self.service.preview_page(10, limit=1)
        self.assertTrue(first["has_more"])
        self.assertEqual(first["items"][0]["external_address"], "0002")
        second = self.service.preview_page(10, before_id=first["next_before_id"], limit=1)
        self.assertFalse(second["has_more"])
        item = second["items"][0]
        self.assertEqual(item["name"], "External meter")
        self.assertEqual(item["configuration"]["cost_center_id"], 1)
        self.assertNotIn("archive_json", item)
        other = ArchiveInitializer(RunQueue(lambda: self.connection, 2))
        with self.assertRaises(PersistenceError):
            other.preview_page(10)
        for limit in (0, 201):
            with self.assertRaises(ContractError):
                self.service.preview_page(10, limit=limit)

    def test_preview_is_read_only_for_business_tables_and_idempotent(self) -> None:
        self.assertEqual(self.preview(archive(), archive("0002", 2)), 2)
        self.assertEqual(self.preview(archive("0002", 2), archive()), 2)
        self.assertEqual(self.count("tbl_jishiyun_run_items"), 2)
        self.assertEqual(self.count("tbl_meters"), 0)
        self.assertEqual(self.count("tbl_points"), 0)

    def test_create_preview_is_atomic_idempotent_and_checks_operator(self) -> None:
        body = json.dumps({"IsSuccess": True, "Code": "0000", "Count": 1, "Data": [archive()]})
        archives = parse_archives(body, timezone.utc)
        key = str(uuid4())
        run_id = self.service.create_preview(3, key, archives, 7)
        self.assertEqual(self.service.create_preview(3, key, archives, 7), run_id)
        with self.assertRaises(InitializationConflict):
            self.service.create_preview(3, key, archives, 8)
        self.assertEqual(self.count("tbl_jishiyun_run_items"), 1)
        self.assertEqual(self.count("tbl_meters"), 0)

    def test_preview_creation_failure_rolls_back_parent_and_items(self) -> None:
        body = json.dumps({"IsSuccess": True, "Code": "0000", "Count": 1, "Data": [archive()]})
        archives = parse_archives(body, timezone.utc)
        self.connection.fail_on = "INSERT INTO myems_system_db.tbl_jishiyun_events"
        with self.assertRaises(sqlite3.OperationalError):
            self.service.create_preview(3, str(uuid4()), archives, 7)
        self.assertEqual(self.count("tbl_jishiyun_runs"), 1)  # Only the seeded preview.
        self.assertEqual(self.count("tbl_jishiyun_run_items"), 0)

    def test_empty_preview_request_cannot_be_reused_for_new_archive_set(self) -> None:
        key = str(uuid4())
        self.service.create_preview(3, key, (), 7)
        body = json.dumps({"IsSuccess": True, "Code": "0000", "Count": 1, "Data": [archive()]})
        with self.assertRaises(InitializationConflict):
            self.service.create_preview(3, key, parse_archives(body, timezone.utc), 7)

    def test_empty_existing_run_preview_is_also_immutable(self) -> None:
        self.assertEqual(self.preview(), 0)
        with self.assertRaises(InitializationConflict):
            self.preview(archive())

    def test_changed_preview_does_not_overwrite_user_approval(self) -> None:
        self.approved()
        with self.assertRaises(InitializationConflict):
            self.preview(archive(name="Changed supplier name"))
        status = self.connection.db.execute("SELECT status FROM myems_system_db.tbl_jishiyun_run_items").fetchone()
        self.assertEqual(status, ("pending",))

    def test_initialize_creates_exactly_one_complete_disabled_configuration(self) -> None:
        self.approved()
        self.assertEqual(self.service.initialize(10, "0001", 7), "created")
        for table in ("tbl_meters", "tbl_points", "tbl_meters_points", "tbl_jishiyun_meter_mappings"):
            self.assertEqual(self.count(table), 1)
        mapping = self.connection.db.execute(
            "SELECT configuration_status,is_enabled,collection_start_utc FROM myems_system_db.tbl_jishiyun_meter_mappings"
        ).fetchone()
        self.assertEqual(mapping, ("pending", 0, None))
        point = self.connection.db.execute(
            "SELECT object_type, ratio, offset_constant, units FROM myems_system_db.tbl_points"
        ).fetchone()
        self.assertEqual(point, (PENDING_OBJECT_TYPE, "1", "0", "kWh"))
        self.assertNotEqual(point[0], "ENERGY_VALUE")

    def test_repeat_initialize_does_not_create_duplicates(self) -> None:
        self.approved()
        self.service.initialize(10, "0001", 7)
        self.assertEqual(self.service.initialize(10, "0001", 7), "created")
        self.assertEqual(self.count("tbl_meters"), 1)
        self.assertEqual(self.count("tbl_points"), 1)
        self.assertEqual(self.count("tbl_jishiyun_meter_mappings"), 1)

    def test_new_run_reuses_identity_without_overwriting_local_configuration(self) -> None:
        self.approved()
        self.service.initialize(10, "0001", 7)
        self.connection.db.execute(
            "INSERT INTO myems_system_db.tbl_jishiyun_runs (id,task_id,data_source_id,status) VALUES (11,3,1,'preview')"
        )
        self.connection.db.commit()
        self.preview(archive(name="Supplier changed"), run_id=11)
        self.service.approve(11, "0001", configuration("Different local proposal"), 7)
        self.assertEqual(self.service.initialize(11, "0001", 7), "reused")
        self.assertEqual(
            self.connection.db.execute("SELECT name FROM myems_system_db.tbl_meters").fetchone(), ("Approved meter",),
        )
        self.assertEqual(self.count("tbl_meters"), 1)

    def test_approval_required_and_reference_ids_are_checked(self) -> None:
        self.preview(archive())
        with self.assertRaises(PersistenceError):
            self.service.initialize(10, "0001", 7)
        self.connection.db.execute("DELETE FROM myems_system_db.tbl_cost_centers")
        self.connection.db.commit()
        with self.assertRaises(PersistenceError):
            self.service.approve(10, "0001", configuration(), 7)
        self.assertEqual(self.count("tbl_meters"), 0)

    def test_missing_reference_at_execution_rolls_back_without_business_objects(self) -> None:
        self.approved()
        self.connection.db.execute("DELETE FROM myems_system_db.tbl_energy_categories")
        self.connection.db.commit()
        with self.assertRaises(PersistenceError):
            self.service.initialize(10, "0001", 7)
        self.assertEqual(self.count("tbl_meters"), 0)

    def test_failure_at_each_write_rolls_back_entire_meter(self) -> None:
        self.approved()
        for fragment in (
            "INSERT INTO myems_system_db.tbl_points",
            "INSERT INTO myems_system_db.tbl_meters_points",
            "INSERT INTO myems_system_db.tbl_jishiyun_meter_mappings",
            "UPDATE myems_system_db.tbl_jishiyun_run_items",
            "INSERT INTO myems_system_db.tbl_jishiyun_events",
        ):
            self.connection.fail_on = fragment
            with self.subTest(fragment=fragment), self.assertRaises(sqlite3.OperationalError):
                self.service.initialize(10, "0001", 7)
            for table in ("tbl_meters", "tbl_points", "tbl_meters_points", "tbl_jishiyun_meter_mappings"):
                self.assertEqual(self.count(table), 0)
        self.connection.fail_on = None
        self.assertEqual(self.service.initialize(10, "0001", 7), "created")

    def test_same_name_never_automatically_binds_unrelated_meter(self) -> None:
        self.approved()
        self.connection.db.execute("INSERT INTO myems_system_db.tbl_meters (name) VALUES ('Approved meter')")
        self.connection.db.commit()
        with self.assertRaises(InitializationConflict):
            self.service.initialize(10, "0001", 7)
        self.assertEqual(self.count("tbl_points"), 0)
        self.assertEqual(self.count("tbl_jishiyun_meter_mappings"), 0)

    def test_external_id_or_address_conflict_is_not_auto_repaired(self) -> None:
        self.approved()
        self.connection.db.execute(
            "INSERT INTO myems_system_db.tbl_jishiyun_meter_mappings "
            "(data_source_id,external_address,external_id,meter_id,point_id) VALUES (1,'other',1,9,9)"
        )
        self.connection.db.commit()
        with self.assertRaises(InitializationConflict):
            self.service.initialize(10, "0001", 7)
        self.assertEqual(self.count("tbl_meters"), 0)

    def test_removed_or_broken_mapping_does_not_trigger_silent_recreation(self) -> None:
        self.approved()
        self.service.initialize(10, "0001", 7)
        self.connection.db.execute("DELETE FROM myems_system_db.tbl_meters_points")
        self.connection.db.commit()
        with self.assertRaises(InitializationConflict):
            self.service.initialize(10, "0001", 7)
        self.connection.db.execute("DELETE FROM myems_system_db.tbl_jishiyun_meter_mappings")
        self.connection.db.commit()
        with self.assertRaises(InitializationConflict):
            self.service.initialize(10, "0001", 7)
        self.assertEqual(self.count("tbl_meters"), 1)

    def test_changed_point_identity_requires_manual_reconciliation(self) -> None:
        self.approved()
        self.service.initialize(10, "0001", 7)
        self.connection.db.execute("UPDATE myems_system_db.tbl_points SET address='{}'")
        self.connection.db.commit()
        with self.assertRaises(InitializationConflict):
            self.service.initialize(10, "0001", 7)
        self.assertEqual(self.count("tbl_meters"), 1)

    def test_source_run_status_and_table_engine_are_enforced(self) -> None:
        self.connection.db.execute("UPDATE myems_system_db.tbl_jishiyun_runs SET data_source_id = 2 WHERE id = 10")
        self.connection.db.commit()
        with self.assertRaises(PersistenceError):
            self.preview(archive())
        self.connection.db.execute("UPDATE myems_system_db.tbl_jishiyun_runs SET data_source_id = 1,status='cancelled' WHERE id=10")
        self.connection.db.commit()
        with self.assertRaises(PersistenceError):
            self.preview(archive())
        self.connection.db.execute("UPDATE information_schema.TABLES SET ENGINE='MyISAM' WHERE TABLE_NAME='tbl_meters'")
        self.connection.db.commit()
        with self.assertRaises(PersistenceError):
            self.preview(archive())

    def test_configuration_validation_requires_explicit_business_values(self) -> None:
        configurations = (
            MeterConfiguration(" ", 1, 1, True, Decimal(0), Decimal(100), "kWh", Decimal(1000)),
            MeterConfiguration("name", True, 1, True, Decimal(0), Decimal(100), "kWh", Decimal(1000)),
            MeterConfiguration("name", 1, 1, 1, Decimal(0), Decimal(100), "kWh", Decimal(1000)),
            MeterConfiguration("name", 1, 1, True, Decimal(101), Decimal(100), "kWh", Decimal(1000)),
            MeterConfiguration("name", 1, 1, True, Decimal(0), Decimal(100), "", Decimal(1000)),
            MeterConfiguration("name", 1, 1, True, Decimal(0), Decimal(100), "kWh", Decimal(0)),
        )
        for value in configurations:
            with self.subTest(value=value), self.assertRaises(ContractError):
                value.serialized()

    def test_multiple_devices_commit_independently(self) -> None:
        self.preview(archive(), archive("0002", 2))
        for address in ("0001", "0002"):
            self.service.approve(10, address, configuration(address), 7)
        self.service.initialize(10, "0001", 7)
        self.connection.fail_on = "INSERT INTO myems_system_db.tbl_points"
        with self.assertRaises(sqlite3.OperationalError):
            self.service.initialize(10, "0002", 7)
        self.assertEqual(self.count("tbl_meters"), 1)
        self.assertEqual(self.count("tbl_jishiyun_meter_mappings"), 1)


class InitializationCommandTests(unittest.TestCase):
    def test_configuration_round_trip_and_corrupt_shapes(self) -> None:
        config = configuration()
        self.assertEqual(configuration_from_json(config.serialized()), config)
        for value in ("[]", "invalid", "{}", '{"name":"x","extra":1}'):
            with self.subTest(value=value), self.assertRaises(PersistenceError):
                configuration_from_json(value)

    def test_preview_command_uses_local_response_without_http_or_key(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "synthetic.json"
            path.write_text(json.dumps({
                "IsSuccess": True, "Code": "0000", "Count": 1, "Data": [archive()],
            }), encoding="utf-8")
            output = io.StringIO()
            with patch.dict("os.environ", {"JISHIYUN_DATA_SOURCE_ID": "1"}, clear=True), patch(
                "myems_jishiyun.worker.database_factory",
            ), patch("myems_jishiyun.worker.ArchiveInitializer") as service, patch("myems_jishiyun.worker.JishiyunClient") as client, redirect_stdout(output):
                service.return_value.create_preview.return_value = 10
                self.assertEqual(main([
                    "archive-preview", "--task-id", "3", "--request-key", str(uuid4()), "--actor", "7",
                    "--response-file", str(path), "--utc-offset=+08:00",
                ]), 0)
                client.assert_not_called()
            self.assertEqual(json.loads(output.getvalue())["archive_count"], 1)

    def test_approval_command_validates_file_before_calling_service(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "configuration.json"
            path.write_text(configuration().serialized(), encoding="utf-8")
            with patch.dict("os.environ", {"JISHIYUN_DATA_SOURCE_ID": "1"}, clear=True), patch(
                "myems_jishiyun.worker.database_factory",
            ), patch("myems_jishiyun.worker.ArchiveInitializer") as service, redirect_stdout(io.StringIO()):
                self.assertEqual(main([
                    "archive-approve", "--run-id", "10", "--address", "0001", "--actor", "7",
                    "--configuration-file", str(path),
                ]), 0)
                service.return_value.approve.assert_called_once_with(10, "0001", configuration(), 7)

    def test_input_size_and_missing_file_fail_explicitly(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "large.json"
            path.write_bytes(b"12345")
            with patch("myems_jishiyun.worker.MAX_RESPONSE_BYTES", 4), self.assertRaises(ValueError):
                bounded_text(path)
            with patch.dict("os.environ", {"JISHIYUN_DATA_SOURCE_ID": "1"}, clear=True), patch(
                "myems_jishiyun.worker.database_factory",
            ), redirect_stderr(io.StringIO()):
                self.assertEqual(main([
                    "archive-preview", "--task-id", "3", "--request-key", str(uuid4()), "--actor", "7",
                    "--response-file", str(path.parent / "missing.json"), "--utc-offset=+08:00",
                ]), 1)


if __name__ == "__main__":
    unittest.main()
