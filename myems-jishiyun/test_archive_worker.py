import io
import json
import sqlite3
import unittest
from contextlib import redirect_stdout
from datetime import timedelta
from unittest.mock import patch
from uuid import uuid4

from myems_jishiyun.contracts import ContractError, parse_archives
from datetime import timezone
from myems_jishiyun.execution import ArchiveOutcome, LEASE, RetryScheduled, execute_one
from myems_jishiyun.persistence import Cursor, PersistenceError
from test_client import BASE_URL, SYNTHETIC_KEY, opened_response
from test_initialization import InitializationFixture, archive, configuration
from myems_jishiyun.worker import main


def response(*rows: dict[str, object]) -> bytes:
    return json.dumps({"IsSuccess": True, "Code": "0000", "Data": list(rows), "Count": len(rows)}).encode()


class ArchiveWorkerTests(InitializationFixture):
    def setUp(self) -> None:
        super().setUp()
        sleep = patch("myems_jishiyun.execution.time.sleep", side_effect=lambda seconds: setattr(
            self.connection, "now", self.connection.now + timedelta(seconds=seconds),
        ))
        sleep.start()
        self.addCleanup(sleep.stop)

    def scalar(self, sql: str, params: tuple[object, ...] = ()) -> object:
        return self.connection.db.execute(sql, params).fetchone()[0]

    def test_fetch_is_queued_without_http_or_business_objects(self) -> None:
        key = str(uuid4())
        with patch("myems_jishiyun.client.build_opener") as opener:
            run_id = self.queue.enqueue_archive(3, key, 7)
            self.assertEqual(self.queue.enqueue_archive(3, key, 7), run_id)
            opener.assert_not_called()
        self.assertEqual(self.count("tbl_meters"), 0)
        self.assertEqual(self.count("tbl_jishiyun_run_items"), 0)
        with self.assertRaises(PersistenceError):
            self.queue.enqueue_archive(3, key, 8)

    def test_fetch_dispatch_uses_archives_endpoint_and_shared_quota_then_waits_for_approval(self) -> None:
        key = str(uuid4())
        run_id = self.queue.enqueue_archive(3, key, 7)
        with patch("myems_jishiyun.client.build_opener") as opener:
            opener.return_value.open.return_value = opened_response(response(archive(), archive("0002", 2)))
            result = execute_one(self.queue, BASE_URL, SYNTHETIC_KEY)
            request = opener.return_value.open.call_args.args[0]
            self.assertTrue(request.full_url.endswith("/NH2/AmmeterBasicInfo"))
        self.assertEqual(result, ArchiveOutcome("preview", run_id, archive_count=2))
        self.assertEqual(self.scalar("SELECT status FROM myems_system_db.tbl_jishiyun_runs WHERE id = ?", (run_id,)), "preview")
        self.assertEqual(self.scalar("SELECT request_count FROM myems_system_db.tbl_jishiyun_runs WHERE id = ?", (run_id,)), 1)
        self.assertEqual(self.count("tbl_jishiyun_run_items"), 2)
        self.assertEqual(self.count("tbl_meters"), 0)
        self.assertIsNone(self.queue.claim())
        self.assertEqual(self.queue.enqueue_archive(3, key, 7), run_id)

    def test_fetched_archive_dates_are_validated_in_explicit_provider_timezone(self) -> None:
        self.queue.enqueue_archive(3, str(uuid4()), 7)
        with patch("myems_jishiyun.client.build_opener") as opener:
            opener.return_value.open.return_value = opened_response(response(archive()))
            result = execute_one(self.queue, BASE_URL, SYNTHETIC_KEY)
        assert isinstance(result, ArchiveOutcome)
        # Raw provider dates are retained; revalidation never mutates those dates.
        item = self.connection.db.execute("SELECT archive_json FROM myems_system_db.tbl_jishiyun_run_items").fetchone()
        self.assertEqual(json.loads(item[0])["AddDate"], "2026-10-01 00:00:00")

    def test_bad_archive_response_is_durable_failure_not_empty_preview(self) -> None:
        run_id = self.queue.enqueue_archive(3, str(uuid4()), 7)
        with patch("myems_jishiyun.client.build_opener") as opener:
            opener.return_value.open.return_value = opened_response(b"invalid")
            with self.assertRaises(ContractError):
                execute_one(self.queue, BASE_URL, SYNTHETIC_KEY)
        self.assertEqual(self.scalar("SELECT status FROM myems_system_db.tbl_jishiyun_runs WHERE id = ?", (run_id,)), "error")
        self.assertEqual(self.count("tbl_jishiyun_run_items"), 0)

    def test_empty_archive_result_is_preview_not_initialization_success(self) -> None:
        run_id = self.queue.enqueue_archive(3, str(uuid4()), 7)
        with patch("myems_jishiyun.client.build_opener") as opener:
            opener.return_value.open.return_value = opened_response(response())
            self.assertEqual(execute_one(self.queue, BASE_URL, SYNTHETIC_KEY), ArchiveOutcome("preview", run_id))
        with self.assertRaises(ContractError):
            self.service.submit_approved(run_id, (), 7)

    def test_fetch_timeout_uses_same_durable_retry_mechanism(self) -> None:
        run_id = self.queue.enqueue_archive(3, str(uuid4()), 7)
        with patch("myems_jishiyun.client.build_opener") as opener:
            opener.return_value.open.side_effect = TimeoutError("Synthetic timeout")
            with self.assertRaises(RetryScheduled):
                execute_one(self.queue, BASE_URL, SYNTHETIC_KEY)
        self.assertEqual(self.scalar("SELECT status FROM myems_system_db.tbl_jishiyun_runs WHERE id = ?", (run_id,)), "queued")
        self.assertEqual(self.count("tbl_jishiyun_run_items"), 0)

    def test_cancelled_fetch_token_cannot_save_preview(self) -> None:
        self.queue.enqueue_archive(3, str(uuid4()), 7)
        run = self.queue.claim()
        assert run is not None
        self.connection.db.execute("UPDATE myems_system_db.tbl_jishiyun_runs SET status = 'cancelled' WHERE id = ?", (run.id,))
        self.connection.db.commit()
        with self.assertRaises(PersistenceError):
            self.service.save_fetched(run, parse_archives(response(archive()).decode(), timezone.utc))
        self.assertEqual(self.count("tbl_jishiyun_run_items"), 0)

    def test_fetch_commit_failure_rolls_back_all_preview_items(self) -> None:
        self.queue.enqueue_archive(3, str(uuid4()), 7)
        run = self.queue.claim()
        assert run is not None
        self.connection.fail_on = "INSERT INTO myems_system_db.tbl_jishiyun_events"
        with self.assertRaises(sqlite3.OperationalError):
            self.service.save_fetched(run, parse_archives(response(archive(), archive("0002", 2)).decode(), timezone.utc))
        self.assertEqual(self.count("tbl_jishiyun_run_items"), 0)
        self.assertEqual(self.scalar("SELECT status FROM myems_system_db.tbl_jishiyun_runs WHERE id=?", (run.id,)), "running")

    def test_fetch_lease_expiring_before_final_update_rolls_back_preview(self) -> None:
        self.queue.enqueue_archive(3, str(uuid4()), 7)
        run = self.queue.claim()
        assert run is not None
        self.connection.expire_on_finish = True
        with self.assertRaises(PersistenceError):
            self.service.save_fetched(run, parse_archives(response(archive()).decode(), timezone.utc))
        self.assertEqual(self.count("tbl_jishiyun_run_items"), 0)

    def test_paused_or_wrong_task_cannot_enqueue_or_claim_archive_fetch(self) -> None:
        with self.assertRaises(PersistenceError):
            self.queue.enqueue_archive(1, str(uuid4()), 7)
        run_id = self.queue.enqueue_archive(3, str(uuid4()), 7)
        self.connection.db.execute("UPDATE myems_system_db.tbl_jishiyun_tasks SET is_enabled=0 WHERE id=3")
        self.connection.db.commit()
        self.assertIsNone(self.queue.claim())
        with self.assertRaises(PersistenceError):
            self.queue.enqueue_archive(3, str(uuid4()), 7)
        self.assertEqual(self.scalar("SELECT status FROM myems_system_db.tbl_jishiyun_runs WHERE id=?", (run_id,)), "queued")

    def test_submit_selected_items_is_idempotent_and_preserves_unselected_preview(self) -> None:
        self.preview(archive(), archive("0002", 2))
        self.service.approve(10, "0001", configuration(), 7)
        self.service.submit_approved(10, ("0001",), 7)
        self.service.submit_approved(10, ("0001",), 7)
        with self.assertRaises(PersistenceError):
            self.service.submit_approved(10, ("0002",), 7)
        with patch("myems_jishiyun.client.build_opener") as opener:
            result = execute_one(self.queue, BASE_URL, SYNTHETIC_KEY)
            opener.assert_not_called()
        self.assertEqual(result, ArchiveOutcome("done", 10, created_count=1))
        self.assertEqual(self.count("tbl_meters"), 1)
        self.assertEqual(self.scalar(
            "SELECT status FROM myems_system_db.tbl_jishiyun_run_items WHERE external_address='0002'",
        ), "preview")
        self.service.submit_approved(10, ("0001",), 7)  # Lost-response submission replay.
        self.assertIsNone(self.queue.claim())

    def test_unapproved_or_duplicate_or_nonexistent_selection_is_rejected(self) -> None:
        self.preview(archive())
        for addresses in (("0001",), ("unknown",), ("0001", "0001")):
            with self.subTest(addresses=addresses), self.assertRaises((PersistenceError, ContractError)):
                self.service.submit_approved(10, addresses, 7)
        self.assertEqual(self.scalar("SELECT status FROM myems_system_db.tbl_jishiyun_runs WHERE id=10"), "preview")

    def test_queued_approval_cannot_be_changed_or_executed_by_direct_cli(self) -> None:
        self.approved()
        self.service.submit_approved(10, ("0001",), 7)
        with self.assertRaises(PersistenceError):
            self.service.approve(10, "0001", configuration("changed"), 8)
        with self.assertRaises(PersistenceError):
            self.service.initialize(10, "0001", 7)

    def test_conflict_is_saved_per_item_and_other_device_continues(self) -> None:
        self.preview(archive(), archive("0002", 2))
        self.service.approve(10, "0001", configuration("Existing name"), 7)
        self.service.approve(10, "0002", configuration("Second name"), 7)
        self.connection.db.execute("INSERT INTO myems_system_db.tbl_meters(name) VALUES ('Existing name')")
        self.connection.db.commit()
        self.service.submit_approved(10, ("0001", "0002"), 7)
        result = execute_one(self.queue, BASE_URL, SYNTHETIC_KEY)
        self.assertEqual(result, ArchiveOutcome("partial", 10, created_count=1, conflict_count=1))
        self.assertEqual(self.scalar(
            "SELECT status FROM myems_system_db.tbl_jishiyun_run_items WHERE external_address='0001'",
        ), "conflict")
        self.assertEqual(self.count("tbl_jishiyun_meter_mappings"), 1)
        message = self.scalar(
            "SELECT error_message FROM myems_system_db.tbl_jishiyun_run_items WHERE external_address='0001'",
        )
        self.assertNotIn("Existing name", str(message))

    def test_missing_business_reference_after_submission_is_saved_as_item_error(self) -> None:
        self.approved()
        self.service.submit_approved(10, ("0001",), 7)
        self.connection.db.execute("DELETE FROM myems_system_db.tbl_cost_centers")
        self.connection.db.commit()
        result = execute_one(self.queue, BASE_URL, SYNTHETIC_KEY)
        self.assertEqual(result, ArchiveOutcome("partial", 10, error_count=1))
        self.assertEqual(self.count("tbl_meters"), 0)

    def test_database_failure_records_item_error_then_exits_without_fake_batch_success(self) -> None:
        self.approved()
        self.service.submit_approved(10, ("0001",), 7)
        self.connection.fail_on = "INSERT INTO myems_system_db.tbl_points"
        with self.assertRaises(sqlite3.OperationalError):
            execute_one(self.queue, BASE_URL, SYNTHETIC_KEY)
        self.assertEqual(self.scalar("SELECT status FROM myems_system_db.tbl_jishiyun_runs WHERE id=10"), "error")
        self.assertEqual(self.scalar("SELECT status FROM myems_system_db.tbl_jishiyun_run_items"), "error")
        self.assertEqual(self.count("tbl_meters"), 0)

    def test_crash_recovery_replays_completed_items_without_recreating(self) -> None:
        self.preview(archive(), archive("0002", 2))
        for address in ("0001", "0002"):
            self.service.approve(10, address, configuration(address), 7)
        self.service.submit_approved(10, ("0001", "0002"), 7)
        old = self.queue.claim()
        assert old is not None
        self.service.initialize(10, "0001", 7, lease=old)
        self.connection.now += LEASE
        new = self.queue.claim()
        assert new is not None
        with self.assertRaises(PersistenceError):
            self.service.initialize(10, "0002", 7, lease=old)
        outcome = self.service.execute_approved(new)
        self.assertEqual(outcome, ArchiveOutcome("done", 10, created_count=2))
        self.assertEqual(self.count("tbl_meters"), 2)
        self.assertEqual(self.count("tbl_points"), 2)

    def test_cancelled_initialization_cannot_create_objects_or_record_false_item_error(self) -> None:
        self.approved()
        self.service.submit_approved(10, ("0001",), 7)
        run = self.queue.claim()
        assert run is not None
        self.connection.db.execute("UPDATE myems_system_db.tbl_jishiyun_runs SET status='cancelled' WHERE id=10")
        self.connection.db.commit()
        with self.assertRaises(PersistenceError):
            self.service.execute_approved(run)
        self.assertEqual(self.count("tbl_meters"), 0)
        self.assertEqual(self.scalar("SELECT status FROM myems_system_db.tbl_jishiyun_run_items"), "pending")

    def test_fetch_request_key_replay_remains_valid_after_initialization_phase_changes(self) -> None:
        key = str(uuid4())
        run_id = self.queue.enqueue_archive(3, key, 7)
        with patch("myems_jishiyun.client.build_opener") as opener:
            opener.return_value.open.return_value = opened_response(response(archive()))
            execute_one(self.queue, BASE_URL, SYNTHETIC_KEY)
        self.service.approve(run_id, "0001", configuration(), 7)
        self.service.submit_approved(run_id, ("0001",), 7)
        self.assertEqual(self.queue.enqueue_archive(3, key, 7), run_id)

    def test_submission_audit_failure_keeps_editable_preview(self) -> None:
        self.approved()
        self.connection.fail_on = "INSERT INTO myems_system_db.tbl_jishiyun_events"
        with self.assertRaises(sqlite3.OperationalError):
            self.service.submit_approved(10, ("0001",), 7)
        self.assertEqual(self.scalar("SELECT status FROM myems_system_db.tbl_jishiyun_runs WHERE id=10"), "preview")

    def test_fetch_approve_submit_execute_full_flow_has_one_http_call(self) -> None:
        run_id = self.queue.enqueue_archive(3, str(uuid4()), 7)
        with patch("myems_jishiyun.client.build_opener") as opener:
            opener.return_value.open.return_value = opened_response(response(archive(), archive("0002", 2)))
            first = execute_one(self.queue, BASE_URL, SYNTHETIC_KEY)
            self.assertEqual(first, ArchiveOutcome("preview", run_id, archive_count=2))
            for address in ("0001", "0002"):
                self.service.approve(run_id, address, configuration(address), 7)
            self.service.submit_approved(run_id, ("0001", "0002"), 7)
            second = execute_one(self.queue, BASE_URL, SYNTHETIC_KEY)
            opener.return_value.open.assert_called_once()
        self.assertEqual(second, ArchiveOutcome("done", run_id, created_count=2))
        self.assertEqual(self.count("tbl_meters"), 2)
        self.assertEqual(self.count("tbl_jishiyun_meter_mappings"), 2)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM myems_system_db.tbl_jishiyun_meter_mappings WHERE is_enabled=1"), 0)

    def test_completed_partial_items_survive_crash_reclaim_and_are_counted_once(self) -> None:
        self.preview(archive(), archive("0002", 2))
        for address in ("0001", "0002"):
            self.service.approve(10, address, configuration(address), 7)
        self.service.submit_approved(10, ("0001", "0002"), 7)
        old = self.queue.claim()
        assert old is not None
        self.service._item_failed(old, "0001", "conflict", 7)
        self.connection.now += LEASE
        new = self.queue.claim()
        assert new is not None
        self.assertEqual(
            self.service.execute_approved(new), ArchiveOutcome("partial", 10, created_count=1, conflict_count=1),
        )
        self.assertEqual(self.count("tbl_meters"), 1)

    def test_progress_is_paged_and_distinguishes_all_preview_items_from_selected_count(self) -> None:
        self.preview(archive(), archive("0002", 2))
        self.service.approve(10, "0001", configuration(), 7)
        self.service.submit_approved(10, ("0001",), 7)
        execute_one(self.queue, BASE_URL, SYNTHETIC_KEY)
        first = self.service.progress(10, limit=1)
        self.assertEqual(first["status"], "done")
        self.assertEqual(first["selected_count"], 1)
        self.assertEqual(first["successful_selected_count"], 1)
        self.assertEqual(first["all_preview_item_status_counts"], {"created": 1, "preview": 1})
        self.assertTrue(first["has_more"])
        second = self.service.progress(10, after_id=int(str(first["next_after_id"])), limit=1)
        self.assertFalse(second["has_more"])
        self.assertNotEqual(first["items"], second["items"])

    def test_progress_rejects_wrong_source_and_invalid_pagination(self) -> None:
        self.preview(archive())
        for after_id, limit in ((-1, 1), (True, 1), (0, 0), (0, 201)):
            with self.subTest(after_id=after_id, limit=limit), self.assertRaises(ContractError):
                self.service.progress(10, after_id=after_id, limit=limit)
        self.connection.db.execute("UPDATE myems_system_db.tbl_jishiyun_runs SET data_source_id=2 WHERE id=10")
        self.connection.db.commit()
        with self.assertRaises(PersistenceError):
            self.service.progress(10)

    def test_item_lease_expiring_after_writes_rolls_back_all_new_objects(self) -> None:
        self.approved()
        self.service.submit_approved(10, ("0001",), 7)
        run = self.queue.claim()
        assert run is not None
        original = self.queue.event

        def expire_after_event(cursor: Cursor, run_id: int, kind: str, details: dict[str, object]) -> None:
            original(cursor, run_id, kind, details)
            if kind == "archive_item_initialized":
                self.connection.now += LEASE

        with patch.object(self.queue, "event", side_effect=expire_after_event):
            with self.assertRaises(PersistenceError):
                self.service.initialize(10, "0001", 7, lease=run)
        self.assertEqual(self.count("tbl_meters"), 0)
        self.assertEqual(self.count("tbl_points"), 0)
        self.assertEqual(self.count("tbl_jishiyun_meter_mappings"), 0)
        self.assertEqual(self.scalar("SELECT status FROM myems_system_db.tbl_jishiyun_run_items"), "pending")


class ArchiveWorkerCommandTests(unittest.TestCase):
    environment = {"JISHIYUN_DATA_SOURCE_ID": "1", "JISHIYUN_BASE_URL": BASE_URL, "JISHIYUN_API_KEY": SYNTHETIC_KEY}

    def test_archive_preview_outcome_is_reported_without_energy_attributes(self) -> None:
        output = io.StringIO()
        with patch.dict("os.environ", self.environment, clear=True), patch("myems_jishiyun.worker.database_factory"), patch(
            "myems_jishiyun.worker.execute_one", return_value=ArchiveOutcome("preview", 17, archive_count=33),
        ), patch("myems_jishiyun.worker.signal.signal"), redirect_stdout(output):
            self.assertEqual(main(["run", "--once"]), 0)
        result = json.loads(output.getvalue())
        self.assertEqual((result["status"], result["archive_count"]), ("preview", 33))
        self.assertFalse(result["collection_activated"])

    def test_archive_fetch_command_only_enqueues(self) -> None:
        key = str(uuid4())
        with patch.dict("os.environ", {"JISHIYUN_DATA_SOURCE_ID": "1"}, clear=True), patch(
            "myems_jishiyun.worker.database_factory",
        ), patch("myems_jishiyun.worker.RunQueue") as queue, patch("myems_jishiyun.worker.JishiyunClient") as client, redirect_stdout(io.StringIO()):
            queue.return_value.enqueue_archive.return_value = 17
            self.assertEqual(main(["archive-fetch", "--task-id", "3", "--request-key", key, "--actor", "7"]), 0)
            queue.return_value.enqueue_archive.assert_called_once_with(3, key, 7)
            client.assert_not_called()

    def test_archive_submit_command_queues_explicit_selection(self) -> None:
        with patch.dict("os.environ", {"JISHIYUN_DATA_SOURCE_ID": "1"}, clear=True), patch(
            "myems_jishiyun.worker.database_factory",
        ), patch("myems_jishiyun.worker.ArchiveInitializer") as service, redirect_stdout(io.StringIO()):
            self.assertEqual(main([
                "archive-submit", "--run-id", "10", "--actor", "7", "--address", "0001", "--address", "0002",
            ]), 0)
            service.return_value.submit_approved.assert_called_once_with(10, ("0001", "0002"), 7)


if __name__ == "__main__":
    unittest.main()
