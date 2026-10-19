"""Business-hour rules and transaction behavior; SQLite is not MySQL lock evidence."""

import io
import json
import sqlite3
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from myems_jishiyun.normalization import BusinessGateError, Register, calculate_hour, normalize_hour
from myems_jishiyun.persistence import ScopeMeter, scope_snapshot
from myems_jishiyun.worker import main
from myems_jishiyun.business import process_business, _finish
from myems_jishiyun.settlement import settle_run
from myems_jishiyun.correction import correct_run
from myems_jishiyun.dimensions import aggregate_dimensions
from myems_jishiyun.processing import reading_processing
from myems_jishiyun.persistence import PersistenceError
from test_initialization import InitializationFixture
from test_persistence import START, END


def registers(values=("10", "11", "12", "13", "14")):
    return tuple(Register(START + timedelta(minutes=15 * index), Decimal(value), 1)
                 for index, value in enumerate(values))


class HourRuleTests(unittest.TestCase):
    def test_exact_decimal_difference_not_sum(self):
        self.assertEqual(calculate_hour(START, registers(), Decimal(0), Decimal(100), Decimal(999)), Decimal(4))
        values = registers(("10.123456", "10.123457", "10.123458", "10.123459", "10.123460"))
        self.assertEqual(calculate_hour(START, values, Decimal(0), Decimal(1), Decimal(999)), Decimal("0.000004"))

    def test_unchanged_register_is_genuine_zero(self):
        self.assertEqual(calculate_hour(
            START, registers(("10",) * 5), Decimal(0), Decimal(100), Decimal(999),
        ), 0)

    def test_missing_interior_or_end_duplicate_and_misaligned_rejected(self):
        full = registers()
        cases = (full[:-1], full[:2] + full[3:], full + full[-1:], full[::-1])
        for rows in cases:
            with self.subTest(rows=rows), self.assertRaises(BusinessGateError):
                calculate_hour(START, rows, Decimal(0), Decimal(100), Decimal(999))
        with self.assertRaises(BusinessGateError):
            calculate_hour(START.replace(tzinfo=None), full, Decimal(0), Decimal(100), Decimal(999))

    def test_reset_even_when_final_register_is_higher_rejected(self):
        with self.assertRaisesRegex(BusinessGateError, "decrease"):
            calculate_hour(START, registers(("10", "11", "1", "13", "14")), Decimal(0), Decimal(100), Decimal(999))

    def test_limits_nonfinite_negative_and_excess_precision_rejected(self):
        cases = (
            (registers(), Decimal(5), Decimal(100), Decimal(999)),
            (registers(), Decimal(0), Decimal(3), Decimal(999)),
            (registers(), Decimal(0), Decimal(100), Decimal(13)),
            (registers(("10", "11", "12", "13", "NaN")), Decimal(0), Decimal(100), Decimal(999)),
            (registers(("-1", "0", "1", "2", "3")), Decimal(0), Decimal(100), Decimal(999)),
            (registers(("10.0000001", "11", "12", "13", "14")), Decimal(0), Decimal(100), Decimal(999)),
        )
        for rows, low, high, ceiling in cases:
            with self.subTest(rows=rows, low=low, high=high), self.assertRaises(BusinessGateError):
                calculate_hour(START, rows, low, high, ceiling)


class BusinessHourTests(InitializationFixture):
    def setUp(self):
        super().setUp()
        self.approved()
        self.service.initialize(10, "0001", 7)
        self.connection.db.executescript("""
            ATTACH DATABASE ':memory:' AS myems_energy_db;
            CREATE TABLE myems_historical_db.tbl_energy_value (
                id INTEGER PRIMARY KEY AUTOINCREMENT, point_id INTEGER, utc_date_time TEXT,
                actual_value TEXT, is_bad INTEGER, is_published INTEGER
            );
            CREATE TABLE myems_energy_db.tbl_meter_hourly (
                id INTEGER PRIMARY KEY AUTOINCREMENT, meter_id INTEGER,
                start_datetime_utc TEXT, actual_value TEXT
            );
            ATTACH DATABASE ':memory:' AS myems_billing_db;
            ATTACH DATABASE ':memory:' AS myems_carbon_db;
            CREATE TABLE myems_billing_db.tbl_meter_hourly (
                id INTEGER PRIMARY KEY, meter_id INTEGER, start_datetime_utc TEXT
            );
            CREATE TABLE myems_carbon_db.tbl_meter_hourly (
                id INTEGER PRIMARY KEY, meter_id INTEGER, start_datetime_utc TEXT
            );
            INSERT INTO information_schema.TABLES VALUES
                ('myems_historical_db','tbl_energy_value','InnoDB'),
                ('myems_energy_db','tbl_meter_hourly','InnoDB'),
                ('myems_billing_db','tbl_meter_hourly','InnoDB'),
                ('myems_carbon_db','tbl_meter_hourly','InnoDB');
            CREATE TABLE myems_system_db.tbl_jishiyun_business_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT, data_source_id INTEGER NOT NULL,
                run_id INTEGER UNIQUE NOT NULL, actor_user_id INTEGER NOT NULL,
                status TEXT DEFAULT 'queued', attempt_count INTEGER DEFAULT 0, gate_code TEXT,
                lease_token TEXT, lease_expires_datetime_utc TEXT, next_attempt_datetime_utc TEXT,
                register_insert_count INTEGER DEFAULT 0, hour_insert_count INTEGER DEFAULT 0
            );
            INSERT INTO information_schema.TABLES VALUES
                ('myems_system_db','tbl_jishiyun_business_jobs','InnoDB');
        """)
        self.connection.db.execute(
            "UPDATE myems_system_db.tbl_jishiyun_meter_mappings "
            "SET is_enabled=1,configuration_status='ready',collection_start_utc=?",
            (START.replace(tzinfo=None).isoformat(" "),),
        )
        snapshot = scope_snapshot((ScopeMeter("0001", 1, START),))
        self.connection.db.execute(
            "INSERT INTO myems_system_db.tbl_jishiyun_runs "
            "(id,task_id,data_source_id,status,window_start_utc,window_end_utc,scope_snapshot_json,"
            "expected_count,valid_count,missing_count,invalid_count,unexpected_count) "
            "VALUES (20,1,1,'done',?,?,?,4,4,0,0,0)",
            (START.replace(tzinfo=None).isoformat(" "), END.replace(tzinfo=None).isoformat(" "), snapshot),
        )
        for register in registers():
            self.connection.db.execute(
                "INSERT INTO myems_historical_db.tbl_jishiyun_energy_readings "
                "(data_source_id,external_address,external_id,reading_datetime_utc,date_type,value_type,"
                "forward_active_register,quality,raw_record_json,first_run_id,last_run_id) "
                "VALUES (1,'0001',1,?,'mi15','SJZ',?,'valid','{}',20,20)",
                (register.timestamp.replace(tzinfo=None).isoformat(" "), str(register.value)),
            )
        self.connection.db.commit()

    def process(self, apply=False):
        return normalize_hour(self.queue, 20, 7, confirm_timestamp_kwh=True, apply=apply)

    def counts(self):
        return tuple(self.connection.db.execute("SELECT COUNT(*) FROM " + table).fetchone()[0] for table in (
            "myems_historical_db.tbl_energy_value", "myems_energy_db.tbl_meter_hourly",
        ))

    def change(self, sql):
        self.connection.db.execute(sql)
        self.connection.db.commit()

    def test_preview_does_not_write_and_apply_is_idempotent(self):
        result = self.process()
        self.assertEqual((result["status"], result["register_insert_count"], result["hour_insert_count"]),
                         ("preview", 5, 1))
        self.assertEqual(self.counts(), (0, 0))
        first = self.process(apply=True)
        self.assertEqual(first["meters"][0]["consumption_kwh"], "4.000000")
        self.assertEqual(self.counts(), (5, 1))
        self.assertEqual(self.process(apply=True)["status"], "already_committed")
        self.assertEqual(self.counts(), (5, 1))
        events = self.connection.db.execute(
            "SELECT actor_user_id FROM myems_system_db.tbl_jishiyun_events "
            "WHERE event_type='business_hour_committed'",
        ).fetchall()
        self.assertEqual(events, [(7,)])
        self.assertEqual(self.connection.db.execute(
            "SELECT object_type FROM myems_system_db.tbl_points",
        ).fetchone()[0], "JISHIYUN_REGISTER")

    def test_unconfirmed_semantics_rejected(self):
        with self.assertRaises(BusinessGateError):
            normalize_hour(self.queue, 20, 7, confirm_timestamp_kwh=False, apply=True)
        self.assertEqual(self.counts(), (0, 0))

    def test_missing_boundary_is_not_filled_or_persisted(self):
        self.change("DELETE FROM myems_historical_db.tbl_jishiyun_energy_readings WHERE id=5")
        with self.assertRaisesRegex(BusinessGateError, "five_quarter"):
            self.process(apply=True)
        self.assertEqual(self.counts(), (0, 0))

    def test_invalid_quality_identity_paused_and_legacy_owned_points_rejected(self):
        changes = (
            ("UPDATE myems_historical_db.tbl_jishiyun_energy_readings SET quality='invalid' WHERE id=5",
             "UPDATE myems_historical_db.tbl_jishiyun_energy_readings SET quality='valid'"),
            ("UPDATE myems_historical_db.tbl_jishiyun_energy_readings SET external_id=2 WHERE id=5",
             "UPDATE myems_historical_db.tbl_jishiyun_energy_readings SET external_id=1"),
            ("UPDATE myems_system_db.tbl_jishiyun_meter_mappings SET is_enabled=0",
             "UPDATE myems_system_db.tbl_jishiyun_meter_mappings SET is_enabled=1"),
            ("UPDATE myems_system_db.tbl_points SET object_type='ENERGY_VALUE'",
             "UPDATE myems_system_db.tbl_points SET object_type='JISHIYUN_REGISTER'"),
            ("UPDATE myems_system_db.tbl_points SET units='Wh'",
             "UPDATE myems_system_db.tbl_points SET units='kWh'"),
            ("UPDATE myems_system_db.tbl_points SET ratio=2",
             "UPDATE myems_system_db.tbl_points SET ratio=1"),
            ("INSERT INTO myems_system_db.tbl_meters_points (meter_id,point_id) VALUES (2,1)",
             "DELETE FROM myems_system_db.tbl_meters_points WHERE meter_id=2"),
        )
        for sql, undo in changes:
            with self.subTest(sql=sql):
                self.change(sql)
                with self.assertRaises(BusinessGateError):
                    self.process(apply=True)
                self.assertEqual(self.counts(), (0, 0))
                self.change(undo)

    def test_partial_run_active_alert_or_nontransactional_target_rejected(self):
        changes = (
            ("UPDATE myems_system_db.tbl_jishiyun_runs SET status='partial' WHERE id=20",
             "UPDATE myems_system_db.tbl_jishiyun_runs SET status='done' WHERE id=20"),
            ("UPDATE myems_system_db.tbl_jishiyun_runs SET missing_count=1 WHERE id=20",
             "UPDATE myems_system_db.tbl_jishiyun_runs SET missing_count=0 WHERE id=20"),
            ("UPDATE information_schema.TABLES SET ENGINE='MyISAM' WHERE TABLE_SCHEMA='myems_energy_db'",
             "UPDATE information_schema.TABLES SET ENGINE='InnoDB' WHERE TABLE_SCHEMA='myems_energy_db'"),
            ("INSERT INTO myems_system_db.tbl_jishiyun_alerts "
             "(data_source_id,external_address,reading_datetime_utc,alert_type,status) "
             "VALUES (1,'0001','2026-10-01 05:00:00','missing','active')",
             "DELETE FROM myems_system_db.tbl_jishiyun_alerts"),
        )
        for sql, undo in changes:
            with self.subTest(sql=sql):
                self.change(sql)
                with self.assertRaises(BusinessGateError):
                    self.process(apply=True)
                self.assertEqual(self.counts(), (0, 0))
                self.change(undo)

    def test_changed_raw_revision_and_missing_committed_rows_require_review(self):
        self.process(apply=True)
        self.change("UPDATE myems_historical_db.tbl_jishiyun_energy_readings SET revision=2 WHERE id=5")
        with self.assertRaisesRegex(BusinessGateError, "historical_recalculation"):
            self.process(apply=True)
        self.change("UPDATE myems_historical_db.tbl_jishiyun_energy_readings SET revision=1")
        self.change("DELETE FROM myems_energy_db.tbl_meter_hourly")
        with self.assertRaisesRegex(BusinessGateError, "rows_missing"):
            self.process(apply=True)

    def test_duplicate_target_and_foreign_hour_are_not_adopted(self):
        for table, values in (
            ("myems_historical_db.tbl_energy_value", "(point_id,utc_date_time,actual_value,is_bad) "
             "VALUES (1,'2026-10-01 04:00:00','10',0)"),
            ("myems_energy_db.tbl_meter_hourly", "(meter_id,start_datetime_utc,actual_value) "
             "VALUES (1,'2026-10-01 04:00:00','4')"),
        ):
            with self.subTest(table=table):
                self.connection.db.execute("INSERT INTO " + table + " " + values)
                self.connection.db.execute("INSERT INTO " + table + " " + values)
                self.connection.db.commit()
                before = self.counts()
                with self.assertRaises(BusinessGateError):
                    self.process(apply=True)
                self.assertEqual(self.counts(), before)
                self.change("DELETE FROM " + table)

    def test_failure_at_hour_insert_rolls_back_all_registers_and_audit(self):
        self.connection.fail_on = "INSERT INTO myems_energy_db.tbl_meter_hourly"
        with self.assertRaises(sqlite3.OperationalError):
            self.process(apply=True)
        self.assertEqual(self.counts(), (0, 0))
        self.assertEqual(self.connection.db.execute(
            "SELECT COUNT(*) FROM myems_system_db.tbl_jishiyun_events WHERE event_type='business_hour_committed'",
        ).fetchone()[0], 0)

    def test_downstream_watermark_blocks_first_write_but_not_noop_replay(self):
        for schema in ("myems_billing_db", "myems_carbon_db"):
            with self.subTest(schema=schema):
                self.change("INSERT INTO " + schema + ".tbl_meter_hourly "
                            "VALUES (1,1,'2026-10-01 06:00:00')")
                with self.assertRaisesRegex(BusinessGateError, "downstream_already"):
                    self.process(apply=True)
                self.assertEqual(self.counts(), (0, 0))
                self.change("DELETE FROM " + schema + ".tbl_meter_hourly")
        self.process(apply=True)
        self.change("INSERT INTO myems_billing_db.tbl_meter_hourly VALUES (1,1,'2026-10-01 06:00:00')")
        self.assertEqual(self.process(apply=True)["status"], "already_committed")

    def test_single_unowned_hour_is_not_silently_adopted(self):
        self.change("INSERT INTO myems_energy_db.tbl_meter_hourly "
                    "(meter_id,start_datetime_utc,actual_value) VALUES (1,'2026-10-01 04:00:00','4')")
        with self.assertRaisesRegex(BusinessGateError, "same_run_proof"):
            self.process(apply=True)
        self.assertEqual(self.counts(), (0, 1))

    def test_cli_defaults_to_preview_and_reports_gate_explicitly(self):
        environment = {"JISHIYUN_DATA_SOURCE_ID": "1"}
        with patch.dict("os.environ", environment), patch(
            "myems_jishiyun.worker.database_factory", return_value=lambda: self.connection,
        ):
            output = io.StringIO()
            with redirect_stdout(output):
                code = main(["business-hour", "--run-id", "20", "--actor", "7", "--confirm-timestamp-kwh"])
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(output.getvalue())["status"], "preview")
            self.assertEqual(self.counts(), (0, 0))
            error = io.StringIO()
            with redirect_stderr(error):
                code = main(["business-hour", "--run-id", "20", "--actor", "7", "--apply"])
            self.assertEqual(code, 2)
            self.assertEqual(json.loads(error.getvalue())["status"], "blocked")

    def business(self):
        return process_business(self.queue, 7, confirm_timestamp_kwh=True)

    def job(self):
        return self.connection.db.execute(
            "SELECT status,attempt_count,gate_code,register_insert_count,hour_insert_count "
            "FROM myems_system_db.tbl_jishiyun_business_jobs",
        ).fetchone()

    def test_business_scheduler_discovers_once_commits_and_then_idles(self):
        result = self.business()
        self.assertEqual((result["status"], result["hour_insert_count"]), ("done", 1))
        self.assertEqual(self.job(), ("done", 1, None, 5, 1))
        self.assertIsNone(self.business())
        self.assertEqual(self.counts(), (5, 1))

    def test_business_gate_retry_waits_and_recovers_boundary_without_recollecting(self):
        self.change("UPDATE myems_historical_db.tbl_jishiyun_energy_readings SET quality='invalid' WHERE id=5")
        self.assertEqual(self.business()["status"], "blocked")
        self.assertEqual(self.counts(), (0, 0))
        self.assertIsNone(self.business())
        self.change("UPDATE myems_historical_db.tbl_jishiyun_energy_readings SET quality='valid'")
        self.connection.now += timedelta(minutes=5)
        self.assertEqual(self.business()["status"], "done")
        self.assertEqual(self.job()[1], 2)

    def test_business_lease_recovery_after_energy_commit_is_safe(self):
        self.process(apply=True)
        self.change("INSERT INTO myems_system_db.tbl_jishiyun_business_jobs "
                    "(data_source_id,run_id,actor_user_id,status,lease_token,lease_expires_datetime_utc) "
                    "VALUES (1,20,7,'running','abandoned','2026-10-01 05:00:00')")
        self.assertEqual(self.business()["status"], "done")
        self.assertEqual(self.job(), ("done", 1, None, 0, 0))
        self.assertEqual(self.counts(), (5, 1))

    def test_business_active_lease_and_stale_finisher_are_fenced(self):
        self.change("INSERT INTO myems_system_db.tbl_jishiyun_business_jobs "
                    "(data_source_id,run_id,actor_user_id,status,lease_token,lease_expires_datetime_utc) "
                    "VALUES (1,20,7,'running','current','2026-10-01 07:00:00')")
        self.assertIsNone(self.business())
        with self.assertRaisesRegex(PersistenceError, "lease"):
            _finish(self.queue, 1, "stale", "done", None, 0, 0)
        self.assertEqual(self.job()[0], "running")
        self.assertEqual(self.counts(), (0, 0))

    def test_business_discovery_skips_partial_and_other_sources(self):
        self.change("UPDATE myems_system_db.tbl_jishiyun_runs SET status='partial' WHERE id=20")
        self.assertIsNone(self.business())
        self.change("UPDATE myems_system_db.tbl_jishiyun_runs SET status='done',data_source_id=2 WHERE id=20")
        self.assertIsNone(self.business())
        self.assertIsNone(self.job())

    def test_business_multihour_run_requires_matching_complete_counts(self):
        self.change("UPDATE myems_system_db.tbl_jishiyun_runs "
                    "SET window_end_utc='2026-10-01 06:00:00' WHERE id=20")
        result = self.business()
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["gate_code"], "run_counts_do_not_prove_complete_scope")

    def extend_two_hours(self):
        self.change("UPDATE myems_system_db.tbl_jishiyun_runs SET "
                    "window_end_utc='2026-10-01 06:00:00',expected_count=8,valid_count=8 WHERE id=20")
        for index in range(5, 9):
            self.connection.db.execute(
                "INSERT INTO myems_historical_db.tbl_jishiyun_energy_readings "
                "(data_source_id,external_address,external_id,reading_datetime_utc,date_type,value_type,"
                "forward_active_register,quality,raw_record_json,first_run_id,last_run_id) "
                "VALUES (1,'0001',1,?,'mi15','SJZ',?,'valid','{}',20,20)",
                ((START + timedelta(minutes=15 * index)).replace(tzinfo=None).isoformat(" "), str(10 + index)),
            )
        self.connection.db.commit()

    def copy_run(self):
        self.change("INSERT INTO myems_system_db.tbl_jishiyun_runs "
                    "(id,task_id,data_source_id,status,window_start_utc,window_end_utc,scope_snapshot_json,"
                    "expected_count,valid_count,missing_count,invalid_count,unexpected_count) "
                    "SELECT 21,task_id,data_source_id,status,window_start_utc,window_end_utc,scope_snapshot_json,"
                    "expected_count,valid_count,missing_count,invalid_count,unexpected_count "
                    "FROM myems_system_db.tbl_jishiyun_runs WHERE id=20")

    def test_multihour_shared_boundary_deduplicates_and_replays(self):
        self.extend_two_hours()
        result = self.process(apply=True)
        self.assertEqual((result["version"], result["register_insert_count"], result["hour_insert_count"]), (2, 9, 2))
        self.assertEqual(len(result["hours"]), 2)
        self.assertEqual(self.counts(), (9, 2))
        self.assertEqual(self.process(apply=True)["status"], "already_committed")

    def test_full_day_crossing_midnight_uses_97_registers_for_24_hours(self):
        self.extend_two_hours()
        for index in range(9, 97):
            self.connection.db.execute(
                "INSERT INTO myems_historical_db.tbl_jishiyun_energy_readings "
                "(data_source_id,external_address,external_id,reading_datetime_utc,date_type,value_type,"
                "forward_active_register,quality,raw_record_json,first_run_id,last_run_id) "
                "VALUES (1,'0001',1,?,'mi15','SJZ',?,'valid','{}',20,20)",
                ((START + timedelta(minutes=15 * index)).replace(tzinfo=None).isoformat(" "), str(10 + index)),
            )
        self.connection.db.commit()
        self.change("UPDATE myems_system_db.tbl_jishiyun_runs SET "
                    "window_end_utc='2026-10-02 04:00:00',expected_count=96,valid_count=96 WHERE id=20")
        result = self.process(apply=True)
        self.assertEqual((result["register_insert_count"], result["hour_insert_count"]), (97, 24))
        self.assertEqual(self.counts(), (97, 24))
        self.assertEqual(len(result["hours"]), 24)
        self.assertEqual(self.process(apply=True)["status"], "already_committed")

    def test_over_one_day_or_non_hour_end_is_rejected(self):
        for end in ("2026-10-02 05:00:00", "2026-10-01 05:15:00"):
            self.change("UPDATE myems_system_db.tbl_jishiyun_runs SET window_end_utc='" + end + "' WHERE id=20")
            with self.assertRaisesRegex(BusinessGateError, "aligned_whole_hour"):
                self.process(apply=True)
            self.assertEqual(self.counts(), (0, 0))

    def test_multihour_missing_last_boundary_and_second_hour_error_write_nothing(self):
        self.extend_two_hours()
        self.change("UPDATE myems_historical_db.tbl_jishiyun_energy_readings SET quality='invalid' WHERE id=9")
        with self.assertRaises(BusinessGateError):
            self.process(apply=True)
        self.assertEqual(self.counts(), (0, 0))
        self.change("UPDATE myems_historical_db.tbl_jishiyun_energy_readings "
                    "SET quality='valid',forward_active_register='1' WHERE id=9")
        with self.assertRaisesRegex(BusinessGateError, "decrease"):
            self.process(apply=True)
        self.assertEqual(self.counts(), (0, 0))

    def test_cross_run_reuses_only_identical_audited_hour(self):
        self.process(apply=True)
        self.copy_run()
        result = normalize_hour(self.queue, 21, 7, confirm_timestamp_kwh=True, apply=True)
        self.assertEqual((result["status"], result["register_insert_count"], result["hour_insert_count"]),
                         ("committed", 0, 0))
        self.assertEqual(self.counts(), (5, 1))
        self.assertEqual(self.connection.db.execute(
            "SELECT COUNT(*) FROM myems_system_db.tbl_jishiyun_events WHERE event_type='business_hour_committed'",
        ).fetchone()[0], 2)

    def test_multihour_overlap_reuses_single_hour_proof_but_not_changed_revision(self):
        self.process(apply=True)
        self.copy_run()
        self.extend_two_hours()
        self.change("UPDATE myems_historical_db.tbl_jishiyun_energy_readings SET revision=2 WHERE id=1")
        with self.assertRaises(BusinessGateError):
            self.process(apply=True)
        self.change("UPDATE myems_historical_db.tbl_jishiyun_energy_readings SET revision=1 WHERE id=1")
        self.change("UPDATE myems_system_db.tbl_jishiyun_runs SET "
                    "window_end_utc='2026-10-01 06:00:00',expected_count=8,valid_count=8 WHERE id=21")
        result = normalize_hour(self.queue, 21, 7, confirm_timestamp_kwh=True, apply=True)
        self.assertEqual((result["register_insert_count"], result["hour_insert_count"]), (4, 1))
        self.assertEqual(self.counts(), (9, 2))

    def test_auxiliary_quarter_hour_is_not_a_billable_hour(self):
        self.change("UPDATE myems_system_db.tbl_jishiyun_runs "
                    "SET window_end_utc='2026-10-01 04:15:00',expected_count=1,valid_count=1 WHERE id=20")
        with self.assertRaisesRegex(BusinessGateError, "aligned_whole_hour"):
            self.process(apply=True)
        self.assertEqual(self.counts(), (0, 0))

    def settlement_setup(self):
        self.connection.db.create_function("TIME_TO_SEC", 1, lambda value: int(value))
        self.connection.db.executescript("""
            ALTER TABLE myems_system_db.tbl_energy_categories ADD COLUMN name TEXT;
            ALTER TABLE myems_system_db.tbl_energy_categories ADD COLUMN unit_of_measure TEXT;
            ALTER TABLE myems_system_db.tbl_energy_categories ADD COLUMN kgco2e TEXT;
            ALTER TABLE myems_system_db.tbl_cost_centers ADD COLUMN name TEXT;
            UPDATE myems_system_db.tbl_energy_categories
                SET name='PILOT TEST electricity',unit_of_measure='kWh',kgco2e='1.5';
            UPDATE myems_system_db.tbl_cost_centers SET name='PILOT TEST cost center';
            ALTER TABLE myems_billing_db.tbl_meter_hourly ADD COLUMN actual_value TEXT;
            ALTER TABLE myems_carbon_db.tbl_meter_hourly ADD COLUMN actual_value TEXT;
            CREATE TABLE myems_system_db.tbl_tariffs (
                id INTEGER PRIMARY KEY, name TEXT, energy_category_id INTEGER,
                valid_from_datetime_utc TEXT,valid_through_datetime_utc TEXT,tariff_type TEXT,unit_of_price TEXT
            );
            CREATE TABLE myems_system_db.tbl_cost_centers_tariffs (cost_center_id INTEGER,tariff_id INTEGER);
            CREATE TABLE myems_system_db.tbl_tariffs_timeofuses (
                tariff_id INTEGER,start_time_of_day INTEGER,end_time_of_day INTEGER,price TEXT
            );
            INSERT INTO information_schema.TABLES VALUES
                ('myems_system_db','tbl_tariffs','InnoDB'),
                ('myems_system_db','tbl_cost_centers_tariffs','InnoDB'),
                ('myems_system_db','tbl_tariffs_timeofuses','InnoDB');
            INSERT INTO myems_system_db.tbl_tariffs VALUES
                (1,'PILOT TEST 2.3',1,'2026-10-01 00:00:00','2026-10-02 00:00:00','timeofuse','元/kWh');
            INSERT INTO myems_system_db.tbl_cost_centers_tariffs VALUES (1,1);
            INSERT INTO myems_system_db.tbl_tariffs_timeofuses VALUES (1,0,86400,'2.3');
        """)
        self.connection.db.commit()

    def correction_setup(self, *, dimensions=False):
        self.settlement_setup()
        self.extend_two_hours()
        self.process(apply=True)
        self.settle(apply=True)
        self.copy_run()
        normalize_hour(self.queue, 21, 7, confirm_timestamp_kwh=True, apply=True)
        self.settle(apply=True, run_id=21)
        self.connection.db.executescript("""
            CREATE TABLE myems_system_db.tbl_spaces_meters (id INTEGER PRIMARY KEY, space_id INTEGER,meter_id INTEGER);
            CREATE TABLE myems_system_db.tbl_equipments_meters
                (id INTEGER PRIMARY KEY,equipment_id INTEGER,meter_id INTEGER,is_output INTEGER);
        """)
        if dimensions:
            self.connection.db.executescript("""
                CREATE TABLE myems_system_db.tbl_spaces (id INTEGER PRIMARY KEY,parent_space_id INTEGER);
                CREATE TABLE myems_system_db.tbl_equipments (id INTEGER PRIMARY KEY,is_input_counted INTEGER);
                INSERT INTO myems_system_db.tbl_spaces VALUES (2,NULL);
                INSERT INTO myems_system_db.tbl_spaces_meters VALUES (1,2,1);
                INSERT INTO myems_system_db.tbl_equipments_meters VALUES (1,3,1,0);
            """)
            for kind, relations in (("space", ("virtual_meters", "offline_meters", "equipments",
                                               "combined_equipments", "shopfloors", "stores", "tenants")),
                                    ("equipment", ("virtual_meters", "offline_meters"))):
                for relation in relations:
                    self.connection.db.execute(
                        "CREATE TABLE myems_system_db.tbl_" + kind + "s_" + relation
                        + "(id INTEGER PRIMARY KEY," + kind + "_id INTEGER"
                        + (",equipment_id INTEGER" if kind == "space" and relation == "equipments" else "") + ")",
                    )
                for schema in ("myems_energy_db", "myems_billing_db", "myems_carbon_db"):
                    table = "tbl_" + kind + "_input_category_hourly"
                    self.connection.db.execute("CREATE TABLE " + schema + "." + table
                                               + "(id INTEGER PRIMARY KEY," + kind
                                               + "_id INTEGER,energy_category_id INTEGER,start_datetime_utc TEXT,actual_value TEXT)")
                    self.connection.db.execute("INSERT INTO information_schema.TABLES VALUES (?,?,'InnoDB')",
                                               (schema, table))
            self.connection.db.execute(
                "INSERT INTO myems_system_db.tbl_jishiyun_events (data_source_id,event_type,details_json) "
                "VALUES (1,'pilot_dimensions_configured',?)",
                (json.dumps({"meter_ids": [1], "space_id": 2, "equipment_id": 3, "energy_category_id": 1}),),
            )
        self.connection.db.commit()
        if dimensions:
            aggregate_dimensions(self.queue, 7, START, START + timedelta(hours=2), apply=True)

    def revised_boundary(self):
        self.change("UPDATE myems_historical_db.tbl_jishiyun_energy_readings "
                    "SET forward_active_register='14.5',revision=2 WHERE id=5")

    def test_correction_preview_hash_adjacent_hours_apply_and_all_proof_replays(self):
        self.correction_setup()
        self.revised_boundary()
        preview = correct_run(self.queue, 20, 7)
        self.assertEqual(len(preview["affected_hours"]), 2)
        self.assertEqual(len(preview["proof_updates"]), 4)
        self.assertEqual([h["after"]["consumption_kwh"] for h in preview["affected_hours"]],
                         ["4.500000", "3.500000"])
        with self.assertRaisesRegex(BusinessGateError, "approval_hash"):
            correct_run(self.queue, 20, 7, apply=True)
        result = correct_run(self.queue, 20, 7, expected_hash=preview["approval_hash"], apply=True)
        self.assertEqual(result["status"], "committed")
        self.assertEqual(correct_run(
            self.queue, 20, 7, expected_hash=preview["approval_hash"], apply=True,
        )["status"], "already_committed")
        self.assertEqual(self.process(apply=True)["status"], "already_committed")
        self.assertEqual(self.settle(apply=True)["status"], "already_committed")
        self.assertEqual(normalize_hour(self.queue, 21, 7, confirm_timestamp_kwh=True, apply=True)["status"],
                         "already_committed")
        self.assertEqual(self.settle(apply=True, run_id=21)["status"], "already_committed")
        self.assertEqual(self.connection.db.execute(
            "SELECT COUNT(*) FROM myems_system_db.tbl_jishiyun_events WHERE event_type='business_correction_committed'",
        ).fetchone()[0], 1)

    def test_correction_stale_preview_and_tampered_target_block(self):
        self.correction_setup()
        self.revised_boundary()
        preview = correct_run(self.queue, 20, 7)
        self.change("UPDATE myems_historical_db.tbl_jishiyun_energy_readings "
                    "SET forward_active_register='14.25',revision=3 WHERE id=5")
        with self.assertRaisesRegex(BusinessGateError, "preview_stale"):
            correct_run(self.queue, 20, 7, expected_hash=preview["approval_hash"], apply=True)
        self.change("UPDATE myems_energy_db.tbl_meter_hourly SET actual_value='999' WHERE id=1")
        with self.assertRaisesRegex(BusinessGateError, "modified"):
            correct_run(self.queue, 20, 7)

    def test_correction_transaction_rolls_back_all_outputs_and_audit(self):
        self.correction_setup()
        self.revised_boundary()
        preview = correct_run(self.queue, 20, 7)
        self.connection.fail_on = "INSERT INTO myems_system_db.tbl_jishiyun_events"
        with self.assertRaises(sqlite3.OperationalError):
            correct_run(self.queue, 20, 7, expected_hash=preview["approval_hash"], apply=True)
        self.connection.fail_on = None
        after = correct_run(self.queue, 20, 7)
        self.assertEqual(after["approval_hash"], preview["approval_hash"])
        self.assertEqual(after["status"], "preview")

    def test_correction_no_changes_and_quality_revision_rate_guards(self):
        self.correction_setup()
        self.assertEqual(correct_run(self.queue, 20, 7)["status"], "no_changes")
        self.revised_boundary()
        self.change("UPDATE myems_historical_db.tbl_jishiyun_energy_readings SET revision=1 WHERE id=5")
        with self.assertRaisesRegex(BusinessGateError, "increasing"):
            correct_run(self.queue, 20, 7)
        self.change("UPDATE myems_historical_db.tbl_jishiyun_energy_readings SET revision=2,quality='invalid' WHERE id=5")
        with self.assertRaisesRegex(BusinessGateError, "quality"):
            correct_run(self.queue, 20, 7)
        self.change("UPDATE myems_historical_db.tbl_jishiyun_energy_readings SET quality='valid' WHERE id=5")
        self.change("UPDATE myems_system_db.tbl_tariffs_timeofuses SET price='3'")
        with self.assertRaisesRegex(BusinessGateError, "rate_changed"):
            correct_run(self.queue, 20, 7)

    def test_dimension_aggregation_and_correction_are_atomic_and_idempotent(self):
        self.correction_setup(dimensions=True)
        self.assertEqual(aggregate_dimensions(
            self.queue, 7, START, START + timedelta(hours=2), apply=True,
        )["status"], "already_committed")
        self.revised_boundary()
        preview = correct_run(self.queue, 20, 7)
        dimension_writes = [w for w in preview["writes"] if "_input_category_" in w["table"]]
        self.assertEqual(len(dimension_writes), 12)
        correct_run(self.queue, 20, 7, expected_hash=preview["approval_hash"], apply=True)
        self.assertEqual(aggregate_dimensions(
            self.queue, 7, START, START + timedelta(hours=2), apply=True,
        )["status"], "already_committed")
        self.assertEqual(self.connection.db.execute(
            "SELECT actual_value FROM myems_energy_db.tbl_space_input_category_hourly ORDER BY start_datetime_utc",
        ).fetchall(), [("4.500000",), ("3.500000",)])

    def test_dimension_missing_member_duplicate_and_changed_topology_never_fill_zero(self):
        self.correction_setup(dimensions=True)
        self.change("DELETE FROM myems_carbon_db.tbl_meter_hourly WHERE id=1")
        with self.assertRaisesRegex(BusinessGateError, "every_member"):
            aggregate_dimensions(self.queue, 7, START, START + timedelta(hours=2), apply=True)
        self.change("INSERT INTO myems_system_db.tbl_spaces_meters VALUES (2,2,1)")
        with self.assertRaisesRegex(BusinessGateError, "membership"):
            aggregate_dimensions(self.queue, 7, START, START + timedelta(hours=2), apply=True)

    def test_correction_refuses_unaudited_adjacent_hour_and_derived_dependencies(self):
        self.correction_setup()
        self.change("UPDATE myems_historical_db.tbl_jishiyun_energy_readings "
                    "SET forward_active_register='18.5',revision=2 WHERE id=9")
        self.change("INSERT INTO myems_energy_db.tbl_meter_hourly "
                    "(meter_id,start_datetime_utc,actual_value) VALUES (1,'2026-10-01 06:00:00','1')")
        with self.assertRaisesRegex(BusinessGateError, "adjacent_hour"):
            correct_run(self.queue, 20, 7)
        self.change("DELETE FROM myems_energy_db.tbl_meter_hourly WHERE start_datetime_utc='2026-10-01 06:00:00'")
        self.connection.db.executescript("""
            CREATE TABLE myems_system_db.tbl_variables (id INTEGER,meter_id INTEGER,meter_type TEXT);
            INSERT INTO myems_system_db.tbl_variables VALUES (1,1,'meter');
            INSERT INTO information_schema.TABLES VALUES ('myems_system_db','tbl_variables','InnoDB');
        """)
        with self.assertRaisesRegex(BusinessGateError, "derived_dependency"):
            correct_run(self.queue, 20, 7)

    def test_successive_corrections_preserve_append_only_audits(self):
        self.correction_setup(dimensions=True)
        original = self.connection.db.execute(
            "SELECT details_json FROM myems_system_db.tbl_jishiyun_events WHERE event_type='business_hour_committed'",
        ).fetchall()
        for value, revision in (("14.5", 2), ("14.25", 3)):
            self.change("UPDATE myems_historical_db.tbl_jishiyun_energy_readings "
                        "SET forward_active_register='" + value + "',revision=" + str(revision) + " WHERE id=5")
            preview = correct_run(self.queue, 20, 7)
            self.assertEqual(correct_run(
                self.queue, 20, 7, expected_hash=preview["approval_hash"], apply=True,
            )["status"], "committed")
            self.assertEqual(self.settle(apply=True)["status"], "already_committed")
            self.assertEqual(self.process(apply=True)["status"], "already_committed")
        self.assertEqual(self.connection.db.execute(
            "SELECT details_json FROM myems_system_db.tbl_jishiyun_events WHERE event_type='business_hour_committed'",
        ).fetchall(), original)
        self.assertEqual(self.connection.db.execute(
            "SELECT actual_value FROM myems_energy_db.tbl_meter_hourly ORDER BY start_datetime_utc",
        ).fetchall(), [("4.250000",), ("3.750000",)])

    def test_dimension_audit_failure_rolls_back_inserts_and_category_tamper_is_rejected(self):
        self.correction_setup(dimensions=True)
        self.change("UPDATE myems_energy_db.tbl_space_input_category_hourly SET energy_category_id=2")
        with self.assertRaisesRegex(BusinessGateError, "category_changed"):
            aggregate_dimensions(self.queue, 7, START, START + timedelta(hours=2), apply=True)
        for schema in ("myems_energy_db", "myems_billing_db", "myems_carbon_db"):
            for kind in ("space", "equipment"):
                self.change("DELETE FROM " + schema + ".tbl_" + kind + "_input_category_hourly")
        self.change("DELETE FROM myems_system_db.tbl_jishiyun_events WHERE event_type='dimension_hours_committed'")
        self.connection.fail_on = "INSERT INTO myems_system_db.tbl_jishiyun_events"
        with self.assertRaises(sqlite3.OperationalError):
            aggregate_dimensions(self.queue, 7, START, START + timedelta(hours=2), apply=True)
        self.connection.fail_on = None
        self.assertEqual(aggregate_dimensions(
            self.queue, 7, START, START + timedelta(hours=2),
        )["insert_count"], 12)

    def test_business_scheduler_finishes_dimensions_or_persists_distinct_dimension_gate(self):
        self.correction_setup(dimensions=True)
        result = process_business(self.queue, 7, confirm_timestamp_kwh=True, pilot_test_settlement=True)
        self.assertEqual(result["status"], "done")
        with patch("myems_jishiyun.dimensions.aggregate_run_dimensions",
                   side_effect=BusinessGateError("topology_changed")):
            result = process_business(self.queue, 7, confirm_timestamp_kwh=True, pilot_test_settlement=True)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["gate_code"], "dimensions:topology_changed")
        self.assertEqual(self.counts(), (9, 2))
        self.assertEqual(self.settlement_counts(), (2, 2))

    def processing_reading(self, minute=15):
        with self.queue.transaction() as cursor:
            return reading_processing(cursor, 1, [("0001", (START + timedelta(minutes=minute)).replace(tzinfo=None))])[0]

    def test_processing_verifies_each_phase_and_midnight_boundary_not_last_writer(self):
        self.correction_setup(dimensions=True)
        result = self.processing_reading()
        self.assertEqual((result["energy_status"], result["settlement_status"], result["dimensions_status"]),
                         ("processed", "processed", "processed"))
        self.change("UPDATE myems_historical_db.tbl_jishiyun_energy_readings SET last_run_id=999")
        self.assertEqual(self.processing_reading()["energy_status"], "processed")
        boundary = self.processing_reading(120)
        self.assertEqual(boundary["energy_status"], "partial")
        self.assertEqual(len(boundary["processing_details"]), 2)
        self.assertEqual(boundary["processing_details"][0]["energy"]["status"], "processed")
        self.assertEqual(boundary["processing_details"][1]["energy"]["status"], "pending")
        self.change("UPDATE myems_system_db.tbl_jishiyun_meter_mappings SET is_enabled=0")
        self.assertEqual(self.processing_reading()["energy_status"], "processed")

    def test_processing_stale_raw_revision_and_tampered_targets_are_not_processed(self):
        self.correction_setup(dimensions=True)
        self.revised_boundary()
        result = self.processing_reading(60)
        self.assertEqual(result["energy_status"], "review_required")
        self.assertEqual(result["settlement_status"], "pending")
        preview = correct_run(self.queue, 20, 7)
        correct_run(self.queue, 20, 7, expected_hash=preview["approval_hash"], apply=True)
        self.assertEqual(self.processing_reading(60)["dimensions_status"], "processed")
        self.change("DELETE FROM myems_billing_db.tbl_meter_hourly WHERE id=1")
        result = self.processing_reading()
        self.assertEqual(result["energy_status"], "processed")
        self.assertEqual(result["settlement_status"], "review_required")
        self.assertEqual(result["dimensions_status"], "pending")

    def test_processing_reports_blocked_missing_boundary_separate_from_failed_job(self):
        self.settlement_setup()
        self.change("DELETE FROM myems_historical_db.tbl_jishiyun_energy_readings WHERE id=5")
        result = self.processing_reading()
        self.assertEqual(result["energy_status"], "blocked")
        self.assertIn("five_quarter", result["processing_details"][0]["energy"]["reason"])
        self.change("INSERT INTO myems_historical_db.tbl_jishiyun_energy_readings "
                    "(data_source_id,external_address,external_id,reading_datetime_utc,date_type,value_type,"
                    "forward_active_register,quality,raw_record_json,first_run_id,last_run_id) "
                    "VALUES (1,'0001',1,'2026-10-01 05:00:00','mi15','SJZ','14','valid','{}',20,20)")
        self.change("INSERT INTO myems_system_db.tbl_jishiyun_business_jobs "
                    "(data_source_id,run_id,actor_user_id,status,gate_code) VALUES (1,20,7,'error','business_execution_failed')")
        self.assertEqual(self.processing_reading()["energy_status"], "error")

    def test_processing_does_not_call_unaudited_business_rows_processed(self):
        self.settlement_setup()
        self.change("INSERT INTO myems_energy_db.tbl_meter_hourly "
                    "(meter_id,start_datetime_utc,actual_value) VALUES (1,'2026-10-01 04:00:00','4')")
        self.assertEqual(self.processing_reading()["energy_status"], "pending")

    def test_processing_settlement_blocked_and_dimensions_not_enabled_or_changed(self):
        self.correction_setup()
        self.assertEqual(self.processing_reading()["dimensions_status"], "not_applicable")
        self.change("DELETE FROM myems_system_db.tbl_jishiyun_events WHERE event_type='business_settlement_committed'")
        self.change("INSERT INTO myems_system_db.tbl_jishiyun_business_jobs "
                    "(data_source_id,run_id,actor_user_id,status,gate_code) "
                    "VALUES (1,21,7,'blocked','settlement:exactly_one_applicable_hourly_tariff_required')")
        result = self.processing_reading()
        self.assertEqual(result["energy_status"], "processed")
        self.assertEqual(result["settlement_status"], "blocked")
        self.assertIn("tariff", result["processing_details"][0]["settlement"]["reason"])

    def test_processing_raw_quality_blocks_even_without_full_hour_run_and_dimension_tamper_needs_review(self):
        self.correction_setup(dimensions=True)
        self.change("UPDATE myems_carbon_db.tbl_equipment_input_category_hourly SET actual_value='999' WHERE id=1")
        self.assertEqual(self.processing_reading()["dimensions_status"], "review_required")
        self.change("UPDATE myems_historical_db.tbl_jishiyun_energy_readings SET quality='invalid' WHERE id=9")
        result = self.processing_reading(120)
        self.assertEqual(result["energy_status"], "blocked")
        self.assertTrue(all(hour["energy"]["reason"] == "raw_quality_invalid_or_conflict"
                            for hour in result["processing_details"]))

    def settle(self, apply=False, run_id=20):
        return settle_run(self.queue, run_id, 7, test_only=True, apply=apply)

    def settlement_counts(self):
        return tuple(self.connection.db.execute("SELECT COUNT(*) FROM " + schema + ".tbl_meter_hourly").fetchone()[0]
                     for schema in ("myems_billing_db", "myems_carbon_db"))

    def test_settlement_requires_energy_audit_and_explicit_test_labels(self):
        self.settlement_setup()
        with self.assertRaisesRegex(BusinessGateError, "audited_energy"):
            self.settle(apply=True)
        self.process(apply=True)
        with self.assertRaisesRegex(BusinessGateError, "test_parameters"):
            settle_run(self.queue, 20, 7, test_only=False, apply=True)
        self.assertEqual(self.settlement_counts(), (0, 0))

    def test_settlement_preview_exact_values_and_idempotency(self):
        self.settlement_setup()
        self.process(apply=True)
        result = self.settle()
        self.assertEqual(self.settlement_counts(), (0, 0))
        self.assertEqual((result["entries"][0]["billing"], result["entries"][0]["carbon"]),
                         ("9.200000", "6.000000"))
        self.assertEqual(self.settle(apply=True)["status"], "committed")
        self.assertEqual(self.settle(apply=True)["status"], "already_committed")
        self.assertEqual(self.settlement_counts(), (1, 1))
        self.assertEqual(self.connection.db.execute(
            "SELECT COUNT(*) FROM myems_system_db.tbl_jishiyun_events "
            "WHERE event_type='business_settlement_committed'",
        ).fetchone()[0], 1)

    def test_settlement_multihour_and_cross_run_overlap_do_not_duplicate(self):
        self.settlement_setup()
        self.extend_two_hours()
        self.process(apply=True)
        self.settle(apply=True)
        self.copy_run()
        normalize_hour(self.queue, 21, 7, confirm_timestamp_kwh=True, apply=True)
        result = self.settle(apply=True, run_id=21)
        self.assertEqual((result["billing_insert_count"], result["carbon_insert_count"]), (0, 0))
        self.assertEqual(self.settlement_counts(), (2, 2))

    def test_settlement_raw_revision_or_energy_tamper_blocks_writes(self):
        self.settlement_setup()
        self.process(apply=True)
        self.change("UPDATE myems_historical_db.tbl_jishiyun_energy_readings SET revision=2 WHERE id=1")
        with self.assertRaisesRegex(BusinessGateError, "committed_input_changed"):
            self.settle(apply=True)
        self.change("UPDATE myems_historical_db.tbl_jishiyun_energy_readings SET revision=1 WHERE id=1")
        self.change("UPDATE myems_energy_db.tbl_meter_hourly SET actual_value='5'")
        with self.assertRaisesRegex(BusinessGateError, "energy_changed"):
            self.settle(apply=True)
        self.assertEqual(self.settlement_counts(), (0, 0))

    def test_settlement_missing_overlapping_or_intrahour_rate_blocks(self):
        self.settlement_setup()
        self.process(apply=True)
        changes = (
            ("UPDATE myems_system_db.tbl_tariffs_timeofuses SET end_time_of_day=3600",
             "UPDATE myems_system_db.tbl_tariffs_timeofuses SET end_time_of_day=86400"),
            ("INSERT INTO myems_system_db.tbl_cost_centers_tariffs VALUES (1,1)",
             "DELETE FROM myems_system_db.tbl_cost_centers_tariffs WHERE rowid=2"),
            ("UPDATE myems_system_db.tbl_tariffs_timeofuses SET end_time_of_day=45000",
             "UPDATE myems_system_db.tbl_tariffs_timeofuses SET end_time_of_day=86400"),
        )
        for sql, undo in changes:
            with self.subTest(sql=sql):
                self.change(sql)
                with self.assertRaises(BusinessGateError):
                    self.settle(apply=True)
                self.assertEqual(self.settlement_counts(), (0, 0))
                self.change(undo)

    def test_tariff_ending_at_hour_start_does_not_overlap_current_tariff(self):
        self.settlement_setup()
        self.process(apply=True)
        self.change("INSERT INTO myems_system_db.tbl_tariffs VALUES "
                    "(2,'PILOT TEST expired',1,'2026-09-01 00:00:00','2026-10-01 04:00:00','timeofuse','元/kWh')")
        self.change("INSERT INTO myems_system_db.tbl_cost_centers_tariffs VALUES (1,2)")
        self.assertEqual(self.settle(apply=True)["status"], "committed")
        self.assertEqual(self.settlement_counts(), (1, 1))

    def test_settlement_failure_rolls_back_billing_carbon_and_audit(self):
        self.settlement_setup()
        self.process(apply=True)
        self.connection.fail_on = "INSERT INTO myems_carbon_db.tbl_meter_hourly"
        with self.assertRaises(sqlite3.OperationalError):
            self.settle(apply=True)
        self.assertEqual(self.settlement_counts(), (0, 0))
        self.assertEqual(self.connection.db.execute(
            "SELECT COUNT(*) FROM myems_system_db.tbl_jishiyun_events "
            "WHERE event_type='business_settlement_committed'",
        ).fetchone()[0], 0)

    def test_settlement_changed_rate_or_deleted_output_requires_review(self):
        self.settlement_setup()
        self.process(apply=True)
        self.settle(apply=True)
        self.change("UPDATE myems_system_db.tbl_tariffs_timeofuses SET price='3'")
        with self.assertRaisesRegex(BusinessGateError, "inputs_changed"):
            self.settle(apply=True)
        self.change("UPDATE myems_system_db.tbl_tariffs_timeofuses SET price='2.3'")
        self.change("DELETE FROM myems_carbon_db.tbl_meter_hourly")
        with self.assertRaisesRegex(BusinessGateError, "settlement_changed"):
            self.settle(apply=True)

    def test_settlement_unowned_existing_result_is_not_adopted(self):
        self.settlement_setup()
        self.process(apply=True)
        self.change("INSERT INTO myems_billing_db.tbl_meter_hourly "
                    "(meter_id,start_datetime_utc,actual_value) VALUES (1,'2026-10-01 04:00:00','9.2')")
        with self.assertRaisesRegex(BusinessGateError, "unowned_settlement"):
            self.settle(apply=True)
        self.assertEqual(self.settlement_counts(), (1, 0))

    def test_automatic_pilot_settlement_is_opt_in_and_durable_on_gate(self):
        self.settlement_setup()
        self.change("UPDATE myems_system_db.tbl_tariffs_timeofuses SET end_time_of_day=3600")
        result = process_business(self.queue, 7, confirm_timestamp_kwh=True, pilot_test_settlement=True)
        self.assertEqual(result["status"], "blocked")
        self.assertTrue(result["gate_code"].startswith("settlement:"))
        self.assertEqual(self.counts(), (5, 1))
        self.assertEqual(self.settlement_counts(), (0, 0))
        self.change("UPDATE myems_system_db.tbl_tariffs_timeofuses SET end_time_of_day=86400")
        self.connection.now += timedelta(minutes=5)
        result = process_business(self.queue, 7, confirm_timestamp_kwh=True, pilot_test_settlement=True)
        self.assertEqual(result["status"], "done")
        self.assertEqual(self.counts(), (5, 1))
        self.assertEqual(self.settlement_counts(), (1, 1))
        self.assertIsNone(process_business(self.queue, 7, confirm_timestamp_kwh=True, pilot_test_settlement=True))

    def test_settlement_cli_preview_and_test_scheduler_requires_business_mode(self):
        self.settlement_setup()
        self.process(apply=True)
        with patch.dict("os.environ", {"JISHIYUN_DATA_SOURCE_ID": "1"}), patch(
            "myems_jishiyun.worker.database_factory", return_value=lambda: self.connection,
        ):
            output = io.StringIO()
            with redirect_stdout(output):
                code = main(["business-settle", "--run-id", "20", "--actor", "7", "--test-only"])
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(output.getvalue())["status"], "preview")
            self.assertEqual(self.settlement_counts(), (0, 0))
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                main(["run", "--pilot-test-settlement"])

    def test_business_unexpected_failure_is_durable_sanitized_and_propagated(self):
        with patch("myems_jishiyun.business.normalize_hour", side_effect=RuntimeError("synthetic-secret")):
            with self.assertRaises(RuntimeError):
                self.business()
        self.assertEqual(self.job()[:3], ("error", 1, "business_execution_failed"))
        self.assertIsNone(self.business())

    def test_business_scheduler_requires_confirmation_and_migration(self):
        with self.assertRaises(BusinessGateError):
            process_business(self.queue, 7, confirm_timestamp_kwh=False)
        self.change("DELETE FROM information_schema.TABLES WHERE TABLE_NAME='tbl_jishiyun_business_jobs'")
        with self.assertRaisesRegex(PersistenceError, "migration"):
            self.business()
        self.assertEqual(self.counts(), (0, 0))

    def test_worker_opt_in_business_calls_after_acquisition(self):
        with patch.dict("os.environ", {
            "JISHIYUN_DATA_SOURCE_ID": "1", "JISHIYUN_BASE_URL": "https://example.test",
            "JISHIYUN_API_KEY": "synthetic",
        }), patch("myems_jishiyun.worker.database_factory", return_value=lambda: self.connection), patch(
            "myems_jishiyun.worker.JishiyunClient",
        ), patch("myems_jishiyun.worker.execute_one", return_value=None), patch(
            "myems_jishiyun.worker.process_business", return_value={"status": "done"},
        ) as business:
            with redirect_stdout(io.StringIO()):
                self.assertEqual(main(["run", "--once"]), 0)
            business.assert_not_called()
            with redirect_stdout(io.StringIO()):
                self.assertEqual(main(["run", "--once", "--business-schedule", "--business-actor", "7",
                                       "--confirm-timestamp-kwh"]), 0)
            business.assert_called_once()
            business.return_value = {"status": "blocked", "gate_code": "boundary_required"}
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(main(["run", "--once", "--business-schedule", "--business-actor", "7",
                                       "--confirm-timestamp-kwh"]), 2)
            self.assertNotIn('"status": "idle"', output.getvalue())
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                main(["run", "--business-schedule"])


if __name__ == "__main__":
    unittest.main()
