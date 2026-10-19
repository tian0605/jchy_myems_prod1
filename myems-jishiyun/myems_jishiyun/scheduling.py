"""Opt-in hourly submissions and resumable explicit backfill; no provider calls."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterator
from uuid import UUID, uuid5

from myems_jishiyun.contracts import ContractError
from myems_jishiyun.execution import RunQueue, _settings_from_task, _uuid
from myems_jishiyun.persistence import PersistenceError, _positive_id, _quarter
from myems_jishiyun.batches import register_batch


SCHEDULE_NAMESPACE = UUID("6c79e9c0-363a-4b5c-b661-8f33fcc9bef2")
MAX_BACKFILL_DAYS = 31


@dataclass(frozen=True)
class Window:
    start: datetime
    end: datetime
    tick: datetime


def _integer(value: object) -> int:
    if type(value) is not int:
        raise ContractError("Scheduler settings must be integers")
    return value


def scheduled_window(
    now: datetime, offset_minutes: int, schedule_minute: int, delay_minutes: int, lookback_hours: int,
) -> Window:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ContractError("Scheduler clock must be timezone-aware")
    if type(offset_minutes) is not int or not -840 <= offset_minutes <= 840 or offset_minutes % 15:
        raise ContractError("Provider UTC offset must align to 15 minutes within +/-14 hours")
    if type(schedule_minute) is not int or not 0 <= schedule_minute <= 59:
        raise ContractError("Schedule minute must be between 0 and 59")
    if type(delay_minutes) is not int or not 0 <= delay_minutes <= 65535:
        raise ContractError("Collection delay must be a non-negative integer")
    if type(lookback_hours) is not int or not 1 <= lookback_hours <= 24:
        raise ContractError("Scheduled lookback must be between 1 and 24 hours")
    local = now.astimezone(timezone(timedelta(minutes=offset_minutes)))
    tick = local.replace(minute=schedule_minute, second=0, microsecond=0)
    if tick > local:
        tick -= timedelta(hours=1)
    end = (tick - timedelta(minutes=delay_minutes)).replace(minute=0, second=0, microsecond=0)
    return Window(
        (end - timedelta(hours=lookback_hours)).astimezone(timezone.utc),
        end.astimezone(timezone.utc), tick.astimezone(timezone.utc),
    )


def schedule_due(queue: RunQueue) -> tuple[int, ...]:
    """Replay the latest eligible tick; no unbounded outage catch-up."""
    with queue.transaction() as cursor:
        now = queue.now(cursor)
        cursor.execute(
            "SELECT id, provider_utc_offset_minutes, schedule_minute, delay_minutes, lookback_hours "
            "FROM myems_system_db.tbl_jishiyun_tasks "
            "WHERE data_source_id = %s AND task_type = 'energy_collect' AND is_enabled = TRUE ORDER BY id",
            (queue.source_id,),
        )
        rows = cursor.fetchall()
    if len(rows) > 128:
        raise PersistenceError("Too many active collection schedules for this worker")
    planned: list[tuple[int, Window]] = []
    for row in rows:
        offset, minute, delay, lookback = (_integer(value) for value in row[1:])
        planned.append((
            _positive_id(row[0], "Scheduled task ID"),
            scheduled_window(now, offset, minute, delay, lookback),
        ))
    run_ids: list[int] = []
    for task_id, window in planned:
        key = str(uuid5(SCHEDULE_NAMESPACE, f"{queue.source_id}:{task_id}:{window.tick.isoformat()}"))
        run_ids.append(queue.enqueue(task_id, key, window.start, window.end, None))
    return tuple(run_ids)


def split_backfill(start: datetime, end: datetime) -> tuple[tuple[datetime, datetime], ...]:
    start, end = _quarter(start), _quarter(end)
    if start >= end or end - start > timedelta(days=MAX_BACKFILL_DAYS):
        raise ContractError("Backfill submission must be increasing and no longer than 31 days")
    windows: list[tuple[datetime, datetime]] = []
    while start < end:
        next_end = min(start + timedelta(days=1), end)
        windows.append((start, next_end))
        start = next_end
    return tuple(windows)


def enqueue_backfill(
    queue: RunQueue, task_id: int, parent_request_key: str,
    start: datetime, end: datetime, requested_by: int,
) -> Iterator[int]:
    """Yield committed child IDs; failures leave a visible, resumable prefix."""
    windows = split_backfill(start, end)
    parent = UUID(_uuid(parent_request_key))
    task_id = _positive_id(task_id, "Backfill task ID")
    requested_by = _positive_id(requested_by, "User ID")
    with queue.transaction() as cursor:
        cursor.execute(
            "SELECT task_type, is_enabled, delay_minutes, provider_utc_offset_minutes, parameters_json "
            "FROM myems_system_db.tbl_jishiyun_tasks WHERE id = %s AND data_source_id = %s FOR UPDATE",
            (task_id, queue.source_id),
        )
        task = cursor.fetchone()
        if task is None or task[0] != "energy_backfill" or task[1] != 1:
            raise PersistenceError("An enabled energy_backfill task for this source is required")
        delay = task[2]
        if type(delay) is not int or not 0 <= delay <= 65535:
            raise PersistenceError("Backfill task delay must be a non-negative integer")
        _settings_from_task(task[3], task[4])
        if windows[-1][1] + timedelta(minutes=delay) > queue.now(cursor):
            raise PersistenceError("Entire backfill window must be due before submission")
        # Reject out-of-retention guesses at the API boundary separately: vendor
        # 'about a year' is not a precise hard-coded expiry date.
    register_batch(queue, task_id, str(parent), windows[0][0], windows[-1][1], requested_by, len(windows))
    for index, (window_start, window_end) in enumerate(windows):
        key = str(uuid5(parent, f"backfill:{index}"))
        yield queue.enqueue(task_id, key, window_start, window_end, requested_by, batch_key=str(parent))
