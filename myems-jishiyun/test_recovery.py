import io
import json
import sqlite3
import unittest
from typing import Iterator
from contextlib import redirect_stdout
from datetime import timedelta
from unittest.mock import patch
from uuid import uuid4

from myems_jishiyun.batches import batch_status
from myems_jishiyun.client import TransportError, retry_delay
from myems_jishiyun.execution import RetryScheduled, RunQueue, SharedRequestGate, execute_one
from myems_jishiyun.persistence import PersistenceError
from myems_jishiyun.scheduling import enqueue_backfill
from test_client import BASE_URL, SYNTHETIC_KEY, http_error, opened_response
from test_execution import ExecutionConnection
from test_persistence import START, END, NOW
from myems_jishiyun.worker import main


class RecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = ExecutionConnection()
        self.addCleanup(self.connection.db.close)
        self.queue = RunQueue(lambda: self.connection, 1)
        sleep = patch("myems_jishiyun.execution.time.sleep", side_effect=lambda seconds: setattr(
            self.connection, "now", self.connection.now + timedelta(seconds=seconds),
        ))
        sleep.start()
        self.addCleanup(sleep.stop)

    def enqueue(self) -> int:
        return self.queue.enqueue(1, str(uuid4()), START, END, 7)

    def row(self, sql: str) -> tuple[object, ...]:
        return self.connection.db.execute(sql).fetchone()

    def test_transient_failures_backoff_then_finish_at_limit(self) -> None:
        self.enqueue()
        for attempt in range(1, 4):
            with patch("myems_jishiyun.client.build_opener") as opener:
                opener.return_value.open.side_effect = TimeoutError(SYNTHETIC_KEY)
                with self.assertRaises(RetryScheduled if attempt < 3 else TransportError):
                    execute_one(self.queue, BASE_URL, SYNTHETIC_KEY)
            row = self.row(
                "SELECT status, attempt_count, request_count, next_attempt_datetime_utc "
                "FROM myems_system_db.tbl_jishiyun_runs"
            )
            self.assertEqual(row[1:3], (attempt, attempt))
            if attempt < 3:
                self.assertEqual(row[0], "queued")
                self.assertIsNone(self.queue.claim())
                self.connection.now += timedelta(minutes=3)
            else:
                self.assertEqual(row[0], "error")

    def test_newer_work_cannot_overtake_backing_off_older_run(self) -> None:
        first = self.enqueue()
        run = self.queue.claim()
        assert run is not None
        self.enqueue()
        self.assertTrue(self.queue.retry(run))
        self.assertIsNone(self.queue.claim())
        self.connection.now += timedelta(minutes=3)
        reclaimed = self.queue.claim()
        assert reclaimed is not None
        self.assertEqual(reclaimed.id, first)
        self.assertNotEqual(reclaimed.token, run.token)

    def test_recovery_can_complete_after_one_timeout(self) -> None:
        self.enqueue()
        with patch("myems_jishiyun.client.build_opener") as opener:
            opener.return_value.open.side_effect = TimeoutError()
            with self.assertRaises(RetryScheduled):
                execute_one(self.queue, BASE_URL, SYNTHETIC_KEY)
        self.connection.now += timedelta(minutes=3)
        records = [
            {"Address": "0001", "AmmeterID": 1, "ReadingDate": f"2026-10-01 12:{minute:02}", "ZYGDNSZ": 10}
            for minute in (0, 15, 30, 45)
        ]
        body = json.dumps({"IsSuccess": True, "Code": "0000", "Data": records, "Count": 4}).encode()
        with patch("myems_jishiyun.client.build_opener") as opener:
            opener.return_value.open.return_value = opened_response(body)
            result = execute_one(self.queue, BASE_URL, SYNTHETIC_KEY)
        assert result is not None
        self.assertEqual(result.status, "done")
        self.assertEqual(self.row("SELECT attempt_count, request_count, error_code FROM myems_system_db.tbl_jishiyun_runs"), (2, 2, None))

    def test_auth_and_contract_errors_are_terminal_without_retry(self) -> None:
        for failure in (http_error(401), http_error(403)):
            self.enqueue()
            with patch("myems_jishiyun.client.build_opener") as opener:
                opener.return_value.open.side_effect = failure
                with self.assertRaises(TransportError) as caught:
                    execute_one(self.queue, BASE_URL, SYNTHETIC_KEY)
            self.assertFalse(caught.exception.retryable)
        self.assertEqual(self.row("SELECT COUNT(*) FROM myems_system_db.tbl_jishiyun_runs WHERE status = 'error'")[0], 2)

    def test_retry_after_is_persisted_before_releasing_request_lock(self) -> None:
        self.enqueue()
        before = self.connection.now
        with patch("myems_jishiyun.client.build_opener") as opener:
            opener.return_value.open.side_effect = http_error(429, "3600")
            with self.assertRaises(RetryScheduled):
                execute_one(self.queue, BASE_URL, SYNTHETIC_KEY)
        row = self.row("SELECT next_request_datetime_utc, is_blocked FROM myems_system_db.tbl_jishiyun_request_quotas")
        self.assertGreaterEqual(str(row[0]), (before + timedelta(seconds=3600)).isoformat(" "))
        self.assertEqual(row[1], 0)
        self.assertEqual(self.connection.locks, set())
        self.assertEqual(self.row("SELECT status FROM myems_system_db.tbl_jishiyun_runs"), ("queued",))

    def test_http_date_cooldown_is_shared_without_echoing_provider_message(self) -> None:
        self.enqueue()
        now = self.connection.now.replace(tzinfo=START.tzinfo).timestamp()
        date = (self.connection.now + timedelta(minutes=20)).strftime("%a, %d %b %Y %H:%M:%S GMT")
        with patch("myems_jishiyun.client.time.time", return_value=now), patch("myems_jishiyun.client.build_opener") as opener:
            opener.return_value.open.side_effect = http_error(503, date)
            with self.assertRaises(RetryScheduled):
                execute_one(self.queue, BASE_URL, SYNTHETIC_KEY)
        self.assertEqual(
            self.row("SELECT next_request_datetime_utc FROM myems_system_db.tbl_jishiyun_request_quotas")[0],
            (self.connection.now + timedelta(minutes=20, seconds=1)).isoformat(" "),
        )

    def test_other_source_cannot_bypass_credential_cooldown(self) -> None:
        self.enqueue()
        run = self.queue.claim()
        assert run is not None
        with self.assertRaises(Exception):
            with SharedRequestGate(self.queue, run, SYNTHETIC_KEY)():
                raise http_error(429, "3600")
        self.connection.db.execute("INSERT INTO myems_system_db.tbl_data_sources VALUES (2)")
        self.connection.db.commit()
        other = RunQueue(lambda: self.connection, 2)
        with self.assertRaises(TransportError) as caught:
            with SharedRequestGate(other, run, SYNTHETIC_KEY)():
                self.fail("Must not reach request while the credential is cooling down")
        self.assertTrue(caught.exception.retryable)
        self.assertGreater(caught.exception.retry_after, 300)

    def test_malformed_provider_delay_quarantines_all_calls_for_key(self) -> None:
        self.enqueue()
        with patch("myems_jishiyun.client.build_opener") as opener:
            opener.return_value.open.side_effect = http_error(429, "not-a-delay")
            with self.assertRaises(TransportError):
                execute_one(self.queue, BASE_URL, SYNTHETIC_KEY)
        self.assertEqual(self.row("SELECT is_blocked FROM myems_system_db.tbl_jishiyun_request_quotas"), (1,))
        self.enqueue()
        with patch("myems_jishiyun.client.build_opener") as opener:
            with self.assertRaisesRegex(TransportError, "blocked"):
                execute_one(self.queue, BASE_URL, SYNTHETIC_KEY)
            opener.return_value.open.assert_not_called()

    def test_retry_cannot_mutate_cancelled_or_expired_run(self) -> None:
        self.enqueue()
        run = self.queue.claim()
        assert run is not None
        self.connection.now += timedelta(minutes=4)
        with self.assertRaises(PersistenceError):
            self.queue.retry(run)
        self.assertEqual(self.row("SELECT status FROM myems_system_db.tbl_jishiyun_runs"), ("running",))

    def test_retry_event_failure_rolls_back_requeue(self) -> None:
        self.enqueue()
        run = self.queue.claim()
        assert run is not None
        self.connection.fail_on = "INSERT INTO myems_system_db.tbl_jishiyun_events"
        with self.assertRaises(sqlite3.OperationalError):
            self.queue.retry(run)
        self.assertEqual(self.row("SELECT status, lease_token FROM myems_system_db.tbl_jishiyun_runs"), ("running", run.token))

    def test_invalid_retry_delays_fail_explicitly(self) -> None:
        for value in ("NaN", "Infinity", "9999999999999999999999999999999999999", "Thu, 01 Jan 2026 01:00:00"):
            with self.subTest(value=value), self.assertRaises(TransportError):
                retry_delay(value)
        self.assertEqual(retry_delay("3600"), 3600)
        self.assertEqual(retry_delay(""), 0)

    def test_quota_review_is_audited_and_does_not_erase_valid_cooldown(self) -> None:
        self.enqueue()
        run = self.queue.claim()
        assert run is not None
        with self.assertRaises(Exception):
            with SharedRequestGate(self.queue, run, SYNTHETIC_KEY)():
                raise http_error(429, "bad-delay")
        before = self.queue.quota_status(SYNTHETIC_KEY)
        self.assertTrue(before["is_blocked"])
        after = self.queue.quota_status(SYNTHETIC_KEY, reviewed_by=9)
        self.assertFalse(after["is_blocked"])
        self.assertEqual(before["next_request_datetime_utc"], after["next_request_datetime_utc"])
        self.assertEqual(
            self.row("SELECT actor_user_id FROM myems_system_db.tbl_jishiyun_events WHERE event_type = 'provider_quota_reviewed'"),
            (9,),
        )

    def test_quota_review_audit_failure_retains_block(self) -> None:
        self.enqueue()
        run = self.queue.claim()
        assert run is not None
        with self.assertRaises(Exception):
            with SharedRequestGate(self.queue, run, SYNTHETIC_KEY)():
                raise http_error(429, "bad-delay")
        self.connection.fail_on = "INSERT INTO myems_system_db.tbl_jishiyun_events"
        with self.assertRaises(sqlite3.OperationalError):
            self.queue.quota_status(SYNTHETIC_KEY, reviewed_by=9)
        self.connection.fail_on = None
        self.assertTrue(self.queue.quota_status(SYNTHETIC_KEY)["is_blocked"])


class BatchRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = ExecutionConnection()
        self.addCleanup(self.connection.db.close)
        self.queue = RunQueue(lambda: self.connection, 1)
        self.connection.now = NOW + timedelta(days=5)
        self.parent = str(uuid4())
        self.end = START + timedelta(days=2, hours=1)

    def submit(self) -> Iterator[int]:
        return enqueue_backfill(self.queue, 2, self.parent, START, self.end, 7)

    def test_progress_survives_interrupted_generator_and_resume(self) -> None:
        generator = self.submit()
        first = next(generator)
        generator.close()
        status = batch_status(self.queue, self.parent)
        self.assertEqual((status["expected_children"], status["submitted_children"], status["unsubmitted_children"]), (3, 1, 2))
        self.assertFalse(status["complete"])
        resumed = tuple(self.submit())
        self.assertEqual(resumed[0], first)
        self.assertEqual(batch_status(self.queue, self.parent)["submitted_children"], 3)

    def test_cancel_batch_fences_running_children_and_prevents_future_submissions(self) -> None:
        generator = self.submit()
        next(generator)
        run = self.queue.claim()
        assert run is not None
        cancelled = batch_status(self.queue, self.parent, cancel_by=8)
        self.assertTrue(cancelled["is_cancelled"])
        self.assertEqual(cancelled["child_status_counts"], {"cancelled": 1})
        with self.assertRaises(PersistenceError):
            next(generator)
        with self.assertRaises(PersistenceError):
            self.queue.renew(run)
        with self.assertRaises(PersistenceError):
            tuple(self.submit())

    def test_reusing_parent_with_longer_tail_cannot_append_silent_new_work(self) -> None:
        tuple(self.submit())
        with self.assertRaisesRegex(PersistenceError, "different submission"):
            tuple(enqueue_backfill(self.queue, 2, self.parent, START, self.end + timedelta(hours=1), 7))
        self.assertEqual(batch_status(self.queue, self.parent)["submitted_children"], 3)

    def test_cancel_is_idempotent_and_retains_completed_results(self) -> None:
        tuple(self.submit())
        self.connection.db.execute("UPDATE myems_system_db.tbl_jishiyun_runs SET status = 'done' WHERE id = 1")
        self.connection.db.commit()
        first = batch_status(self.queue, self.parent, cancel_by=7)
        second = batch_status(self.queue, self.parent, cancel_by=7)
        self.assertEqual(first, second)
        self.assertEqual(first["child_status_counts"], {"done": 1, "cancelled": 2})
        self.assertFalse(first["complete"])

    def test_all_done_is_required_for_complete_batch(self) -> None:
        tuple(self.submit())
        self.connection.db.execute("UPDATE myems_system_db.tbl_jishiyun_runs SET status = 'partial'")
        self.connection.db.commit()
        self.assertFalse(batch_status(self.queue, self.parent)["complete"])
        self.connection.db.execute("UPDATE myems_system_db.tbl_jishiyun_runs SET status = 'done'")
        self.connection.db.commit()
        self.assertTrue(batch_status(self.queue, self.parent)["complete"])

    def test_source_boundary_prevents_other_source_cancellation(self) -> None:
        tuple(self.submit())
        self.connection.db.execute("INSERT INTO myems_system_db.tbl_data_sources VALUES (2)")
        self.connection.db.commit()
        with self.assertRaises(PersistenceError):
            batch_status(RunQueue(lambda: self.connection, 2), self.parent, cancel_by=7)
        self.assertFalse(batch_status(self.queue, self.parent)["is_cancelled"])

    def test_cancel_audit_failure_rolls_back_batch_and_children(self) -> None:
        tuple(self.submit())
        self.connection.fail_on = "INSERT INTO myems_system_db.tbl_jishiyun_events"
        with self.assertRaises(sqlite3.OperationalError):
            batch_status(self.queue, self.parent, cancel_by=7)
        self.connection.fail_on = None
        status = batch_status(self.queue, self.parent)
        self.assertFalse(status["is_cancelled"])
        self.assertEqual(status["child_status_counts"], {"queued": 3})

    def test_cancelled_inflight_child_cannot_commit_raw_result(self) -> None:
        from myems_jishiyun.persistence import ResultStore

        tuple(self.submit())
        run = self.queue.claim()
        assert run is not None
        batch_status(self.queue, self.parent, cancel_by=7)
        with self.assertRaises(PersistenceError):
            ResultStore(self.connection).save(run.id, run.token, ())
        self.assertEqual(
            self.connection.db.execute("SELECT COUNT(*) FROM myems_historical_db.tbl_jishiyun_energy_readings").fetchone()[0],
            0,
        )


class RetryCommandTests(unittest.TestCase):
    def test_once_retry_uses_distinct_exit_not_success(self) -> None:
        environment = {
            "JISHIYUN_DATA_SOURCE_ID": "1", "JISHIYUN_BASE_URL": BASE_URL, "JISHIYUN_API_KEY": SYNTHETIC_KEY,
        }
        output = io.StringIO()
        with patch.dict("os.environ", environment, clear=True), patch("myems_jishiyun.worker.database_factory"), patch(
            "myems_jishiyun.worker.execute_one", side_effect=RetryScheduled("Retry queued"),
        ), patch("myems_jishiyun.worker.signal.signal"), redirect_stdout(output):
            self.assertEqual(main(["run", "--once"]), 3)
        self.assertEqual(json.loads(output.getvalue())["status"], "retry_scheduled")

    def test_continuous_worker_waits_on_durable_retry_without_reporting_success(self) -> None:
        environment = {
            "JISHIYUN_DATA_SOURCE_ID": "1", "JISHIYUN_BASE_URL": BASE_URL, "JISHIYUN_API_KEY": SYNTHETIC_KEY,
        }
        output = io.StringIO()
        with patch.dict("os.environ", environment, clear=True), patch("myems_jishiyun.worker.database_factory"), patch(
            "myems_jishiyun.worker.execute_one", side_effect=RetryScheduled("Retry queued"),
        ), patch("myems_jishiyun.worker.signal.signal"), patch("myems_jishiyun.worker.threading.Event") as stop, patch(
            "myems_jishiyun.worker.record_health",
        ) as heartbeat, redirect_stdout(output):
            stop.return_value.is_set.side_effect = (False, True)
            self.assertEqual(main(["run"]), 0)
            stop.return_value.wait.assert_called_once_with(5)
            heartbeat.assert_not_called()
        self.assertEqual(json.loads(output.getvalue())["completed_run"], False)


if __name__ == "__main__":
    unittest.main()
