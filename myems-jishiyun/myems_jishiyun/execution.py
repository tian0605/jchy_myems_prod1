"""Source-serialized energy/archive queue with fenced worker execution."""

from __future__ import annotations

import hashlib
import json
import time
import math
import random
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterator, Literal, Protocol, cast
from uuid import UUID, uuid4
from urllib.error import HTTPError

from myems_jishiyun.client import Encoding, JishiyunClient, RETRYABLE_STATUS, TransportError, retry_delay
from myems_jishiyun.contracts import ContractError
from myems_jishiyun.persistence import (
    Connection, Cursor, PersistenceError, Result, ResultStore, ScopeMeter,
    _address, _db_time, _positive_id, _quarter, prepare_batch, scope_snapshot,
)


LEASE = timedelta(minutes=3)
MAX_CLAIMS = 3
ENERGY_TYPES = ("energy_collect", "energy_backfill")


class RetryScheduled(TransportError):
    """The attempt failed, but a durable bounded retry was committed."""


class RecordedContractError(ContractError):
    """A run's contract failure and audit were committed successfully."""

    def __init__(self, run_id: int) -> None:
        super().__init__("Provider response failed contract validation")
        self.run_id = run_id


class ManagedConnection(Connection, Protocol):
    def close(self) -> None: ...


ConnectionFactory = Callable[[], ManagedConnection]


@dataclass(frozen=True)
class ClaimedRun:
    id: int
    token: str
    source_id: int
    start: datetime | None
    end: datetime | None
    offset_minutes: int
    encoding: Encoding
    task_type: str = "energy_collect"
    phase: str = "energy"


def _uuid(value: str) -> str:
    try:
        return str(UUID(value))
    except (ValueError, AttributeError):
        raise PersistenceError("Request or lease identifier must be a UUID") from None


def _settings(body: object) -> tuple[int, Encoding]:
    if not isinstance(body, str):
        raise PersistenceError("Run parameters must be JSON text")
    try:
        parsed = json.loads(body)
    except ValueError:
        raise PersistenceError("Run parameters must be valid JSON") from None
    if not isinstance(parsed, dict):
        raise PersistenceError("Run parameters must be an object")
    settings = cast(dict[str, object], parsed)
    offset = settings.get("provider_utc_offset_minutes")
    encoding = settings.get("encoding")
    if type(offset) is not int or not -840 <= offset <= 840 or offset % 15:
        raise PersistenceError("Provider UTC offset must align to 15 minutes within +/-14 hours")
    if encoding not in ("json", "form"):
        raise PersistenceError("Run request encoding must be json or form")
    return offset, cast(Encoding, encoding)


class RunQueue:
    def __init__(self, connect: ConnectionFactory, source_id: int) -> None:
        self.connect = connect
        self.source_id = _positive_id(source_id, "Data source ID")

    @contextmanager
    def transaction(self) -> Iterator[Cursor]:
        connection = self.connect()
        cursor: Cursor | None = None
        try:
            connection.start_transaction()
            cursor = connection.cursor()
            self._engines(cursor)
            cursor.execute(
                "SELECT id FROM myems_system_db.tbl_data_sources WHERE id = %s FOR UPDATE",
                (self.source_id,),
            )
            if cursor.fetchone() is None:
                raise PersistenceError("Worker data source does not exist")
            yield cursor
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            try:
                if cursor is not None:
                    cursor.close()
            finally:
                connection.close()

    @staticmethod
    def _engines(cursor: Cursor) -> None:
        names = (
            "tbl_data_sources", "tbl_jishiyun_tasks", "tbl_jishiyun_runs",
            "tbl_jishiyun_meter_mappings", "tbl_jishiyun_events",
            "tbl_jishiyun_worker_state", "tbl_jishiyun_request_quotas",
        )
        cursor.execute(
            "SELECT TABLE_NAME, ENGINE FROM information_schema.TABLES "
            "WHERE TABLE_SCHEMA = 'myems_system_db' AND TABLE_NAME IN (" + ", ".join(["%s"] * len(names)) + ")",
            names,
        )
        rows = cursor.fetchall()
        if len(rows) != len(names) or {row[0] for row in rows} != set(names) or any(row[1] != "InnoDB" for row in rows):
            raise PersistenceError("Worker queue requires all extension tables using InnoDB")

    @staticmethod
    def now(cursor: Cursor) -> datetime:
        cursor.execute("SELECT UTC_TIMESTAMP()")
        row = cursor.fetchone()
        if row is None:
            raise PersistenceError("Database clock unavailable")
        return _db_time(row[0])

    def event(self, cursor: Cursor, run_id: int, kind: str, details: dict[str, object]) -> None:
        actor = details.get("actor_user_id")
        if actor is not None:
            actor = _positive_id(actor, "Event actor user ID")
        cursor.execute(
            "INSERT INTO myems_system_db.tbl_jishiyun_events "
            "(data_source_id, run_id, actor_user_id, event_type, details_json) VALUES (%s, %s, %s, %s, %s)",
            (self.source_id, run_id, actor, kind, json.dumps(details, sort_keys=True)),
        )

    def enqueue(
        self, task_id: int, request_key: str, start: datetime, end: datetime,
        requested_by: int | None, *, batch_key: str | None = None,
    ) -> int:
        task_id = _positive_id(task_id, "Task ID")
        if requested_by is not None:
            requested_by = _positive_id(requested_by, "User ID")
        request_key = _uuid(request_key)
        start, end = _quarter(start), _quarter(end)
        if start >= end or end - start > timedelta(days=1):
            raise ContractError("Queued energy windows must be increasing and at most one day")
        with self.transaction() as cursor:
            if batch_key is not None:
                cursor.execute(
                    "SELECT is_cancelled FROM myems_system_db.tbl_jishiyun_backfill_batches "
                    "WHERE request_key = %s AND data_source_id = %s FOR UPDATE",
                    (_uuid(batch_key), self.source_id),
                )
                batch = cursor.fetchone()
                if batch is None or batch[0] != 0:
                    raise PersistenceError("Backfill batch is missing or cancelled")
            cursor.execute(
                "SELECT id, task_id, data_source_id, window_start_utc, window_end_utc, requested_by_user_id "
                "FROM myems_system_db.tbl_jishiyun_runs WHERE request_key = %s FOR UPDATE",
                (request_key,),
            )
            previous = cursor.fetchone()
            if previous is not None:
                if (
                    previous[1] != task_id or previous[2] != self.source_id or _db_time(previous[3]) != start
                    or _db_time(previous[4]) != end or previous[5] != requested_by
                ):
                    raise PersistenceError("Request identifier was already used for a different submission")
                return _positive_id(previous[0], "Existing run ID")
            cursor.execute(
                "SELECT task_type, is_enabled, delay_minutes, provider_utc_offset_minutes, parameters_json "
                "FROM myems_system_db.tbl_jishiyun_tasks WHERE id = %s AND data_source_id = %s FOR UPDATE",
                (task_id, self.source_id),
            )
            task = cursor.fetchone()
            if task is None or task[0] not in ENERGY_TYPES or task[1] != 1:
                raise PersistenceError("An enabled energy task for this source is required")
            delay = task[2]
            if type(delay) is not int or not 0 <= delay <= 65535:
                raise PersistenceError("Task delay must be a non-negative integer")
            # Only allowlisted execution parameters are copied; never copy connection secrets.
            offset, encoding = _settings_from_task(task[3], task[4])
            if end + timedelta(minutes=delay) > self.now(cursor):
                raise PersistenceError("Cannot enqueue an energy window before its delay has elapsed")
            cursor.execute(
                "SELECT external_address, external_id, collection_start_utc, collection_end_utc "
                "FROM myems_system_db.tbl_jishiyun_meter_mappings "
                "WHERE data_source_id = %s AND configuration_status = 'ready' "
                "AND collection_start_utc IS NOT NULL "
                "AND (is_enabled = TRUE OR collection_end_utc IS NOT NULL) "
                "AND collection_start_utc < %s AND (collection_end_utc IS NULL OR collection_end_utc > %s) "
                "ORDER BY external_address FOR UPDATE",
                (self.source_id, end.replace(tzinfo=None), start.replace(tzinfo=None)),
            )
            scope = tuple(
                ScopeMeter(
                    _address(row[0]), _positive_id(row[1], "External ID"), _db_time(row[2]),
                    _db_time(row[3]) if row[3] is not None else None,
                )
                for row in cursor.fetchall()
            )
            prepare_batch((), scope, start, end)
            parameters = json.dumps({
                "delay_minutes": delay, "provider_utc_offset_minutes": offset, "encoding": encoding,
            }, sort_keys=True)
            cursor.execute(
                "INSERT INTO myems_system_db.tbl_jishiyun_runs "
                "(task_id, data_source_id, request_key, status, window_start_utc, window_end_utc, "
                "scope_snapshot_json, parameters_json, requested_by_user_id) "
                "VALUES (%s, %s, %s, 'queued', %s, %s, %s, %s, %s)",
                (
                    task_id, self.source_id, request_key, start.replace(tzinfo=None), end.replace(tzinfo=None),
                    scope_snapshot(scope), parameters, requested_by,
                ),
            )
            run_id = _positive_id(cursor.lastrowid, "New run ID")
            self.event(cursor, run_id, "energy_run_queued", {"requested_by_user_id": requested_by})
            return run_id

    def enqueue_archive(self, task_id: int, request_key: str, requested_by: int) -> int:
        task_id = _positive_id(task_id, "Archive task ID")
        requested_by = _positive_id(requested_by, "Requesting user ID")
        request_key = _uuid(request_key)
        with self.transaction() as cursor:
            cursor.execute(
                "SELECT id, task_id, data_source_id, requested_by_user_id, parameters_json "
                "FROM myems_system_db.tbl_jishiyun_runs WHERE request_key = %s FOR UPDATE", (request_key,),
            )
            existing = cursor.fetchone()
            if existing is not None:
                if existing[1:4] != (task_id, self.source_id, requested_by):
                    raise PersistenceError("Archive request key belongs to a different submission")
                cursor.execute(
                    "SELECT id FROM myems_system_db.tbl_jishiyun_events "
                    "WHERE run_id = %s AND data_source_id = %s AND event_type = 'archive_fetch_queued' LIMIT 1",
                    (existing[0], self.source_id),
                )
                if cursor.fetchone() is None:
                    raise PersistenceError("Request key was not submitted for provider archive fetching")
                return _positive_id(existing[0], "Existing archive run ID")
            cursor.execute(
                "SELECT task_type, is_enabled, provider_utc_offset_minutes, parameters_json "
                "FROM myems_system_db.tbl_jishiyun_tasks WHERE id = %s AND data_source_id = %s FOR UPDATE",
                (task_id, self.source_id),
            )
            task = cursor.fetchone()
            if task is None or task[0] != "meter_initialize" or task[1] != 1:
                raise PersistenceError("An enabled meter_initialize task for this source is required")
            offset, encoding = _settings_from_task(task[2], task[3])
            cursor.execute(
                "INSERT INTO myems_system_db.tbl_jishiyun_runs "
                "(task_id, data_source_id, request_key, status, scope_snapshot_json, parameters_json, requested_by_user_id) "
                "VALUES (%s, %s, %s, 'queued', %s, %s, %s)",
                (task_id, self.source_id, request_key, '{"version":1,"meters":[]}', json.dumps({
                    "phase": "archive_fetch", "provider_utc_offset_minutes": offset, "encoding": encoding,
                }), requested_by),
            )
            run_id = _positive_id(cursor.lastrowid, "Archive run ID")
            self.event(cursor, run_id, "archive_fetch_queued", {"actor_user_id": requested_by})
            return run_id

    def claim(self) -> ClaimedRun | None:
        with self.transaction() as cursor:
            now = self.now(cursor)
            cursor.execute(
                "SELECT id FROM myems_system_db.tbl_jishiyun_runs "
                "WHERE data_source_id = %s AND status = 'running' "
                "AND lease_expires_datetime_utc > %s ORDER BY id LIMIT 1 FOR UPDATE",
                (self.source_id, now.replace(tzinfo=None)),
            )
            if cursor.fetchone() is not None:
                return None
            cursor.execute(
                "SELECT r.id, r.attempt_count, r.window_start_utc, r.window_end_utc, r.parameters_json, "
                "r.next_attempt_datetime_utc, t.task_type "
                "FROM myems_system_db.tbl_jishiyun_runs r "
                "JOIN myems_system_db.tbl_jishiyun_tasks t ON t.id = r.task_id AND t.data_source_id = r.data_source_id "
                "WHERE r.data_source_id = %s AND t.is_enabled = TRUE "
                "AND t.task_type IN ('energy_collect', 'energy_backfill', 'meter_initialize') "
                "AND (r.status = 'queued' OR (r.status = 'running' AND r.lease_expires_datetime_utc <= %s)) "
                "ORDER BY r.id LIMIT 1 FOR UPDATE",
                (self.source_id, now.replace(tzinfo=None)),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            # Preserve source order while an older attempt is backing off.
            if row[5] is not None and _db_time(row[5]) > now:
                return None
            run_id = _positive_id(row[0], "Run ID")
            attempts = row[1]
            if type(attempts) is not int or attempts < 0:
                raise PersistenceError("Invalid run attempt count")
            if attempts >= MAX_CLAIMS:
                cursor.execute(
                    "UPDATE myems_system_db.tbl_jishiyun_runs SET status = 'error', error_code = 'lease_exhausted', "
                    "error_message = 'Worker lease recovery limit reached', finished_datetime_utc = %s, "
                    "lease_token = NULL, lease_expires_datetime_utc = NULL WHERE id = %s",
                    (now.replace(tzinfo=None), run_id),
                )
                self.event(cursor, run_id, "lease_recovery_exhausted", {})
                return None
            offset, encoding = _settings(row[4])
            phase = _phase(row[4]) if row[6] == "meter_initialize" else "energy"
            if row[6] == "meter_initialize" and phase not in ("archive_fetch", "archive_initialize"):
                raise PersistenceError("Queued initialization phase is unsupported")
            token = str(uuid4())
            cursor.execute(
                "UPDATE myems_system_db.tbl_jishiyun_runs SET status = 'running', "
                "lease_token = %s, lease_expires_datetime_utc = %s, attempt_count = attempt_count + 1, "
                "started_datetime_utc = COALESCE(started_datetime_utc, %s), finished_datetime_utc = NULL, "
                "next_attempt_datetime_utc = NULL, error_code = NULL, error_message = NULL WHERE id = %s",
                (token, (now + LEASE).replace(tzinfo=None), now.replace(tzinfo=None), run_id),
            )
            self._heartbeat(cursor, token, now)
            self.event(cursor, run_id, "run_claimed", {"attempt": attempts + 1, "phase": phase})
            return ClaimedRun(
                run_id, token, self.source_id,
                _db_time(row[2]) if row[2] is not None else None,
                _db_time(row[3]) if row[3] is not None else None,
                offset, encoding, str(row[6]), phase,
            )

    def retry(self, run: ClaimedRun, provider_delay: float = 0) -> bool:
        if run.source_id != self.source_id or not math.isfinite(provider_delay) or not 0 <= provider_delay <= 31536000:
            raise PersistenceError("Invalid retry source or delay")
        with self.transaction() as cursor:
            now = self.now(cursor)
            cursor.execute(
                "SELECT attempt_count, lease_token, lease_expires_datetime_utc, status "
                "FROM myems_system_db.tbl_jishiyun_runs WHERE id = %s AND data_source_id = %s FOR UPDATE",
                (run.id, self.source_id),
            )
            row = cursor.fetchone()
            if row is None or row[1] != run.token or row[3] != "running" or _db_time(row[2]) <= now:
                raise PersistenceError("Lost lease: cannot schedule retry")
            attempts = _positive_id(row[0], "Attempt count")
            if attempts >= MAX_CLAIMS:
                return False
            seconds = max(provider_delay, min(60 * 2 ** (attempts - 1), 900) + random.uniform(0, 10))
            next_attempt = now + timedelta(seconds=math.ceil(seconds))
            cursor.execute(
                "UPDATE myems_system_db.tbl_jishiyun_runs SET status = 'queued', "
                "next_attempt_datetime_utc = %s, lease_token = NULL, lease_expires_datetime_utc = NULL, "
                "error_code = 'retry_scheduled', error_message = 'Transient provider failure; retry queued' "
                "WHERE id = %s",
                (next_attempt.replace(tzinfo=None), run.id),
            )
            self.event(cursor, run.id, "energy_retry_scheduled", {
                "attempt": attempts, "next_attempt_datetime_utc": next_attempt.isoformat(),
                "provider_delay_seconds": provider_delay,
            })
            return True

    def _heartbeat(self, cursor: Cursor, token: str, now: datetime) -> None:
        cursor.execute(
            "SELECT data_source_id FROM myems_system_db.tbl_jishiyun_worker_state WHERE data_source_id = %s FOR UPDATE",
            (self.source_id,),
        )
        existing = cursor.fetchone()
        values = (token, now.replace(tzinfo=None), (now + LEASE).replace(tzinfo=None), now.replace(tzinfo=None), self.source_id)
        if existing is None:
            cursor.execute(
                "INSERT INTO myems_system_db.tbl_jishiyun_worker_state "
                "(worker_id, heartbeat_datetime_utc, lease_expires_datetime_utc, next_request_datetime_utc, data_source_id) "
                "VALUES (%s, %s, %s, %s, %s)", values,
            )
        else:
            cursor.execute(
                "UPDATE myems_system_db.tbl_jishiyun_worker_state SET worker_id = %s, heartbeat_datetime_utc = %s, "
                "lease_expires_datetime_utc = %s, next_request_datetime_utc = %s WHERE data_source_id = %s", values,
            )

    def renew(self, run: ClaimedRun, count_request: bool = False) -> None:
        if run.source_id != self.source_id:
            raise PersistenceError("Worker run source mismatch")
        with self.transaction() as cursor:
            now = self.now(cursor)
            cursor.execute(
                "UPDATE myems_system_db.tbl_jishiyun_runs SET lease_expires_datetime_utc = %s, "
                "request_count = request_count + %s "
                "WHERE id = %s AND data_source_id = %s AND status = 'running' AND lease_token = %s "
                "AND lease_expires_datetime_utc > %s",
                ((now + LEASE).replace(tzinfo=None), int(count_request), run.id, self.source_id, run.token, now.replace(tzinfo=None)),
            )
            # MySQL reports changed rows, not matched rows. A same-second renewal
            # can be unchanged, so verify the live token rather than rowcount alone.
            cursor.execute(
                "SELECT lease_token, lease_expires_datetime_utc FROM myems_system_db.tbl_jishiyun_runs "
                "WHERE id = %s AND data_source_id = %s AND status = 'running' FOR UPDATE",
                (run.id, self.source_id),
            )
            row = cursor.fetchone()
            if row is None or row[0] != run.token or _db_time(row[1]) <= now:
                raise PersistenceError("Worker lost its run lease")
            self._heartbeat(cursor, run.token, now)

    def fail(self, run: ClaimedRun, code: str) -> None:
        messages = {
            "transport_error": "Provider transport failed; inspect safe service status",
            "contract_error": "Provider response failed contract validation",
            "persistence_error": "Result could not be safely persisted",
            "worker_error": "Worker execution failed; inspect database and service health",
        }
        if code not in messages or run.source_id != self.source_id:
            raise PersistenceError("Invalid worker failure record")
        with self.transaction() as cursor:
            now = self.now(cursor)
            cursor.execute(
                "UPDATE myems_system_db.tbl_jishiyun_runs SET status = 'error', error_code = %s, error_message = %s, "
                "finished_datetime_utc = %s, lease_token = NULL, lease_expires_datetime_utc = NULL "
                "WHERE id = %s AND data_source_id = %s AND status = 'running' AND lease_token = %s "
                "AND lease_expires_datetime_utc > %s",
                (code, messages[code], now.replace(tzinfo=None), run.id, self.source_id, run.token, now.replace(tzinfo=None)),
            )
            if cursor.rowcount != 1:
                raise PersistenceError("Lost lease: failure cannot overwrite current run state")
            self.event(cursor, run.id, "energy_run_failed", {"error_code": code})

    def quota_status(self, api_key: str, *, reviewed_by: int | None = None) -> dict[str, object]:
        if not api_key:
            raise PersistenceError("Provider credential is required to inspect quota")
        if reviewed_by is not None:
            _positive_id(reviewed_by, "Reviewing user ID")
        fingerprint = hashlib.sha256(api_key.encode("utf-8")).hexdigest()
        with self.transaction() as cursor:
            cursor.execute(
                "SELECT next_request_datetime_utc, is_blocked FROM myems_system_db.tbl_jishiyun_request_quotas "
                "WHERE credential_fingerprint = %s FOR UPDATE", (fingerprint,),
            )
            row = cursor.fetchone()
            if row is None:
                raise PersistenceError("No shared quota exists for the supplied credential")
            if reviewed_by is not None and row[1] == 1:
                cursor.execute(
                    "UPDATE myems_system_db.tbl_jishiyun_request_quotas SET is_blocked = FALSE "
                    "WHERE credential_fingerprint = %s", (fingerprint,),
                )
                cursor.execute(
                    "INSERT INTO myems_system_db.tbl_jishiyun_events "
                    "(data_source_id, actor_user_id, event_type, details_json) VALUES (%s, %s, %s, %s)",
                    (self.source_id, reviewed_by, "provider_quota_reviewed", '{"block_cleared":true}'),
                )
            return {
                "is_blocked": row[1] == 1 and reviewed_by is None,
                "next_request_datetime_utc": _db_time(row[0]).isoformat(),
            }


def _settings_from_task(offset: object, body: object) -> tuple[int, Encoding]:
    if not isinstance(body, str):
        raise PersistenceError("Task parameters must be JSON text")
    try:
        values = json.loads(body)
    except ValueError:
        raise PersistenceError("Task parameters must be valid JSON") from None
    if not isinstance(values, dict):
        raise PersistenceError("Task parameters must be an object")
    settings = cast(dict[str, object], values)
    return _settings(json.dumps({"provider_utc_offset_minutes": offset, "encoding": settings.get("encoding")}))


def _phase(body: object) -> str:
    if not isinstance(body, str):
        raise PersistenceError("Initialization parameters must be JSON text")
    try:
        value = json.loads(body)
    except ValueError:
        raise PersistenceError("Initialization parameters must be valid JSON") from None
    if not isinstance(value, dict):
        raise PersistenceError("Initialization parameters must be an object")
    phase = cast(dict[str, object], value).get("phase")
    if not isinstance(phase, str):
        raise PersistenceError("Initialization run requires an explicit phase")
    return phase


class SharedRequestGate:
    """Session advisory lock spans the actual HTTP call, not a future reservation.

    All workers sharing a credential must use the same MySQL server and this gate.
    DB outages fail closed. The persisted timestamp survives process restarts.
    """

    def __init__(self, queue: RunQueue, run: ClaimedRun, api_key: str) -> None:
        if not api_key:
            raise ValueError("API key required for shared quota")
        self.queue, self.run = queue, run
        self.fingerprint = hashlib.sha256(api_key.encode("utf-8")).hexdigest()

    @contextmanager
    def __call__(self) -> Iterator[None]:
        connection = self.queue.connect()
        cursor: Cursor | None = None
        locked = False
        reserved = False
        cooldown = 0.0
        quarantine = False
        lock_name = "jishiyun:" + self.fingerprint[:48]
        try:
            cursor = connection.cursor()
            cursor.execute("SELECT GET_LOCK(%s, 10)", (lock_name,))
            acquired = cursor.fetchone()
            if acquired is None or acquired[0] != 1:
                raise PersistenceError("Shared provider request lock unavailable")
            locked = True
            cursor.execute(
                "INSERT IGNORE INTO myems_system_db.tbl_jishiyun_request_quotas "
                "(credential_fingerprint, next_request_datetime_utc) VALUES (%s, UTC_TIMESTAMP())",
                (self.fingerprint,),
            )
            connection.commit()
            # The advisory lock, not the transaction, protects this wait and call.
            cursor.execute(
                "SELECT next_request_datetime_utc, UTC_TIMESTAMP(), is_blocked "
                "FROM myems_system_db.tbl_jishiyun_request_quotas WHERE credential_fingerprint = %s",
                (self.fingerprint,),
            )
            row = cursor.fetchone()
            if row is None:
                raise PersistenceError("Shared quota row unavailable")
            if row[2] == 1:
                raise TransportError("Provider quota is blocked pending manual review")
            wait = (_db_time(row[0]) - _db_time(row[1])).total_seconds()
            connection.commit()
            if wait > 60:
                raise TransportError("Shared provider cooldown is active", retryable=True, retry_after=wait)
            if wait > 0:
                time.sleep(wait)
            self.queue.renew(self.run, count_request=True)
            # Persist before the call: crashes may waste a slot, never bypass one.
            now = self.queue.now(cursor)
            cursor.execute(
                "UPDATE myems_system_db.tbl_jishiyun_request_quotas SET next_request_datetime_utc = %s "
                "WHERE credential_fingerprint = %s",
                # DATETIME has second precision. Eight DB seconds guarantees at
                # least seven elapsed seconds even across truncation boundaries.
                ((now + timedelta(seconds=8)).replace(tzinfo=None), self.fingerprint),
            )
            connection.commit()
            reserved = True
            try:
                yield
            except HTTPError as exc:
                if exc.code in RETRYABLE_STATUS:
                    try:
                        cooldown = retry_delay(exc.headers.get("Retry-After", ""))
                    except TransportError:
                        # Ambiguous provider instructions must not let another
                        # source bypass the delay; explicit operator reset needed.
                        quarantine = True
                raise
        except Exception:
            connection.rollback()
            raise
        finally:
            try:
                if cursor is not None:
                    if locked:
                        try:
                            if reserved:
                                # Start spacing after completion too: a paused
                                # process between reservation and HTTP must not
                                # compress the next request's real start gap.
                                now = self.queue.now(cursor)
                                cursor.execute(
                                    "UPDATE myems_system_db.tbl_jishiyun_request_quotas "
                                    "SET next_request_datetime_utc = %s, is_blocked = %s WHERE credential_fingerprint = %s",
                                    (
                                        (now + timedelta(seconds=max(8, math.ceil(cooldown) + 1))).replace(tzinfo=None),
                                        int(quarantine), self.fingerprint,
                                    ),
                                )
                                connection.commit()
                        finally:
                            cursor.execute("SELECT RELEASE_LOCK(%s)", (lock_name,))
                            released = cursor.fetchone()
                            if released is None or released[0] != 1:
                                raise PersistenceError("Shared provider lock release failed")
                    cursor.close()
            finally:
                connection.close()


def execute_one(
    queue: RunQueue, base_url: str, api_key: str, *, allow_http_test: bool = False,
) -> Result | ArchiveOutcome | None:
    run = queue.claim()
    if run is None:
        return None
    try:
        if run.phase == "archive_initialize":
            from myems_jishiyun.initialization import ArchiveInitializer

            return ArchiveInitializer(queue).execute_approved(run)
        client = JishiyunClient(
            base_url, api_key, timezone(timedelta(minutes=run.offset_minutes)), encoding=run.encoding,
            max_attempts=1, request_guard=SharedRequestGate(queue, run, api_key),
            allow_http_test=allow_http_test,
        )
        if run.phase == "archive_fetch":
            from myems_jishiyun.initialization import ArchiveInitializer

            archives = client.archives()
            queue.renew(run)
            return ArchiveInitializer(queue).save_fetched(run, archives)
        if run.start is None or run.end is None:
            raise PersistenceError("Energy run requires a time window")
        readings = client.readings(run.start, run.end)
        queue.renew(run)
        connection = queue.connect()
        try:
            return ResultStore(connection).save(run.id, run.token, readings)
        finally:
            connection.close()
    except Exception as exc:
        if isinstance(exc, TransportError) and exc.retryable and queue.retry(run, exc.retry_after):
            raise RetryScheduled("Transient provider failure; durable retry scheduled") from None
        code = (
            "transport_error" if isinstance(exc, TransportError)
            else "contract_error" if isinstance(exc, ContractError)
            else "persistence_error" if isinstance(exc, PersistenceError)
            else "worker_error"
        )
        # Record only fixed codes/messages, never exception text or response bodies.
        queue.fail(run, code)
        if isinstance(exc, ContractError):
            raise RecordedContractError(run.id) from None
        raise


@dataclass(frozen=True)
class ArchiveOutcome:
    status: Literal["preview", "done", "partial"]
    run_id: int
    archive_count: int = 0
    created_count: int = 0
    reused_count: int = 0
    conflict_count: int = 0
    error_count: int = 0
