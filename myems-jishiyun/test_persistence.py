"""Behavior tests using an explicit SQLite adapter, not MySQL acceptance tests."""

import json
import sqlite3
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import cast
from unittest.mock import patch

from myems_jishiyun.contracts import ContractError, EnergyReading, parse_readings
from myems_jishiyun.persistence import (
    Parameters, PersistenceError, ResultStore, ScopeMeter, prepare_batch, scope_snapshot,
)


START = datetime(2026, 10, 1, 4, tzinfo=timezone.utc)
END = START + timedelta(hours=1)
NOW = datetime(2026, 10, 1, 6)
TOKEN = "00000000-0000-4000-8000-000000000001"
OTHER_TOKEN = "00000000-0000-4000-8000-000000000002"
SCOPE = (ScopeMeter("0001", 1),)


def reading(minute: int = 0, value: str | None = "10", address: str = "0001", external_id: int = 1) -> EnergyReading:
    timestamp = START + timedelta(minutes=minute)
    return EnergyReading(
        address, external_id, timestamp,
        Decimal(value) if value is not None else None, None if value is not None else "Invalid register",
        {"Address": address, "AmmeterID": external_id, "ZYGDNSZ": value, "ReadingDate": timestamp.strftime("%Y-%m-%d %H:%M")},
    )


def complete(value: str = "10") -> tuple[EnergyReading, ...]:
    return tuple(reading(minute, value) for minute in (0, 15, 30, 45))


class BatchTests(unittest.TestCase):
    def test_scope_effective_ranges_determine_expected_slots(self) -> None:
        scope = (
            ScopeMeter("0001", 1, START + timedelta(minutes=15), START + timedelta(minutes=45)),
            ScopeMeter("0002", 2, START + timedelta(minutes=30)),
        )
        batch = prepare_batch((), scope, START, END)
        self.assertEqual(len(batch.expected), 4)
        self.assertEqual(set(batch.issues), {(key, "missing") for key in batch.expected})
        self.assertNotIn(("0001", START), batch.expected)
        self.assertNotIn(("0001", START + timedelta(minutes=45)), batch.expected)

    def test_sample_scale_33_devices_29_returned_has_16_gaps(self) -> None:
        scope = tuple(ScopeMeter(f"{index:04}", index + 1) for index in range(33))
        readings = tuple(
            reading(minute, address=f"{index:04}", external_id=index + 1)
            for index in range(29) for minute in (0, 15, 30, 45)
        )
        batch = prepare_batch(readings, scope, START, END)
        self.assertEqual((len(batch.expected), batch.returned_count, batch.valid_count), (132, 116, 116))
        self.assertEqual(len(batch.issues), 16)
        self.assertTrue(all(kind == "missing" for _, kind in batch.issues))

    def test_70_devices_full_day_has_exactly_6720_slots(self) -> None:
        scope = tuple(ScopeMeter(f"{index:04}", index + 1) for index in range(70))
        readings = tuple(
            reading(minute, address=f"{index:04}", external_id=index + 1)
            for index in range(70) for minute in range(0, 1440, 15)
        )
        batch = prepare_batch(readings, scope, START, START + timedelta(days=1))
        self.assertEqual((len(batch.expected), batch.returned_count, batch.valid_count), (6720, 6720, 6720))
        self.assertEqual(batch.issues, ())
        self.assertEqual(batch.unexpected, ())

    def test_duplicate_and_conflict_classification(self) -> None:
        batch = prepare_batch((reading(), reading(), reading(15, "20"), reading(15, "21")), SCOPE, START, END)
        self.assertEqual(batch.duplicate_count, 2)
        self.assertEqual(batch.valid_count, 1)
        self.assertEqual(batch.samples[1].quality, "conflict")
        self.assertIsNone(batch.samples[1].value)
        self.assertEqual(len(json.loads(batch.samples[1].raw_json)["records"]), 2)

    def test_identity_mismatch_does_not_bind_by_address_alone(self) -> None:
        batch = prepare_batch((reading(external_id=99),), SCOPE, START, END)
        self.assertIn((("0001", START), "identity_mismatch"), batch.issues)
        self.assertEqual(batch.samples[0].external_id, 1)
        self.assertEqual(batch.samples[0].quality, "conflict")
        self.assertEqual(json.loads(batch.samples[0].raw_json)["records"][0]["AmmeterID"], 99)

    def test_invalid_values_remain_null_and_valid_zero_is_preserved(self) -> None:
        batch = prepare_batch((reading(0, None), reading(15, "0")), SCOPE, START, END)
        self.assertEqual(batch.samples[0].quality, "invalid")
        self.assertIsNone(batch.samples[0].value)
        self.assertEqual(batch.samples[1].value, Decimal("0"))
        self.assertEqual(batch.samples[1].quality, "valid")

    def test_unexpected_and_end_boundary_are_not_persistable_samples(self) -> None:
        batch = prepare_batch((reading(60), reading(address="unmapped", external_id=2)), SCOPE, START, END)
        self.assertEqual(len(batch.samples), 0)
        self.assertEqual(len(batch.unexpected), 2)
        self.assertEqual(len(batch.issues), 4)

    def test_nonfinite_raw_value_can_be_audited_as_invalid(self) -> None:
        body = '{"IsSuccess":true,"Code":"0000","Data":[{"Address":"0001","AmmeterID":1,"ReadingDate":"2026-10-01 04:00","ZYGDNSZ":NaN}],"Count":1}'
        readings = parse_readings(body, timezone.utc)
        batch = prepare_batch(readings, SCOPE, START, END)
        self.assertEqual(batch.samples[0].quality, "invalid")
        self.assertEqual(
            json.loads(batch.samples[0].raw_json)["records"][0]["ZYGDNSZ"],
            {"non_finite_number": "nan"},
        )

    def test_invalid_scope_and_windows_rejected(self) -> None:
        cases = (
            (), (ScopeMeter("0001", 1), ScopeMeter("0001", 2)),
            (ScopeMeter("0001", 1), ScopeMeter("0002", 1)),
            (ScopeMeter("bad address", 1),), (ScopeMeter("unicode-\u00e9", 1),),
            (ScopeMeter("0001", True),), (ScopeMeter("0001", 1, END, START),),
            (ScopeMeter("0001", 1, END),),
        )
        for scope in cases:
            with self.subTest(scope=scope), self.assertRaises(ContractError):
                prepare_batch((), scope, START, END)
        for start, end in ((START, START), (END, START), (START.replace(tzinfo=None), END),
                           (START, START + timedelta(days=2)), (START + timedelta(minutes=1), END)):
            with self.subTest(start=start, end=end), self.assertRaises(ContractError):
                prepare_batch((), SCOPE, start, end)


class SQLiteCursor:
    """Adapts only locking, bind syntax, datetime and Decimal for behavioral tests."""

    def __init__(self, connection: "SQLiteConnection") -> None:
        self.connection = connection
        self.cursor = connection.db.cursor()

    @property
    def rowcount(self) -> int:
        return self.cursor.rowcount

    @property
    def lastrowid(self) -> int | None:
        return self.cursor.lastrowid

    def execute(self, operation: str, params: Parameters = ()) -> object:
        self.connection.statements.append(operation)
        if self.connection.fail_on is not None and self.connection.fail_on in operation:
            raise sqlite3.OperationalError("Injected database failure")
        if operation.startswith("UPDATE myems_system_db.tbl_jishiyun_runs") and self.connection.expire_on_finish:
            self.connection.now = NOW + timedelta(hours=2)
        converted = tuple(
            value.isoformat(" ") if isinstance(value, datetime)
            else str(value) if isinstance(value, Decimal) else value
            for value in params
        )
        return self.cursor.execute(operation.replace(" FOR UPDATE", "").replace("%s", "?"), converted)

    def _row(self, row: tuple[object, ...] | None) -> tuple[object, ...] | None:
        if row is None:
            return None
        return tuple(
            datetime.fromisoformat(value) if isinstance(value, str) and (
                self.cursor.description[index][0].endswith("_utc")
                or self.cursor.description[index][0] == "UTC_TIMESTAMP()"
            ) else value
            for index, value in enumerate(row)
        )

    def fetchone(self) -> tuple[object, ...] | None:
        return self._row(self.cursor.fetchone())

    def fetchall(self) -> list[tuple[object, ...]]:
        return [cast(tuple[object, ...], self._row(row)) for row in self.cursor.fetchall()]

    def close(self) -> None:
        self.cursor.close()


class SQLiteConnection:
    def __init__(self) -> None:
        self.db = sqlite3.connect(":memory:")
        self.now = NOW
        self.statements: list[str] = []
        self.fail_on: str | None = None
        self.expire_on_finish = False
        self.commits = 0
        self.rollbacks = 0
        self.db.create_function("UTC_TIMESTAMP", 0, lambda: self.now.isoformat(" "))
        self.db.executescript("""
            ATTACH DATABASE ':memory:' AS myems_system_db;
            ATTACH DATABASE ':memory:' AS myems_historical_db;
            ATTACH DATABASE ':memory:' AS information_schema;
            CREATE TABLE information_schema.TABLES (TABLE_SCHEMA TEXT, TABLE_NAME TEXT, ENGINE TEXT);
            INSERT INTO information_schema.TABLES VALUES
                ('myems_system_db', 'tbl_data_sources', 'InnoDB'),
                ('myems_system_db', 'tbl_jishiyun_runs', 'InnoDB'),
                ('myems_system_db', 'tbl_jishiyun_alerts', 'InnoDB'),
                ('myems_system_db', 'tbl_jishiyun_events', 'InnoDB'),
                ('myems_historical_db', 'tbl_jishiyun_energy_readings', 'InnoDB');
            CREATE TABLE myems_system_db.tbl_data_sources (id INTEGER PRIMARY KEY);
            INSERT INTO myems_system_db.tbl_data_sources VALUES (1);
            CREATE TABLE myems_system_db.tbl_jishiyun_runs (
                id INTEGER PRIMARY KEY, data_source_id INTEGER, status TEXT, lease_token TEXT,
                lease_expires_datetime_utc TEXT, window_start_utc TEXT, window_end_utc TEXT,
                scope_snapshot_json TEXT, parameters_json TEXT, expected_count INTEGER, returned_count INTEGER,
                valid_count INTEGER, inserted_count INTEGER, updated_count INTEGER, duplicate_count INTEGER,
                invalid_count INTEGER, missing_count INTEGER, unexpected_count INTEGER,
                finished_datetime_utc TEXT, error_code TEXT, error_message TEXT
            );
            CREATE TABLE myems_historical_db.tbl_jishiyun_energy_readings (
                id INTEGER PRIMARY KEY AUTOINCREMENT, data_source_id INTEGER, external_address TEXT,
                reading_datetime_utc TEXT, date_type TEXT, value_type TEXT, external_id INTEGER,
                forward_active_register TEXT, quality TEXT, raw_record_json TEXT,
                first_run_id INTEGER, last_run_id INTEGER, revision INTEGER NOT NULL DEFAULT 1,
                UNIQUE (data_source_id, external_address, reading_datetime_utc, date_type, value_type)
            );
            CREATE TABLE myems_system_db.tbl_jishiyun_alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT, data_source_id INTEGER, external_address TEXT,
                reading_datetime_utc TEXT, alert_type TEXT, status TEXT NOT NULL DEFAULT 'active',
                first_run_id INTEGER, last_run_id INTEGER, first_seen_datetime_utc TEXT,
                last_seen_datetime_utc TEXT, recovered_datetime_utc TEXT, acknowledged_by_user_id INTEGER,
                acknowledged_datetime_utc TEXT,
                UNIQUE (data_source_id, external_address, reading_datetime_utc, alert_type)
            );
            CREATE TABLE myems_system_db.tbl_jishiyun_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, data_source_id INTEGER, run_id INTEGER,
                alert_id INTEGER, event_type TEXT, details_json TEXT
            );
        """)

    def start_transaction(self) -> None:
        self.db.execute("BEGIN")

    def cursor(self) -> SQLiteCursor:
        return SQLiteCursor(self)

    def commit(self) -> None:
        self.db.commit()
        self.commits += 1

    def rollback(self) -> None:
        self.db.rollback()
        self.rollbacks += 1


class StoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = SQLiteConnection()
        self.addCleanup(self.connection.db.close)
        self.store = ResultStore(self.connection)

    def run_record(self, run_id: int, scope: tuple[ScopeMeter, ...] = SCOPE) -> None:
        self.connection.db.execute(
            "INSERT INTO myems_system_db.tbl_jishiyun_runs "
            "(id, data_source_id, status, lease_token, lease_expires_datetime_utc, "
            "window_start_utc, window_end_utc, scope_snapshot_json, parameters_json) "
            "VALUES (?, 1, 'running', ?, ?, ?, ?, ?, ?)",
            (
                run_id, TOKEN, (NOW + timedelta(hours=1)).isoformat(" "),
                START.replace(tzinfo=None).isoformat(" "), END.replace(tzinfo=None).isoformat(" "),
                scope_snapshot(scope), '{"delay_minutes":40}',
            ),
        )
        self.connection.db.commit()

    def rows(self, table: str, columns: str = "*") -> list[tuple[object, ...]]:
        return self.connection.db.execute(f"SELECT {columns} FROM {table} ORDER BY id").fetchall()

    def test_atomic_complete_result_and_only_namespaced_writes(self) -> None:
        self.run_record(1)
        result = self.store.save(1, TOKEN, complete())
        self.assertEqual((result.status, result.expected_count, result.inserted_count, result.valid_count), ("done", 4, 4, 4))
        self.assertEqual(self.connection.commits, 1)
        self.assertEqual(self.rows("myems_system_db.tbl_jishiyun_runs", "status, lease_token"), [("done", None)])
        self.assertEqual(len(self.rows("myems_historical_db.tbl_jishiyun_energy_readings")), 4)
        self.assertEqual(self.rows("myems_system_db.tbl_jishiyun_alerts"), [])
        writes = [sql for sql in self.connection.statements if sql.startswith(("INSERT", "UPDATE", "DELETE"))]
        self.assertTrue(all("tbl_jishiyun_" in sql for sql in writes))

    def test_partial_33_device_run_persists_16_alerts(self) -> None:
        scope = tuple(ScopeMeter(f"{index:04}", index + 1) for index in range(33))
        self.run_record(1, scope)
        readings = tuple(
            reading(minute, address=f"{index:04}", external_id=index + 1)
            for index in range(29) for minute in (0, 15, 30, 45)
        )
        result = self.store.save(1, TOKEN, readings)
        self.assertEqual((result.status, result.expected_count, result.returned_count, result.missing_count), ("partial", 132, 116, 16))
        self.assertEqual(len(self.rows("myems_system_db.tbl_jishiyun_alerts")), 16)

    def test_70_device_hour_persists_exactly_280_samples(self) -> None:
        scope = tuple(ScopeMeter(f"{index:04}", index + 1) for index in range(70))
        self.run_record(1, scope)
        readings = tuple(
            reading(minute, address=f"{index:04}", external_id=index + 1)
            for index in range(70) for minute in (0, 15, 30, 45)
        )
        result = self.store.save(1, TOKEN, readings)
        self.assertEqual((result.status, result.inserted_count, result.expected_count), ("done", 280, 280))
        self.assertEqual(len(self.rows("myems_historical_db.tbl_jishiyun_energy_readings")), 280)

    def test_replay_keeps_rows_and_revision_but_updates_provenance(self) -> None:
        self.run_record(1)
        self.store.save(1, TOKEN, complete())
        self.run_record(2)
        result = self.store.save(2, TOKEN, complete())
        self.assertEqual((result.inserted_count, result.updated_count, result.duplicate_count), (0, 0, 4))
        self.assertEqual(
            self.rows("myems_historical_db.tbl_jishiyun_energy_readings", "first_run_id, last_run_id, revision"),
            [(1, 2, 1)] * 4,
        )

    def test_corrected_values_increment_revision_and_audit_before_after(self) -> None:
        self.run_record(1)
        self.store.save(1, TOKEN, complete())
        self.run_record(2)
        result = self.store.save(2, TOKEN, complete("10.123456"))
        self.assertEqual(result.updated_count, 4)
        self.assertEqual(
            self.rows("myems_historical_db.tbl_jishiyun_energy_readings", "forward_active_register, revision"),
            [("10.123456", 2)] * 4,
        )
        events = self.connection.db.execute(
            "SELECT details_json FROM myems_system_db.tbl_jishiyun_events WHERE event_type = 'raw_sample_revised'"
        ).fetchall()
        self.assertEqual(len(events), 4)
        details = json.loads(events[0][0])
        self.assertEqual(details["previous"]["value"], "10")
        self.assertEqual(details["current"]["value"], "10.123456")
        self.assertTrue(details["requires_business_recalculation"])

    def test_repeated_missing_does_not_duplicate_alerts_or_clear_ack(self) -> None:
        self.run_record(1)
        self.store.save(1, TOKEN, ())
        self.connection.db.execute(
            "UPDATE myems_system_db.tbl_jishiyun_alerts SET acknowledged_by_user_id = 7, acknowledged_datetime_utc = ?",
            (NOW.isoformat(" "),),
        )
        self.connection.db.commit()
        self.run_record(2)
        result = self.store.save(2, TOKEN, ())
        self.assertEqual(result.missing_count, 4)
        alerts = self.rows("myems_system_db.tbl_jishiyun_alerts", "first_run_id, last_run_id, status, acknowledged_by_user_id")
        self.assertEqual(alerts, [(1, 2, "active", 7)] * 4)

    def test_recovery_requires_valid_return_and_reopening_resets_ack(self) -> None:
        self.run_record(1)
        self.store.save(1, TOKEN, ())
        self.run_record(2)
        self.store.save(2, TOKEN, complete())
        self.assertEqual(self.rows("myems_system_db.tbl_jishiyun_alerts", "status"), [("recovered",)] * 4)
        self.connection.db.execute(
            "UPDATE myems_system_db.tbl_jishiyun_alerts SET acknowledged_by_user_id = 7, acknowledged_datetime_utc = ?",
            (NOW.isoformat(" "),),
        )
        self.connection.db.commit()
        self.run_record(3)
        self.store.save(3, TOKEN, ())
        alerts = self.rows(
            "myems_system_db.tbl_jishiyun_alerts",
            "status, first_run_id, last_run_id, recovered_datetime_utc, acknowledged_by_user_id",
        )
        self.assertEqual(alerts, [("active", 1, 3, None, None)] * 4)
        self.assertEqual(len(self.rows("myems_historical_db.tbl_jishiyun_energy_readings")), 4)
        events = self.connection.db.execute(
            "SELECT alert_id, details_json FROM myems_system_db.tbl_jishiyun_events WHERE event_type = 'alert_reopened'"
        ).fetchall()
        self.assertEqual(len(events), 4)
        self.assertTrue(all(row[0] is not None for row in events))
        self.assertEqual(json.loads(events[0][1])["previous_acknowledged_by_user_id"], 7)

    def test_invalid_does_not_recover_missing_and_valid_recovers_both(self) -> None:
        for run_id, readings in ((1, ()), (2, (reading(value=None),)), (3, complete())):
            self.run_record(run_id)
            self.store.save(run_id, TOKEN, readings)
            if run_id == 2:
                self.assertEqual(
                    len([row for row in self.rows("myems_system_db.tbl_jishiyun_alerts", "status") if row == ("active",)]), 5,
                )
        self.assertEqual(self.rows("myems_system_db.tbl_jishiyun_alerts", "status"), [("recovered",)] * 5)

    def test_raw_coverage_does_not_recover_hourly_boundary_alert(self) -> None:
        self.run_record(1)
        self.connection.db.execute(
            "INSERT INTO myems_system_db.tbl_jishiyun_alerts "
            "(data_source_id, external_address, reading_datetime_utc, alert_type, first_run_id, last_run_id) "
            "VALUES (1, '0001', ?, 'boundary_missing', 1, 1)",
            (START.replace(tzinfo=None).isoformat(" "),),
        )
        self.connection.db.commit()
        self.assertEqual(self.store.save(1, TOKEN, complete()).status, "done")
        self.assertEqual(
            self.rows("myems_system_db.tbl_jishiyun_alerts", "alert_type, status"),
            [("boundary_missing", "active")],
        )

    def test_conflict_and_identity_mismatch_quarantined(self) -> None:
        self.run_record(1)
        result = self.store.save(1, TOKEN, (
            reading(0, "10"), reading(0, "11"), reading(15, external_id=99),
            reading(30, "0"), reading(45, "0"),
        ))
        self.assertEqual((result.status, result.valid_count, result.invalid_count, result.duplicate_count), ("partial", 2, 2, 1))
        self.assertEqual(
            self.rows("myems_historical_db.tbl_jishiyun_energy_readings", "quality, forward_active_register"),
            [("conflict", None), ("conflict", None), ("valid", "0"), ("valid", "0")],
        )
        self.assertEqual(
            self.rows("myems_system_db.tbl_jishiyun_alerts", "alert_type"),
            [("conflict",), ("identity_mismatch",)],
        )

    def test_unexpected_records_audited_without_mutating_other_slots(self) -> None:
        self.run_record(1)
        result = self.store.save(1, TOKEN, complete() + (reading(60), reading(address="unmapped", external_id=2)))
        self.assertEqual((result.status, result.valid_count, result.unexpected_count), ("partial", 4, 2))
        self.assertEqual(len(self.rows("myems_historical_db.tbl_jishiyun_energy_readings")), 4)
        event = self.connection.db.execute(
            "SELECT details_json FROM myems_system_db.tbl_jishiyun_events WHERE event_type = 'unexpected_readings'"
        ).fetchone()
        self.assertEqual(len(json.loads(event[0])["records"]), 2)

    def test_wrong_token_expired_lease_or_completed_run_cannot_write(self) -> None:
        self.run_record(1)
        with self.assertRaisesRegex(PersistenceError, "matching lease"):
            self.store.save(1, OTHER_TOKEN, complete())
        self.connection.now = NOW + timedelta(hours=2)
        with self.assertRaisesRegex(PersistenceError, "matching lease"):
            self.store.save(1, TOKEN, complete())
        self.assertEqual(self.rows("myems_historical_db.tbl_jishiyun_energy_readings"), [])
        self.connection.now = NOW
        self.store.save(1, TOKEN, complete())
        with self.assertRaises(PersistenceError):
            self.store.save(1, TOKEN, complete())
        self.assertEqual(len(self.rows("myems_historical_db.tbl_jishiyun_energy_readings")), 4)

    def test_lease_expiring_during_write_rolls_back_everything(self) -> None:
        self.run_record(1)
        self.connection.expire_on_finish = True
        with self.assertRaisesRegex(PersistenceError, "expired before commit"):
            self.store.save(1, TOKEN, (reading(),))
        self.assertEqual(self.rows("myems_historical_db.tbl_jishiyun_energy_readings"), [])
        self.assertEqual(self.rows("myems_system_db.tbl_jishiyun_alerts"), [])
        self.assertEqual(self.rows("myems_system_db.tbl_jishiyun_events"), [])
        self.assertEqual(self.rows("myems_system_db.tbl_jishiyun_runs", "status"), [("running",)])

    def test_not_yet_due_window_cannot_raise_missing_alerts(self) -> None:
        self.run_record(1)
        self.connection.now = END.replace(tzinfo=None) + timedelta(minutes=39, seconds=59)
        with self.assertRaisesRegex(PersistenceError, "not due"):
            self.store.save(1, TOKEN, ())
        self.assertEqual(self.rows("myems_system_db.tbl_jishiyun_alerts"), [])
        self.connection.now = END.replace(tzinfo=None) + timedelta(minutes=40)
        self.assertEqual(self.store.save(1, TOKEN, ()).missing_count, 4)

    def test_missing_or_invalid_delay_snapshot_does_not_default_to_zero(self) -> None:
        self.run_record(1)
        for parameters in ("{}", "[]", '{"delay_minutes":true}', '{"delay_minutes":-1}'):
            self.connection.db.execute(
                "UPDATE myems_system_db.tbl_jishiyun_runs SET parameters_json = ? WHERE id = 1", (parameters,),
            )
            self.connection.db.commit()
            with self.subTest(parameters=parameters), self.assertRaises(PersistenceError):
                self.store.save(1, TOKEN, ())
        self.assertEqual(self.rows("myems_system_db.tbl_jishiyun_alerts"), [])

    def test_audit_failure_rolls_back_raw_and_alerts(self) -> None:
        self.run_record(1)
        self.connection.fail_on = "INSERT INTO myems_system_db.tbl_jishiyun_events"
        with self.assertRaises(sqlite3.OperationalError):
            self.store.save(1, TOKEN, (reading(),))
        self.assertEqual(self.connection.rollbacks, 1)
        self.assertEqual(self.rows("myems_historical_db.tbl_jishiyun_energy_readings"), [])
        self.assertEqual(self.rows("myems_system_db.tbl_jishiyun_alerts"), [])
        self.assertEqual(self.rows("myems_system_db.tbl_jishiyun_runs", "status"), [("running",)])

    def test_raw_failure_does_not_report_success(self) -> None:
        self.run_record(1)
        self.connection.fail_on = "INSERT INTO myems_historical_db"
        with self.assertRaises(sqlite3.OperationalError):
            self.store.save(1, TOKEN, complete())
        self.assertEqual(self.connection.commits, 0)
        self.assertEqual(self.rows("myems_system_db.tbl_jishiyun_events"), [])

    def test_older_run_cannot_overwrite_newer_values_or_create_false_gaps(self) -> None:
        self.run_record(1)
        self.run_record(2)
        self.store.save(2, TOKEN, complete("20"))
        for readings in (complete("10"), ()):
            with self.subTest(readings=len(readings)), self.assertRaisesRegex(PersistenceError, "older run"):
                self.store.save(1, TOKEN, readings)
        self.assertEqual(self.rows("myems_system_db.tbl_jishiyun_alerts"), [])
        self.assertEqual(
            self.rows("myems_historical_db.tbl_jishiyun_energy_readings", "forward_active_register, last_run_id"),
            [("20", 2)] * 4,
        )

    def test_older_missing_run_cannot_change_newer_missing_alerts(self) -> None:
        self.run_record(1)
        self.run_record(2)
        self.store.save(2, TOKEN, ())
        with self.assertRaisesRegex(PersistenceError, "older run"):
            self.store.save(1, TOKEN, ())
        self.assertEqual(self.rows("myems_system_db.tbl_jishiyun_alerts", "last_run_id"), [(2,)] * 4)

    def test_corrupt_snapshot_fails_without_writes(self) -> None:
        self.run_record(1)
        cases = (
            "invalid json", "[]", '{"version":2,"meters":[]}', '{"version":true,"meters":[]}',
            '{"version":1,"meters":[{"address":"0001"}]}',
            '{"version":1,"meters":[{"address":"0001","external_id":1,'
            '"effective_start_utc":"2026-10-01T04:00:00","effective_end_utc":null}]}',
        )
        for snapshot in cases:
            self.connection.db.execute(
                "UPDATE myems_system_db.tbl_jishiyun_runs SET scope_snapshot_json = ? WHERE id = 1",
                (snapshot,),
            )
            self.connection.db.commit()
            with self.subTest(snapshot=snapshot), self.assertRaises((ContractError, PersistenceError)):
                self.store.save(1, TOKEN, complete())
        self.assertEqual(self.rows("myems_historical_db.tbl_jishiyun_energy_readings"), [])

    def test_cursor_creation_failure_rolls_back_transaction(self) -> None:
        self.run_record(1)
        with patch.object(self.connection, "cursor", side_effect=sqlite3.OperationalError("Synthetic failure")):
            with self.assertRaises(sqlite3.OperationalError):
                self.store.save(1, TOKEN, complete())
        self.assertEqual(self.connection.rollbacks, 1)
        self.assertFalse(self.connection.db.in_transaction)

    def test_nontransactional_or_missing_table_is_rejected_before_writes(self) -> None:
        self.run_record(1)
        self.connection.db.execute(
            "UPDATE information_schema.TABLES SET ENGINE = 'MyISAM' WHERE TABLE_NAME = 'tbl_data_sources'"
        )
        self.connection.db.commit()
        with self.assertRaisesRegex(PersistenceError, "InnoDB"):
            self.store.save(1, TOKEN, complete())
        self.connection.db.execute("DELETE FROM information_schema.TABLES WHERE TABLE_NAME = 'tbl_data_sources'")
        self.connection.db.commit()
        with self.assertRaisesRegex(PersistenceError, "InnoDB"):
            self.store.save(1, TOKEN, complete())
        self.assertEqual(self.rows("myems_historical_db.tbl_jishiyun_energy_readings"), [])


if __name__ == "__main__":
    unittest.main()
