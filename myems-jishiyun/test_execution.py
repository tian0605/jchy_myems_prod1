"""Queue and runner behavior tests, with explicit simulated MySQL lock functions."""

import io
import json
import sqlite3
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import timedelta, timezone
from unittest.mock import MagicMock, patch
from uuid import uuid4

from myems_jishiyun.client import JishiyunClient, TransportError
from myems_jishiyun.contracts import ContractError
from myems_jishiyun.execution import LEASE, ClaimedRun, RecordedContractError, RunQueue, SharedRequestGate, execute_one
from myems_jishiyun.persistence import PersistenceError
from test_client import BASE_URL, SYNTHETIC_KEY, http_error, opened_response
from test_persistence import END, NOW, START, SQLiteConnection, SQLiteCursor, reading
from myems_jishiyun.worker import main


class ExecutionCursor(SQLiteCursor):
    def execute(self, operation: str, params: tuple[object, ...] = ()) -> object:
        return super().execute(operation.replace("INSERT IGNORE", "INSERT OR IGNORE"), params)


class ExecutionConnection(SQLiteConnection):
    def __init__(self) -> None:
        super().__init__()
        self.locks: set[str] = set()
        self.lock_available = True
        self.db.create_function("GET_LOCK", 2, self.get_lock)
        self.db.create_function("RELEASE_LOCK", 1, self.release_lock)
        self.db.executescript("""
            ALTER TABLE myems_system_db.tbl_jishiyun_runs ADD COLUMN task_id INTEGER;
            ALTER TABLE myems_system_db.tbl_jishiyun_runs ADD COLUMN request_key TEXT;
        """)
        self.db.executescript("""
            CREATE UNIQUE INDEX myems_system_db.run_key ON tbl_jishiyun_runs(request_key);
            ALTER TABLE myems_system_db.tbl_jishiyun_runs ADD COLUMN requested_by_user_id INTEGER;
            ALTER TABLE myems_system_db.tbl_jishiyun_runs ADD COLUMN started_datetime_utc TEXT;
            ALTER TABLE myems_system_db.tbl_jishiyun_runs ADD COLUMN attempt_count INTEGER NOT NULL DEFAULT 0;
            ALTER TABLE myems_system_db.tbl_jishiyun_runs ADD COLUMN request_count INTEGER NOT NULL DEFAULT 0;
            ALTER TABLE myems_system_db.tbl_jishiyun_runs ADD COLUMN next_attempt_datetime_utc TEXT;
            CREATE TABLE myems_system_db.tbl_jishiyun_tasks (
                id INTEGER PRIMARY KEY, data_source_id INTEGER, task_type TEXT, is_enabled INTEGER,
                delay_minutes INTEGER, provider_utc_offset_minutes INTEGER, parameters_json TEXT
            );
            INSERT INTO myems_system_db.tbl_jishiyun_tasks VALUES
                (1, 1, 'energy_collect', 1, 40, 480, '{"encoding":"json"}'),
                (2, 1, 'energy_backfill', 1, 40, 480, '{"encoding":"form"}');
            CREATE TABLE myems_system_db.tbl_jishiyun_meter_mappings (
                id INTEGER PRIMARY KEY, data_source_id INTEGER, external_address TEXT, external_id INTEGER,
                configuration_status TEXT, is_enabled INTEGER, collection_start_utc TEXT, collection_end_utc TEXT
            );
            CREATE TABLE myems_system_db.tbl_jishiyun_worker_state (
                data_source_id INTEGER PRIMARY KEY, worker_id TEXT, heartbeat_datetime_utc TEXT,
                lease_expires_datetime_utc TEXT, next_request_datetime_utc TEXT
            );
            CREATE TABLE myems_system_db.tbl_jishiyun_request_quotas (
                credential_fingerprint TEXT PRIMARY KEY, next_request_datetime_utc TEXT,
                is_blocked INTEGER NOT NULL DEFAULT 0
            );
            INSERT INTO information_schema.TABLES VALUES
                ('myems_system_db', 'tbl_jishiyun_tasks', 'InnoDB'),
                ('myems_system_db', 'tbl_jishiyun_meter_mappings', 'InnoDB'),
                ('myems_system_db', 'tbl_jishiyun_worker_state', 'InnoDB'),
                ('myems_system_db', 'tbl_jishiyun_request_quotas', 'InnoDB');
            CREATE TABLE myems_system_db.tbl_jishiyun_backfill_batches (
                id INTEGER PRIMARY KEY AUTOINCREMENT, request_key TEXT UNIQUE, data_source_id INTEGER,
                task_id INTEGER, requested_by_user_id INTEGER, window_start_utc TEXT, window_end_utc TEXT,
                child_count INTEGER, is_cancelled INTEGER NOT NULL DEFAULT 0,
                cancelled_by_user_id INTEGER, cancelled_datetime_utc TEXT
            );
            INSERT INTO information_schema.TABLES VALUES
                ('myems_system_db', 'tbl_jishiyun_backfill_batches', 'InnoDB');
        """)
        self.db.executescript("""
            ALTER TABLE myems_system_db.tbl_jishiyun_tasks ADD COLUMN schedule_minute INTEGER NOT NULL DEFAULT 40;
            ALTER TABLE myems_system_db.tbl_jishiyun_tasks ADD COLUMN lookback_hours INTEGER NOT NULL DEFAULT 1;
            ALTER TABLE myems_system_db.tbl_jishiyun_events ADD COLUMN actor_user_id INTEGER;
        """)
        self.db.execute(
            "INSERT INTO myems_system_db.tbl_jishiyun_meter_mappings VALUES "
            "(1, 1, '0001', 1, 'ready', 1, ?, NULL)", (START.replace(tzinfo=None).isoformat(" "),),
        )
        self.db.commit()

    def get_lock(self, name: str, _timeout: int) -> int:
        if not self.lock_available or name in self.locks:
            return 0
        self.locks.add(name)
        return 1

    def release_lock(self, name: str) -> int:
        if name not in self.locks:
            return 0
        self.locks.remove(name)
        return 1

    def cursor(self) -> ExecutionCursor:
        return ExecutionCursor(self)

    def close(self) -> None:
        # Tests deliberately share one SQLite session; real factory connections
        # are independent. MySQL advisory-lock/session semantics need live tests.
        pass


class QueueTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = ExecutionConnection()
        self.addCleanup(self.connection.db.close)
        self.queue = RunQueue(lambda: self.connection, 1)
        sleep_patch = patch("myems_jishiyun.execution.time.sleep", side_effect=lambda seconds: setattr(
            self.connection, "now", self.connection.now + timedelta(seconds=seconds),
        ))
        sleep_patch.start()
        self.addCleanup(sleep_patch.stop)

    def enqueue(self, task_id: int = 1, key: str | None = None) -> int:
        return self.queue.enqueue(task_id, key or str(uuid4()), START, END, 7)

    def claimed(self) -> ClaimedRun:
        self.enqueue()
        run = self.queue.claim()
        assert run is not None
        return run

    def scalar(self, expression: str, params: tuple[object, ...] = ()) -> object:
        return self.connection.db.execute(expression, params).fetchone()[0]

    def test_enqueue_snapshots_only_approved_scope_and_allowlisted_parameters(self) -> None:
        self.connection.db.execute(
            "UPDATE myems_system_db.tbl_jishiyun_tasks SET parameters_json = ? WHERE id = 1",
            (json.dumps({"encoding": "json", "secret_must_not_copy": SYNTHETIC_KEY}),),
        )
        self.connection.db.commit()
        run_id = self.enqueue()
        row = self.connection.db.execute(
            "SELECT status, scope_snapshot_json, parameters_json FROM myems_system_db.tbl_jishiyun_runs WHERE id = ?",
            (run_id,),
        ).fetchone()
        self.assertEqual(row[0], "queued")
        self.assertEqual(json.loads(row[1])["meters"][0]["address"], "0001")
        self.assertEqual(json.loads(row[2]), {"delay_minutes": 40, "provider_utc_offset_minutes": 480, "encoding": "json"})
        self.assertNotIn(SYNTHETIC_KEY, row[2])

    def test_enqueue_idempotency_and_submission_mismatch(self) -> None:
        key = str(uuid4())
        first = self.enqueue(key=key)
        self.assertEqual(self.enqueue(key=key), first)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM myems_system_db.tbl_jishiyun_runs"), 1)
        for task_id, actor, end in ((2, 7, END), (1, 8, END), (1, 7, END + timedelta(minutes=15))):
            with self.subTest(task=task_id, actor=actor), self.assertRaisesRegex(PersistenceError, "different submission"):
                self.queue.enqueue(task_id, key, START, end, actor)

    def test_pause_or_nonenergy_task_cannot_enqueue(self) -> None:
        for task_type, enabled in (("energy_collect", 0), ("archive_sync", 1), ("meter_initialize", 1)):
            self.connection.db.execute(
                "UPDATE myems_system_db.tbl_jishiyun_tasks SET task_type = ?, is_enabled = ? WHERE id = 1",
                (task_type, enabled),
            )
            self.connection.db.commit()
            with self.subTest(task_type=task_type), self.assertRaises(PersistenceError):
                self.enqueue()
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM myems_system_db.tbl_jishiyun_runs"), 0)

    def test_due_empty_scope_and_bad_encoding_fail_before_insert(self) -> None:
        self.connection.now = END.replace(tzinfo=None) + timedelta(minutes=39)
        with self.assertRaisesRegex(PersistenceError, "delay"):
            self.enqueue()
        self.connection.now = NOW
        self.connection.db.execute("UPDATE myems_system_db.tbl_jishiyun_meter_mappings SET configuration_status = 'pending'")
        self.connection.db.commit()
        with self.assertRaises(ContractError):
            self.enqueue()
        self.connection.db.execute("UPDATE myems_system_db.tbl_jishiyun_tasks SET parameters_json = '{}' WHERE id = 1")
        self.connection.db.commit()
        with self.assertRaisesRegex(PersistenceError, "encoding"):
            self.enqueue()

    def test_scope_effective_end_retained_for_stopped_meter_history(self) -> None:
        self.connection.db.execute(
            "UPDATE myems_system_db.tbl_jishiyun_meter_mappings SET is_enabled = 0, collection_end_utc = ?",
            ((START + timedelta(minutes=30)).replace(tzinfo=None).isoformat(" "),),
        )
        self.connection.db.commit()
        run_id = self.enqueue()
        snapshot = json.loads(self.scalar(
            "SELECT scope_snapshot_json FROM myems_system_db.tbl_jishiyun_runs WHERE id = ?", (run_id,),
        ))
        self.assertEqual(snapshot["meters"][0]["effective_end_utc"], (START + timedelta(minutes=30)).isoformat())

    def test_claim_source_serialization_and_heartbeat(self) -> None:
        run = self.claimed()
        self.enqueue(task_id=2)
        self.assertIsNone(self.queue.claim())
        self.assertEqual(self.scalar("SELECT attempt_count FROM myems_system_db.tbl_jishiyun_runs WHERE id = ?", (run.id,)), 1)
        self.assertEqual(self.scalar("SELECT worker_id FROM myems_system_db.tbl_jishiyun_worker_state"), run.token)
        self.queue.renew(run)
        self.queue.renew(run, count_request=True)
        self.assertEqual(self.scalar("SELECT request_count FROM myems_system_db.tbl_jishiyun_runs WHERE id = ?", (run.id,)), 1)

    def test_paused_task_stays_queued_and_unsupported_work_is_not_claimed(self) -> None:
        self.enqueue()
        self.connection.db.execute("UPDATE myems_system_db.tbl_jishiyun_tasks SET is_enabled = 0 WHERE id = 1")
        self.connection.db.commit()
        self.assertIsNone(self.queue.claim())
        self.assertEqual(self.scalar("SELECT status FROM myems_system_db.tbl_jishiyun_runs"), "queued")

    def test_expired_claim_recovery_changes_token_and_fences_previous_worker(self) -> None:
        run = self.claimed()
        self.connection.now += LEASE
        newer = self.queue.claim()
        assert newer is not None
        self.assertEqual(newer.id, run.id)
        self.assertNotEqual(newer.token, run.token)
        with self.assertRaisesRegex(PersistenceError, "lost"):
            self.queue.renew(run)
        with self.assertRaisesRegex(PersistenceError, "Lost lease"):
            self.queue.fail(run, "transport_error")
        self.assertEqual(self.scalar("SELECT lease_token FROM myems_system_db.tbl_jishiyun_runs"), newer.token)

    def test_repeated_crashes_reach_bounded_lease_limit(self) -> None:
        self.claimed()
        for _ in range(2):
            self.connection.now += LEASE
            self.assertIsNotNone(self.queue.claim())
        self.connection.now += LEASE
        self.assertIsNone(self.queue.claim())
        self.assertEqual(self.scalar("SELECT error_code FROM myems_system_db.tbl_jishiyun_runs"), "lease_exhausted")
        self.assertEqual(self.scalar("SELECT status FROM myems_system_db.tbl_jishiyun_runs"), "error")

    def test_failure_uses_fixed_message_and_does_not_create_false_missing_alerts(self) -> None:
        run = self.claimed()
        self.queue.fail(run, "transport_error")
        self.assertEqual(self.scalar("SELECT status FROM myems_system_db.tbl_jishiyun_runs"), "error")
        self.assertNotIn(SYNTHETIC_KEY, str(self.scalar("SELECT error_message FROM myems_system_db.tbl_jishiyun_runs")))
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM myems_system_db.tbl_jishiyun_alerts"), 0)
        with self.assertRaises(PersistenceError):
            self.queue.fail(run, "transport_error")

    def test_request_gate_holds_lock_across_actual_request_and_releases_after_error(self) -> None:
        run = self.claimed()
        gate = SharedRequestGate(self.queue, run, SYNTHETIC_KEY)
        with self.assertRaisesRegex(RuntimeError, "synthetic failure"):
            with gate():
                self.assertEqual(len(self.connection.locks), 1)
                raise RuntimeError("synthetic failure")
        self.assertEqual(self.connection.locks, set())
        self.assertEqual(self.scalar("SELECT request_count FROM myems_system_db.tbl_jishiyun_runs"), 1)
        self.assertNotEqual(self.scalar("SELECT credential_fingerprint FROM myems_system_db.tbl_jishiyun_request_quotas"), SYNTHETIC_KEY)

    def test_same_key_across_sources_uses_same_gate_and_persisted_delay(self) -> None:
        run = self.claimed()
        first = SharedRequestGate(self.queue, run, SYNTHETIC_KEY)
        other_source_queue = RunQueue(lambda: self.connection, 2)
        other = SharedRequestGate(other_source_queue, run, SYNTHETIC_KEY)
        self.assertEqual(first.fingerprint, other.fingerprint)
        with first():
            pass
        with patch("myems_jishiyun.execution.time.sleep", side_effect=lambda seconds: setattr(
            self.connection, "now", self.connection.now + timedelta(seconds=seconds),
        )) as sleep:
            with SharedRequestGate(self.queue, run, SYNTHETIC_KEY)():
                self.assertEqual(len(self.connection.locks), 1)
            sleep.assert_called_once_with(8)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM myems_system_db.tbl_jishiyun_request_quotas"), 1)
        self.assertEqual(self.scalar("SELECT request_count FROM myems_system_db.tbl_jishiyun_runs"), 2)

    def test_shared_lock_unavailable_fails_closed_before_counting_request(self) -> None:
        run = self.claimed()
        self.connection.lock_available = False
        with self.assertRaisesRegex(PersistenceError, "lock unavailable"):
            with SharedRequestGate(self.queue, run, SYNTHETIC_KEY)():
                self.fail("Must not enter HTTP body without a quota lock")
        self.assertEqual(self.scalar("SELECT request_count FROM myems_system_db.tbl_jishiyun_runs"), 0)

    def test_expired_run_cannot_enter_http_guard(self) -> None:
        run = self.claimed()
        self.connection.now += LEASE
        with patch("myems_jishiyun.execution.time.sleep"), self.assertRaisesRegex(PersistenceError, "lost"):
            with SharedRequestGate(self.queue, run, SYNTHETIC_KEY)():
                self.fail("Expired worker must not call provider")
        self.assertEqual(self.connection.locks, set())
        self.assertEqual(self.scalar("SELECT request_count FROM myems_system_db.tbl_jishiyun_runs"), 0)

    def test_client_guard_failure_does_not_open_network(self) -> None:
        with patch("myems_jishiyun.client.build_opener") as opener:
            client = JishiyunClient(
                BASE_URL, SYNTHETIC_KEY, timezone.utc, max_attempts=1,
                request_guard=MagicMock(side_effect=PersistenceError("Synthetic quota failure")),
            )
            with self.assertRaises(PersistenceError):
                client.readings(START, END)
            opener.return_value.open.assert_not_called()
            self.assertEqual(client.request_count, 0)

    def test_slow_actual_request_cannot_compress_next_request_gap(self) -> None:
        run = self.claimed()
        gate = SharedRequestGate(self.queue, run, SYNTHETIC_KEY)
        with gate():
            self.connection.now += timedelta(seconds=20)
        with patch("myems_jishiyun.execution.time.sleep", side_effect=lambda seconds: setattr(
            self.connection, "now", self.connection.now + timedelta(seconds=seconds),
        )) as sleep:
            with gate():
                pass
        sleep.assert_called_once_with(8)

    def test_shared_guard_is_reentered_for_each_http_retry(self) -> None:
        run = self.claimed()
        with patch("myems_jishiyun.client.build_opener") as opener:
            opener.return_value.open.side_effect = [
                http_error(503), http_error(503), opened_response(),
            ]
            client = JishiyunClient(
                BASE_URL, SYNTHETIC_KEY, timezone.utc,
                request_guard=SharedRequestGate(self.queue, run, SYNTHETIC_KEY),
            )
            self.assertEqual(client.readings(START, END), ())
        self.assertEqual(self.scalar("SELECT request_count FROM myems_system_db.tbl_jishiyun_runs"), 3)
        self.assertEqual(client.request_count, 3)
        self.assertEqual(self.connection.locks, set())

    def test_end_to_end_claim_http_raw_alert_commit(self) -> None:
        run_id = self.enqueue()
        rows = []
        for minute in (0, 15, 30):
            row = reading(minute).raw.copy()
            row["ReadingDate"] = f"2026-10-01 12:{minute:02}"
            rows.append(row)
        body = json.dumps({"IsSuccess": True, "Code": "0000", "Data": rows, "Count": 3}).encode()
        with patch("myems_jishiyun.client.build_opener") as opener:
            opener.return_value.open.return_value = opened_response(body)
            result = execute_one(self.queue, BASE_URL, SYNTHETIC_KEY)
        assert result is not None
        self.assertEqual((result.status, result.inserted_count, result.missing_count), ("partial", 3, 1))
        self.assertEqual(self.scalar("SELECT status FROM myems_system_db.tbl_jishiyun_runs WHERE id = ?", (run_id,)), "partial")
        self.assertEqual(self.scalar("SELECT request_count FROM myems_system_db.tbl_jishiyun_runs"), 1)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM myems_historical_db.tbl_jishiyun_energy_readings"), 3)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM myems_system_db.tbl_jishiyun_alerts"), 1)

    def test_transport_failure_persists_retry_not_empty_success(self) -> None:
        self.enqueue()
        with patch("myems_jishiyun.client.build_opener") as opener:
            opener.return_value.open.side_effect = TimeoutError(SYNTHETIC_KEY)
            with self.assertRaises(TransportError):
                execute_one(self.queue, BASE_URL, SYNTHETIC_KEY)
        self.assertEqual(self.scalar("SELECT error_code FROM myems_system_db.tbl_jishiyun_runs"), "retry_scheduled")
        self.assertEqual(self.scalar("SELECT status FROM myems_system_db.tbl_jishiyun_runs"), "queued")
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM myems_historical_db.tbl_jishiyun_energy_readings"), 0)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM myems_system_db.tbl_jishiyun_alerts"), 0)
        self.assertEqual(self.connection.locks, set())

    def test_contract_and_persistence_failures_are_recorded(self) -> None:
        for invalid_body, failure in ((True, None), (False, sqlite3.OperationalError(SYNTHETIC_KEY))):
            run_id = self.enqueue()
            with patch("myems_jishiyun.client.build_opener") as opener:
                opener.return_value.open.return_value = opened_response(b"invalid" if invalid_body else b'{"IsSuccess":true,"Code":"0000","Data":[],"Count":0}')
                save_patch = patch("myems_jishiyun.execution.ResultStore.save", side_effect=failure) if failure else patch(
                    "myems_jishiyun.execution.ResultStore.save", side_effect=AssertionError("Contract error must precede save"),
                )
                with save_patch, self.assertRaises((ContractError, sqlite3.OperationalError)):
                    execute_one(self.queue, BASE_URL, SYNTHETIC_KEY)
            self.assertEqual(self.scalar("SELECT status FROM myems_system_db.tbl_jishiyun_runs WHERE id = ?", (run_id,)), "error")
            self.assertNotIn(SYNTHETIC_KEY, str(self.scalar("SELECT error_message FROM myems_system_db.tbl_jishiyun_runs WHERE id = ?", (run_id,))))

    def test_failure_audit_database_outage_is_not_suppressed(self) -> None:
        self.enqueue()
        with patch("myems_jishiyun.client.build_opener") as opener, patch.object(
            self.queue, "retry", side_effect=sqlite3.OperationalError("Synthetic DB outage"),
        ):
            opener.return_value.open.side_effect = TimeoutError(SYNTHETIC_KEY)
            with self.assertRaises(sqlite3.OperationalError):
                execute_one(self.queue, BASE_URL, SYNTHETIC_KEY)
        self.assertEqual(self.scalar("SELECT status FROM myems_system_db.tbl_jishiyun_runs"), "running")
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM myems_historical_db.tbl_jishiyun_energy_readings"), 0)

    def test_contract_failure_marker_requires_committed_failure_audit(self) -> None:
        run_id = self.enqueue()
        with patch("myems_jishiyun.client.build_opener") as opener:
            opener.return_value.open.return_value = opened_response(b"invalid")
            with self.assertRaises(RecordedContractError) as caught:
                execute_one(self.queue, BASE_URL, SYNTHETIC_KEY)
        self.assertEqual(caught.exception.run_id, run_id)
        self.assertEqual(self.scalar("SELECT error_code FROM myems_system_db.tbl_jishiyun_runs"), "contract_error")
        self.enqueue()
        with patch("myems_jishiyun.client.build_opener") as opener, patch.object(
            self.queue, "fail", side_effect=PersistenceError("Failure audit unavailable"),
        ):
            opener.return_value.open.return_value = opened_response(b"invalid")
            with self.assertRaises(PersistenceError):
                execute_one(self.queue, BASE_URL, SYNTHETIC_KEY)

    def test_idle_returns_no_completed_result(self) -> None:
        self.assertIsNone(execute_one(self.queue, BASE_URL, SYNTHETIC_KEY))

    def test_pause_does_not_cancel_already_running_task(self) -> None:
        run = self.claimed()
        self.connection.db.execute("UPDATE myems_system_db.tbl_jishiyun_tasks SET is_enabled = 0 WHERE id = 1")
        self.connection.db.commit()
        self.queue.renew(run)
        self.queue.fail(run, "transport_error")
        self.assertEqual(self.scalar("SELECT status FROM myems_system_db.tbl_jishiyun_runs"), "error")

    def test_cancelled_run_cannot_send_request_or_change_status(self) -> None:
        run = self.claimed()
        self.connection.db.execute("UPDATE myems_system_db.tbl_jishiyun_runs SET status = 'cancelled' WHERE id = ?", (run.id,))
        self.connection.db.commit()
        with self.assertRaises(PersistenceError):
            with SharedRequestGate(self.queue, run, SYNTHETIC_KEY)():
                self.fail("Cancelled run must not call provider")
        with self.assertRaises(PersistenceError):
            self.queue.fail(run, "worker_error")
        self.assertEqual(self.scalar("SELECT status FROM myems_system_db.tbl_jishiyun_runs"), "cancelled")

    def test_enqueue_audit_failure_rolls_back_submission(self) -> None:
        self.connection.fail_on = "INSERT INTO myems_system_db.tbl_jishiyun_events"
        with self.assertRaises(sqlite3.OperationalError):
            self.enqueue()
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM myems_system_db.tbl_jishiyun_runs"), 0)

    def test_quota_database_failure_prevents_network_call(self) -> None:
        self.enqueue()
        self.connection.fail_on = "INSERT OR IGNORE INTO myems_system_db.tbl_jishiyun_request_quotas"
        with patch("myems_jishiyun.client.build_opener") as opener:
            with self.assertRaises(sqlite3.OperationalError):
                execute_one(self.queue, BASE_URL, SYNTHETIC_KEY)
            opener.return_value.open.assert_not_called()
        self.assertEqual(self.scalar("SELECT status FROM myems_system_db.tbl_jishiyun_runs"), "error")
        self.assertEqual(self.scalar("SELECT request_count FROM myems_system_db.tbl_jishiyun_runs"), 0)
        self.assertEqual(self.connection.locks, set())


class WorkerCommandTests(unittest.TestCase):
    def test_recorded_contract_failure_once_is_nonzero_and_not_idle(self) -> None:
        environment = {
            "JISHIYUN_DATA_SOURCE_ID": "1", "JISHIYUN_BASE_URL": BASE_URL, "JISHIYUN_API_KEY": SYNTHETIC_KEY,
        }
        stderr, stdout = io.StringIO(), io.StringIO()
        with patch.dict("os.environ", environment, clear=True), patch("myems_jishiyun.worker.database_factory"), patch(
            "myems_jishiyun.worker.execute_one", side_effect=RecordedContractError(89),
        ), patch("myems_jishiyun.worker.signal.signal"), redirect_stderr(stderr), redirect_stdout(stdout):
            self.assertEqual(main(["run", "--once"]), 2)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(json.loads(stderr.getvalue()), {
            "status": "error", "run_id": 89, "error_code": "contract_error",
            "completed_run": False, "worker_continues": False,
        })

    def test_recorded_contract_failure_keeps_scheduling_and_health_alive(self) -> None:
        environment = {
            "JISHIYUN_DATA_SOURCE_ID": "1", "JISHIYUN_BASE_URL": BASE_URL, "JISHIYUN_API_KEY": SYNTHETIC_KEY,
        }
        event = MagicMock()
        event.is_set.side_effect = [False, False, True]
        stderr, stdout = io.StringIO(), io.StringIO()
        with patch.dict("os.environ", environment, clear=True), patch("myems_jishiyun.worker.database_factory"), patch(
            "myems_jishiyun.worker.execute_one", side_effect=[RecordedContractError(89), None],
        ) as execute, patch("myems_jishiyun.worker.threading.Event", return_value=event), patch(
            "myems_jishiyun.worker.schedule_due",
        ) as schedule, patch("myems_jishiyun.worker.process_business", return_value=None) as business, patch(
            "myems_jishiyun.worker.signal.signal",
        ), patch("myems_jishiyun.worker.record_health") as heartbeat, patch(
            "myems_jishiyun.worker.clear_health",
        ), redirect_stderr(stderr), redirect_stdout(stdout):
            self.assertEqual(main([
                "run", "--schedule", "--business-schedule", "--business-actor", "1",
                "--confirm-timestamp-kwh", "--health-file", "synthetic-health.json",
            ]), 0)
        self.assertEqual(execute.call_count, 2)
        self.assertEqual(schedule.call_count, 2)
        self.assertEqual(business.call_count, 2)
        self.assertEqual(heartbeat.call_count, 2)
        self.assertTrue(json.loads(stderr.getvalue())["worker_continues"])
        self.assertNotIn(SYNTHETIC_KEY, stderr.getvalue())

    def test_unrecorded_contract_or_persistence_failure_still_stops_worker(self) -> None:
        environment = {
            "JISHIYUN_DATA_SOURCE_ID": "1", "JISHIYUN_BASE_URL": BASE_URL, "JISHIYUN_API_KEY": SYNTHETIC_KEY,
        }
        for failure in (ContractError(SYNTHETIC_KEY), PersistenceError(SYNTHETIC_KEY)):
            stderr = io.StringIO()
            with self.subTest(failure=type(failure).__name__), patch.dict(
                "os.environ", environment, clear=True,
            ), patch("myems_jishiyun.worker.database_factory"), patch(
                "myems_jishiyun.worker.execute_one", side_effect=failure,
            ), patch("myems_jishiyun.worker.signal.signal"), redirect_stderr(stderr):
                self.assertEqual(main(["run"]), 1)
            self.assertNotIn(SYNTHETIC_KEY, stderr.getvalue())

    def test_missing_config_exits_nonzero_without_exception_text(self) -> None:
        stderr = io.StringIO()
        with patch.dict("os.environ", {}, clear=True), redirect_stderr(stderr):
            self.assertEqual(main(["run", "--once"]), 1)
        self.assertEqual(json.loads(stderr.getvalue())["status"], "error")

    def test_invalid_url_is_validated_before_claim(self) -> None:
        environment = {
            "JISHIYUN_DATA_SOURCE_ID": "1", "JISHIYUN_BASE_URL": "http://synthetic.invalid/WebApi",
            "JISHIYUN_API_KEY": SYNTHETIC_KEY,
        }
        stderr = io.StringIO()
        with patch.dict("os.environ", environment, clear=True), patch("myems_jishiyun.worker.database_factory"), patch(
            "myems_jishiyun.worker.execute_one",
        ) as execute, redirect_stderr(stderr):
            self.assertEqual(main(["run", "--once"]), 1)
            execute.assert_not_called()
        self.assertNotIn(SYNTHETIC_KEY, stderr.getvalue())

    def test_once_idle_is_not_reported_as_completed_job(self) -> None:
        environment = {
            "JISHIYUN_DATA_SOURCE_ID": "1", "JISHIYUN_BASE_URL": BASE_URL, "JISHIYUN_API_KEY": SYNTHETIC_KEY,
        }
        stdout = io.StringIO()
        with patch.dict("os.environ", environment, clear=True), patch("myems_jishiyun.worker.database_factory"), patch(
            "myems_jishiyun.worker.execute_one", return_value=None,
        ), patch("myems_jishiyun.worker.signal.signal"), redirect_stdout(stdout):
            self.assertEqual(main(["run", "--once"]), 0)
        self.assertEqual(json.loads(stdout.getvalue()), {"status": "idle", "completed_run": False})


if __name__ == "__main__":
    unittest.main()
