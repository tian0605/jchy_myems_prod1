"""Independent energy worker with opt-in hourly scheduling and explicit backfill."""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from types import FrameType
from typing import cast

from myems_jishiyun.client import JishiyunClient, MAX_RESPONSE_BYTES, http_test_setting
from myems_jishiyun.contracts import parse_archives
from myems_jishiyun.initialization import ArchiveInitializer, configuration_from_json
from myems_jishiyun.verify import provider_timezone
from myems_jishiyun.execution import (
    ArchiveOutcome, ConnectionFactory, ManagedConnection, RecordedContractError, RetryScheduled, RunQueue, execute_one,
)
from myems_jishiyun.health import clear_health, record_health
from myems_jishiyun.scheduling import enqueue_backfill, schedule_due
from myems_jishiyun.batches import batch_status
from myems_jishiyun.normalization import BusinessGateError, normalize_hour
from myems_jishiyun.business import process_business
from myems_jishiyun.settlement import settle_run


def required(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise ValueError("Required deployment setting is missing")
    return value


def bounded_text(path: Path) -> str:
    with path.open("rb") as input_file:
        body = input_file.read(MAX_RESPONSE_BYTES + 1)
    if len(body) > MAX_RESPONSE_BYTES:
        raise ValueError("Input file exceeds supported response size")
    return body.decode("utf-8-sig")


def database_factory() -> ConnectionFactory:
    host, user, password = required("JISHIYUN_DB_HOST"), required("JISHIYUN_DB_USER"), required("JISHIYUN_DB_PASSWORD")
    port = int(os.environ.get("JISHIYUN_DB_PORT", "3306"))
    if not 1 <= port <= 65535:
        raise ValueError("Invalid database port")

    def connect() -> ManagedConnection:
        import mysql.connector

        # Connector's dynamic factory is narrowed at this boundary; the queue
        # uses only the documented connection/cursor protocol.
        return cast(ManagedConnection, mysql.connector.connect(
            host=host, port=port, user=user, password=password,
            database="myems_system_db", autocommit=False, connection_timeout=10,
            read_timeout=30, write_timeout=30, charset="utf8mb4", use_pure=True, buffered=True,
        ))

    return connect


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    run_parser = commands.add_parser("run", help="Execute previously queued energy jobs")
    run_parser.add_argument("--once", action="store_true", help="Claim at most one job and exit")
    run_parser.add_argument("--schedule", action="store_true", help="Also submit latest due hourly windows")
    run_parser.add_argument("--health-file", type=Path, help="Dedicated local loop-heartbeat file for this worker")
    run_parser.add_argument("--business-schedule", action="store_true",
                            help="Also process durable first-write business jobs; no historical rewrites")
    run_parser.add_argument("--business-actor", type=int, help="Operator approving business scheduler execution")
    run_parser.add_argument("--confirm-timestamp-kwh", action="store_true",
                            help="Confirm ReadingDate is timestamp cumulative kWh")
    run_parser.add_argument("--pilot-test-settlement", action="store_true",
                            help="Also settle new business jobs using isolated PILOT TEST tariffs and factors")
    hour_parser = commands.add_parser("business-hour", help="Preview or first-write complete accepted hours")
    hour_parser.add_argument("--run-id", required=True, type=int, help="Successful aligned energy run, 1 to 24 hours")
    hour_parser.add_argument("--actor", required=True, type=int, help="Trusted local operator ID; not authorization")
    hour_parser.add_argument("--confirm-timestamp-kwh", action="store_true",
                             help="Confirm ReadingDate is timestamp register kWh; hour = R(end)-R(start)")
    hour_parser.add_argument("--apply", action="store_true", help="Commit; default is a read-only preview")
    settlement_parser = commands.add_parser("business-settle", help="Preview or first-write audited billing/carbon")
    settlement_parser.add_argument("--run-id", required=True, type=int)
    settlement_parser.add_argument("--actor", required=True, type=int, help="Trusted local operator ID; not authorization")
    settlement_parser.add_argument("--test-only", action="store_true", help="Require isolated PILOT TEST parameters")
    settlement_parser.add_argument("--apply", action="store_true", help="Commit; default is a read-only preview")
    correction_parser = commands.add_parser("business-correct", help="Preview or hash-approve audited raw revisions")
    correction_parser.add_argument("--run-id", required=True, type=int)
    correction_parser.add_argument("--actor", required=True, type=int)
    correction_parser.add_argument("--test-only", action="store_true")
    correction_parser.add_argument("--expected-hash", help="Exact approval hash from a separately reviewed preview")
    correction_parser.add_argument("--apply", action="store_true")
    dimensions_parser = commands.add_parser("business-dimensions", help="Aggregate explicitly configured direct pilot inputs")
    dimensions_parser.add_argument("--actor", required=True, type=int)
    dimensions_parser.add_argument("--start", required=True)
    dimensions_parser.add_argument("--end", required=True)
    dimensions_parser.add_argument("--apply", action="store_true")
    for command, description in (
        ("enqueue", "Submit one explicitly approved energy window"),
        ("backfill", "Submit up to 31 days as resumable, at-most-one-day child runs"),
    ):
        enqueue_parser = commands.add_parser(command, help=description)
        enqueue_parser.add_argument("--task-id", required=True, type=int)
        enqueue_parser.add_argument("--request-key", required=True, help="UUID idempotency key; reuse only for an identical submission")
        enqueue_parser.add_argument("--requested-by", required=True, type=int, help="Audited operator user ID; not an authorization check")
        enqueue_parser.add_argument("--start", required=True, help="Timezone-aware ISO timestamp")
        enqueue_parser.add_argument("--end", required=True, help="Exclusive timezone-aware ISO timestamp")
    batch_parser = commands.add_parser("batch", help="Inspect or cancel a persisted backfill batch")
    batch_parser.add_argument("--request-key", required=True)
    batch_parser.add_argument("--cancel-by", type=int, help="Operator user ID; cancel only after approval")
    quota_parser = commands.add_parser("quota", help="Inspect the configured credential's shared cooldown")
    quota_parser.add_argument("--reviewed-by", type=int, help="Operator ID: clear a block after manual provider review")
    preview_parser = commands.add_parser("archive-preview", help="Import an explicitly supplied archive response as an immutable preview")
    preview_parser.add_argument("--task-id", type=int, required=True)
    preview_parser.add_argument("--request-key", required=True)
    preview_parser.add_argument("--actor", type=int, required=True)
    preview_parser.add_argument("--response-file", type=Path, required=True)
    preview_parser.add_argument("--utc-offset", required=True)
    fetch_parser = commands.add_parser("archive-fetch", help="Queue a provider archive fetch; no HTTP in this command")
    fetch_parser.add_argument("--task-id", type=int, required=True)
    fetch_parser.add_argument("--request-key", required=True)
    fetch_parser.add_argument("--actor", type=int, required=True)
    submit_parser = commands.add_parser("archive-submit", help="Queue initialization of selected approved preview items")
    submit_parser.add_argument("--run-id", type=int, required=True)
    submit_parser.add_argument("--actor", type=int, required=True)
    submit_parser.add_argument("--address", action="append", required=True, help="Repeat for each approved address")
    status_parser = commands.add_parser("archive-status", help="Inspect initialization run and paged per-meter outcomes")
    status_parser.add_argument("--run-id", type=int, required=True)
    status_parser.add_argument("--after-id", type=int, default=0)
    status_parser.add_argument("--limit", type=int, default=100)
    for command in ("archive-approve", "archive-initialize"):
        initializer_parser = commands.add_parser(command, help="Approve or initialize one archived meter")
        initializer_parser.add_argument("--run-id", type=int, required=True)
        initializer_parser.add_argument("--address", required=True)
        initializer_parser.add_argument("--actor", type=int, required=True)
        if command == "archive-approve":
            initializer_parser.add_argument("--configuration-file", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "run" and args.business_schedule and (
        args.business_actor is None or args.business_actor <= 0 or not args.confirm_timestamp_kwh
    ):
        parser.error("--business-schedule requires --business-actor and --confirm-timestamp-kwh")
    if args.command == "run" and args.pilot_test_settlement and not args.business_schedule:
        parser.error("--pilot-test-settlement requires --business-schedule")
    try:
        if args.command == "run" and args.health_file is not None:
            clear_health(args.health_file)
        queue = RunQueue(database_factory(), int(required("JISHIYUN_DATA_SOURCE_ID")))
        if args.command == "business-correct":
            from myems_jishiyun.correction import correct_run

            print(json.dumps(correct_run(
                queue, args.run_id, args.actor, expected_hash=args.expected_hash,
                apply=args.apply, test_only=args.test_only,
            )))
            return 0
        if args.command == "business-dimensions":
            from myems_jishiyun.dimensions import aggregate_dimensions

            print(json.dumps(aggregate_dimensions(
                queue, args.actor, datetime.fromisoformat(args.start), datetime.fromisoformat(args.end), apply=args.apply,
            )))
            return 0
        if args.command == "business-settle":
            print(json.dumps(settle_run(queue, args.run_id, args.actor, test_only=args.test_only, apply=args.apply)))
            return 0
        if args.command == "business-hour":
            result = normalize_hour(queue, args.run_id, args.actor,
                                    confirm_timestamp_kwh=args.confirm_timestamp_kwh, apply=args.apply)
            print(json.dumps(result))
            return 0
        if args.command.startswith("archive-"):
            initializer = ArchiveInitializer(queue)
            if args.command == "archive-status":
                print(json.dumps(initializer.progress(args.run_id, after_id=args.after_id, limit=args.limit)))
            elif args.command == "archive-fetch":
                run_id = queue.enqueue_archive(args.task_id, args.request_key, args.actor)
                print(json.dumps({"status": "queued_or_existing", "run_id": run_id, "phase": "archive_fetch"}))
            elif args.command == "archive-submit":
                initializer.submit_approved(args.run_id, tuple(args.address), args.actor)
                print('{"status":"queued","phase":"archive_initialize"}')
            elif args.command == "archive-preview":
                body = bounded_text(args.response_file)
                archives = parse_archives(body, provider_timezone(args.utc_offset))
                run_id = initializer.create_preview(args.task_id, args.request_key, archives, args.actor)
                print(json.dumps({"status": "preview", "run_id": run_id, "archive_count": len(archives)}))
            elif args.command == "archive-approve":
                initializer.approve(
                    args.run_id, args.address, configuration_from_json(bounded_text(args.configuration_file)), args.actor,
                )
                print('{"status":"approved","collection_enabled":false}')
            else:
                outcome = initializer.initialize(args.run_id, args.address, args.actor)
                print(json.dumps({"status": outcome, "collection_configuration": "unchanged_or_new_disabled"}))
            return 0
        if args.command == "quota":
            print(json.dumps(queue.quota_status(required("JISHIYUN_API_KEY"), reviewed_by=args.reviewed_by)))
            return 0
        if args.command == "batch":
            print(json.dumps(batch_status(queue, args.request_key, cancel_by=args.cancel_by)))
            return 0
        if args.command in ("enqueue", "backfill"):
            if args.command == "backfill":
                for run_id in enqueue_backfill(
                    queue, args.task_id, args.request_key, datetime.fromisoformat(args.start),
                    datetime.fromisoformat(args.end), args.requested_by,
                ):
                    print(json.dumps({"status": "backfill_child_queued_or_existing", "run_id": run_id}), flush=True)
                return 0
            run_id = queue.enqueue(
                args.task_id, args.request_key, datetime.fromisoformat(args.start),
                datetime.fromisoformat(args.end), args.requested_by,
            )
            print(json.dumps({"status": "queued_or_existing", "run_id": run_id}))
            return 0
        base_url, api_key = required("JISHIYUN_BASE_URL"), required("JISHIYUN_API_KEY")
        allow_http_test = http_test_setting(os.environ.get("JISHIYUN_ALLOW_HTTP_TEST", "false"))
        # Fail configuration before claiming any work. This does not make a request.
        JishiyunClient(base_url, api_key, timezone.utc, max_attempts=1, allow_http_test=allow_http_test)
        stopped = threading.Event()

        def stop(_signum: int, _frame: FrameType | None) -> None:
            stopped.set()

        signal.signal(signal.SIGINT, stop)
        signal.signal(signal.SIGTERM, stop)
        while not stopped.is_set():
            if args.schedule:
                schedule_due(queue)
            try:
                result = execute_one(queue, base_url, api_key, **({"allow_http_test": True} if allow_http_test else {}))
            except RetryScheduled:
                print('{"status":"retry_scheduled","completed_run":false}', flush=True)
                if args.once:
                    return 3
                stopped.wait(5)
                continue
            except RecordedContractError as exc:
                print(json.dumps({
                    "status": "error", "run_id": exc.run_id, "error_code": "contract_error",
                    "completed_run": False, "worker_continues": not args.once,
                }), file=sys.stderr, flush=True)
                if args.once:
                    return 2
                result = None
            business = None
            if args.business_schedule:
                business = process_business(
                    queue, args.business_actor, confirm_timestamp_kwh=True,
                    **({"pilot_test_settlement": True} if args.pilot_test_settlement else {}),
                )
                if business is not None:
                    print(json.dumps(business), flush=True)
            if args.health_file is not None:
                record_health(args.health_file)
            if result is not None:
                if isinstance(result, ArchiveOutcome):
                    summary: dict[str, object] = {
                        "status": result.status, "run_id": result.run_id, "collection_activated": False,
                    }
                    if result.status == "preview":
                        summary["archive_count"] = result.archive_count
                    else:
                        summary.update({
                            "created_count": result.created_count, "reused_count": result.reused_count,
                            "conflict_count": result.conflict_count, "error_count": result.error_count,
                        })
                    print(json.dumps(summary), flush=True)
                else:
                    print(json.dumps({
                        "status": result.status, "expected_count": result.expected_count,
                        "valid_count": result.valid_count, "missing_count": result.missing_count,
                        "unexpected_count": result.unexpected_count,
                    }), flush=True)
            if args.once:
                if result is None:
                    if business is not None:
                        return 0 if business["status"] == "done" else 2
                    print(json.dumps({"status": "idle", "completed_run": False}))
                    return 0
                return 0 if result.status in ("done", "preview") else 2
            stopped.wait(5)
        return 0
    except BusinessGateError as exc:
        print(json.dumps({"status": "blocked", "gate": str(exc), "business_rows_committed": False}),
              file=sys.stderr, flush=True)
        return 2
    except Exception:
        # Driver/URL/config exceptions may contain credentials. Durable failures
        # use fixed codes; stderr deliberately does not format exception objects.
        print(json.dumps({
            "status": "error",
            "message": "Worker failed; inspect run error codes, database/schema and deployment configuration",
        }), file=sys.stderr, flush=True)
        return 1
    finally:
        if args.command == "run" and args.health_file is not None:
            try:
                clear_health(args.health_file)
            except OSError:
                print('{"status":"error","message":"Worker heartbeat cleanup failed"}', file=sys.stderr, flush=True)
                return 1


if __name__ == "__main__":
    sys.exit(main())
