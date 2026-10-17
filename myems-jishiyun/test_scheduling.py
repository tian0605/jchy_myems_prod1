import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator
from unittest.mock import patch
from uuid import uuid4

from myems_jishiyun.contracts import ContractError
from myems_jishiyun.execution import RunQueue, execute_one
from myems_jishiyun.health import MAX_AGE_SECONDS, clear_health, healthy, record_health
from myems_jishiyun.persistence import PersistenceError, Result
from myems_jishiyun.scheduling import enqueue_backfill, schedule_due, scheduled_window, split_backfill
from test_client import BASE_URL, SYNTHETIC_KEY, opened_response
from test_execution import ExecutionConnection
from test_persistence import END, NOW, START
from myems_jishiyun.worker import main


class WindowTests(unittest.TestCase):
    def test_at_minute_40_collects_previous_hour_with_lookback(self) -> None:
        now = datetime(2026, 10, 1, 5, 40, tzinfo=timezone.utc)
        window = scheduled_window(now, 480, 40, 40, 3)
        self.assertEqual(window.end, datetime(2026, 10, 1, 5, tzinfo=timezone.utc))
        self.assertEqual(window.start, window.end - timedelta(hours=3))
        self.assertEqual(window.tick, now)

    def test_before_schedule_minute_uses_last_due_tick(self) -> None:
        before = datetime(2026, 10, 1, 5, 39, 59, tzinfo=timezone.utc)
        window = scheduled_window(before, 480, 40, 40, 1)
        self.assertEqual(window.end, datetime(2026, 10, 1, 4, tzinfo=timezone.utc))
        self.assertEqual(window.tick, datetime(2026, 10, 1, 4, 40, tzinfo=timezone.utc))

    def test_delay_exceeding_schedule_minute_never_collects_undue_hour(self) -> None:
        now = datetime(2026, 10, 1, 5, 40, tzinfo=timezone.utc)
        window = scheduled_window(now, 480, 40, 50, 1)
        self.assertEqual(window.end, datetime(2026, 10, 1, 4, tzinfo=timezone.utc))
        self.assertLessEqual(window.end + timedelta(minutes=50), now)

    def test_half_hour_timezone_and_midnight(self) -> None:
        now = datetime(2026, 10, 1, 18, 10, tzinfo=timezone.utc)  # 23:40 at +05:30
        window = scheduled_window(now, 330, 40, 40, 1)
        self.assertEqual(window.end, datetime(2026, 10, 1, 17, 30, tzinfo=timezone.utc))
        next_window = scheduled_window(now + timedelta(hours=1), 330, 40, 40, 1)
        self.assertEqual(next_window.start, window.end)

    def test_invalid_settings_fail_explicitly(self) -> None:
        cases = (
            (841, 40, 40, 1), (1, 40, 40, 1), (480, 60, 40, 1),
            (480, -1, 40, 1), (480, 40, -1, 1), (480, 40, 40, 0),
            (480, 40, 40, 25), (True, 40, 40, 1), (480, True, 40, 1),
        )
        for values in cases:
            with self.subTest(values=values), self.assertRaises(ContractError):
                scheduled_window(START, *values)
        with self.assertRaises(ContractError):
            scheduled_window(START.replace(tzinfo=None), 480, 40, 40, 1)

    def test_backfill_split_is_gapless_and_preserves_exact_partial_tail(self) -> None:
        end = START + timedelta(days=2, minutes=45)
        windows = split_backfill(START, end)
        self.assertEqual(len(windows), 3)
        self.assertEqual(windows[0][0], START)
        self.assertEqual(windows[-1][1], end)
        self.assertTrue(all(left[1] == right[0] for left, right in zip(windows, windows[1:])))
        self.assertTrue(all(timedelta(0) < end - start <= timedelta(days=1) for start, end in windows))

    def test_backfill_bounds_and_utc_conversion(self) -> None:
        self.assertEqual(len(split_backfill(START, START + timedelta(days=31))), 31)
        local = START.astimezone(timezone(timedelta(hours=8)))
        self.assertEqual(split_backfill(local, local + timedelta(hours=1)), ((START, END),))
        for start, end in ((START, START), (END, START), (START, START + timedelta(days=31, minutes=15)),
                           (START.replace(tzinfo=None), END), (START + timedelta(minutes=1), END)):
            with self.subTest(start=start, end=end), self.assertRaises(ContractError):
                split_backfill(start, end)


class ScheduleQueueTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = ExecutionConnection()
        self.addCleanup(self.connection.db.close)
        self.queue = RunQueue(lambda: self.connection, 1)
        self.connection.now = datetime(2026, 10, 1, 5, 40)

    def count(self) -> int:
        return self.connection.db.execute("SELECT COUNT(*) FROM myems_system_db.tbl_jishiyun_runs").fetchone()[0]

    def test_repeated_ticks_and_new_scheduler_instance_do_not_duplicate(self) -> None:
        first = schedule_due(self.queue)
        self.assertEqual(len(first), 1)
        self.assertEqual(schedule_due(self.queue), first)
        self.assertEqual(schedule_due(RunQueue(lambda: self.connection, 1)), first)
        self.assertEqual(self.count(), 1)
        row = self.connection.db.execute(
            "SELECT requested_by_user_id, window_start_utc, window_end_utc FROM myems_system_db.tbl_jishiyun_runs"
        ).fetchone()
        self.assertEqual(row, (None, START.replace(tzinfo=None).isoformat(" "), END.replace(tzinfo=None).isoformat(" ")))

    def test_next_hour_creates_next_window_and_pause_stops_submissions(self) -> None:
        first = schedule_due(self.queue)
        self.connection.now += timedelta(hours=1)
        second = schedule_due(self.queue)
        self.assertNotEqual(first, second)
        self.assertEqual(self.count(), 2)
        self.connection.db.execute("UPDATE myems_system_db.tbl_jishiyun_tasks SET is_enabled = 0")
        self.connection.db.commit()
        self.connection.now += timedelta(hours=1)
        self.assertEqual(schedule_due(self.queue), ())
        self.assertEqual(self.count(), 2)

    def test_scheduled_tick_runs_through_mocked_provider_and_is_not_requeued(self) -> None:
        run_ids = schedule_due(self.queue)
        records = [
            {"Address": "0001", "AmmeterID": 1, "ReadingDate": f"2026-10-01 12:{minute:02}", "ZYGDNSZ": "10"}
            for minute in (0, 15, 30, 45)
        ]
        body = json.dumps({"IsSuccess": True, "Code": "0000", "Data": records, "Count": 4}).encode()
        with patch("myems_jishiyun.client.build_opener") as opener:
            opener.return_value.open.return_value = opened_response(body)
            result = execute_one(self.queue, BASE_URL, SYNTHETIC_KEY)
        assert result is not None
        self.assertEqual((result.status, result.expected_count, result.inserted_count), ("done", 4, 4))
        self.assertEqual(schedule_due(self.queue), run_ids)
        self.assertIsNone(self.queue.claim())
        self.assertEqual(self.count(), 1)

    def test_outage_only_schedules_latest_tick_not_unbounded_history(self) -> None:
        schedule_due(self.queue)
        self.connection.now += timedelta(days=10)
        schedule_due(self.queue)
        self.assertEqual(self.count(), 2)

    def test_invalid_schedule_does_not_create_a_guessed_window(self) -> None:
        self.connection.db.execute("UPDATE myems_system_db.tbl_jishiyun_tasks SET schedule_minute = 90 WHERE id = 1")
        self.connection.db.commit()
        with self.assertRaises(ContractError):
            schedule_due(self.queue)
        self.assertEqual(self.count(), 0)

    def test_same_tick_changed_window_is_rejected_not_silently_rewritten(self) -> None:
        schedule_due(self.queue)
        self.connection.db.execute("UPDATE myems_system_db.tbl_jishiyun_tasks SET lookback_hours = 2 WHERE id = 1")
        self.connection.db.commit()
        with self.assertRaisesRegex(PersistenceError, "different submission"):
            schedule_due(self.queue)
        self.assertEqual(self.count(), 1)

    def test_empty_scope_does_not_produce_success_shaped_schedule(self) -> None:
        self.connection.db.execute("UPDATE myems_system_db.tbl_jishiyun_meter_mappings SET configuration_status = 'pending'")
        self.connection.db.commit()
        with self.assertRaises(ContractError):
            schedule_due(self.queue)
        self.assertEqual(self.count(), 0)

    def test_backfill_replay_resumes_committed_prefix(self) -> None:
        self.connection.now = NOW + timedelta(days=4)
        parent = str(uuid4())
        end = START + timedelta(days=2, hours=1)
        original = self.queue.enqueue
        calls = 0

        def fail_second(
            task_id: int, key: str, start: datetime, finish: datetime, requested_by: int | None,
            *, batch_key: str | None = None,
        ) -> int:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise sqlite3.OperationalError("Injected enqueue failure")
            return original(task_id, key, start, finish, requested_by, batch_key=batch_key)

        ids: list[int] = []
        with patch.object(self.queue, "enqueue", side_effect=fail_second):
            with self.assertRaises(sqlite3.OperationalError):
                for run_id in enqueue_backfill(self.queue, 2, parent, START, end, 7):
                    ids.append(run_id)
        self.assertEqual(len(ids), 1)
        self.assertEqual(self.count(), 1)
        resumed = tuple(enqueue_backfill(self.queue, 2, parent, START, end, 7))
        self.assertEqual(len(resumed), 3)
        self.assertEqual(resumed[0], ids[0])
        self.assertEqual(self.count(), 3)
        self.assertEqual(tuple(enqueue_backfill(self.queue, 2, parent, START, end, 7)), resumed)

    def test_future_tail_is_rejected_before_any_backfill_child_is_created(self) -> None:
        with self.assertRaisesRegex(PersistenceError, "Entire backfill"):
            tuple(enqueue_backfill(self.queue, 2, str(uuid4()), START, START + timedelta(days=2), 7))
        self.assertEqual(self.count(), 0)

    def test_backfill_requires_backfill_task_and_conflicting_parent_reuse_fails(self) -> None:
        with self.assertRaisesRegex(PersistenceError, "energy_backfill"):
            tuple(enqueue_backfill(self.queue, 1, str(uuid4()), START, END, 7))
        parent = str(uuid4())
        tuple(enqueue_backfill(self.queue, 2, parent, START, END, 7))
        with self.assertRaisesRegex(PersistenceError, "different submission"):
            tuple(enqueue_backfill(self.queue, 2, parent, START + timedelta(minutes=15), END, 7))


class HealthTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "heartbeat.json"

    def test_health_missing_fresh_stale_and_cleared(self) -> None:
        self.assertFalse(healthy(self.path))
        with patch("myems_jishiyun.health.time.monotonic", return_value=1000):
            record_health(self.path)
            self.assertTrue(healthy(self.path))
        with patch("myems_jishiyun.health.time.monotonic", return_value=1000 + MAX_AGE_SECONDS):
            self.assertTrue(healthy(self.path))
        with patch("myems_jishiyun.health.time.monotonic", return_value=1001 + MAX_AGE_SECONDS):
            self.assertFalse(healthy(self.path))
        clear_health(self.path)
        clear_health(self.path)
        self.assertFalse(healthy(self.path))

    def test_bad_or_future_heartbeat_is_unhealthy(self) -> None:
        for value in ("invalid", "[]", '{"version":2}', '{"version":1,"pid":true,"monotonic":1}',
                      '{"version":true,"pid":1,"monotonic":1}',
                      '{"version":1,"pid":1,"monotonic":NaN}', '{"version":1,"pid":1,"monotonic":Infinity}'):
            self.path.write_text(value, encoding="ascii")
            with self.subTest(value=value):
                self.assertFalse(healthy(self.path))
        with patch("myems_jishiyun.health.time.monotonic", return_value=1000):
            record_health(self.path)
        with patch("myems_jishiyun.health.time.monotonic", return_value=999):
            self.assertFalse(healthy(self.path))

    def test_health_write_failure_is_not_ignored(self) -> None:
        with self.assertRaises(OSError):
            record_health(self.path / "missing" / "heartbeat.json")


class ScheduledWorkerTests(unittest.TestCase):
    environment = {
        "JISHIYUN_DATA_SOURCE_ID": "1", "JISHIYUN_BASE_URL": BASE_URL, "JISHIYUN_API_KEY": SYNTHETIC_KEY,
    }

    def test_schedule_is_opt_in_and_precedes_claim(self) -> None:
        environment = {
            "JISHIYUN_DATA_SOURCE_ID": "1", "JISHIYUN_BASE_URL": BASE_URL, "JISHIYUN_API_KEY": SYNTHETIC_KEY,
        }
        for enabled in (False, True):
            calls: list[str] = []
            with patch.dict("os.environ", environment, clear=True), patch("myems_jishiyun.worker.database_factory"), patch(
                "myems_jishiyun.worker.schedule_due", side_effect=lambda _: calls.append("schedule"),
            ), patch("myems_jishiyun.worker.execute_one", side_effect=lambda *_: calls.append("execute")), patch(
                "myems_jishiyun.worker.signal.signal",
            ), redirect_stdout(io.StringIO()):
                self.assertEqual(main(["run", "--once"] + (["--schedule"] if enabled else [])), 0)
            self.assertEqual(calls, ["schedule", "execute"] if enabled else ["execute"])

    def test_failed_loop_does_not_write_health_and_clears_old_marker(self) -> None:
        environment = {
            "JISHIYUN_DATA_SOURCE_ID": "1", "JISHIYUN_BASE_URL": BASE_URL, "JISHIYUN_API_KEY": SYNTHETIC_KEY,
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "heartbeat.json"
            record_health(path)
            with patch.dict("os.environ", environment, clear=True), patch("myems_jishiyun.worker.database_factory"), patch(
                "myems_jishiyun.worker.execute_one", side_effect=PersistenceError("Synthetic DB outage"),
            ), patch("myems_jishiyun.worker.signal.signal"), patch("myems_jishiyun.worker.record_health") as record, redirect_stderr(io.StringIO()):
                self.assertEqual(main(["run", "--once", "--health-file", str(path)]), 1)
                record.assert_not_called()
            self.assertFalse(path.exists())

    def test_partial_data_can_have_fresh_loop_health_but_returns_partial_exit(self) -> None:
        result = Result("partial", 4, 3, 3, 3, 0, 0, 0, 1, 0)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "heartbeat.json"
            with patch.dict("os.environ", self.environment, clear=True), patch("myems_jishiyun.worker.database_factory"), patch(
                "myems_jishiyun.worker.execute_one", return_value=result,
            ), patch("myems_jishiyun.worker.signal.signal"), patch("myems_jishiyun.worker.record_health", wraps=record_health) as record, redirect_stdout(io.StringIO()):
                self.assertEqual(main(["run", "--once", "--health-file", str(path)]), 2)
                record.assert_called_once_with(path)
            self.assertFalse(path.exists())  # A one-shot process is not a healthy service.

    def test_idle_continuous_loop_updates_health_and_cleans_after_stop(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "heartbeat.json"
            with patch.dict("os.environ", self.environment, clear=True), patch("myems_jishiyun.worker.database_factory"), patch(
                "myems_jishiyun.worker.execute_one", return_value=None,
            ), patch("myems_jishiyun.worker.signal.signal"), patch("myems_jishiyun.worker.threading.Event") as event:
                event.return_value.is_set.side_effect = (False, True)
                event.return_value.wait.side_effect = lambda _: self.assertTrue(healthy(path))
                self.assertEqual(main(["run", "--health-file", str(path)]), 0)
            self.assertFalse(path.exists())

    def test_backfill_cli_reports_committed_prefix_before_later_failure(self) -> None:
        def committed_prefix(*_args: object) -> Iterator[int]:
            yield 17
            raise PersistenceError("Synthetic later chunk failure")

        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.dict("os.environ", self.environment, clear=True), patch("myems_jishiyun.worker.database_factory"), patch(
            "myems_jishiyun.worker.enqueue_backfill", side_effect=committed_prefix,
        ), redirect_stdout(stdout), redirect_stderr(stderr):
            self.assertEqual(main([
                "backfill", "--task-id", "2", "--request-key", str(uuid4()), "--requested-by", "7",
                "--start", START.isoformat(), "--end", END.isoformat(),
            ]), 1)
        self.assertEqual(json.loads(stdout.getvalue())["run_id"], 17)
        self.assertEqual(json.loads(stderr.getvalue())["status"], "error")


if __name__ == "__main__":
    unittest.main()
