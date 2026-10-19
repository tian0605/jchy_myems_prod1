"""Durable backfill parent identity, live child progress and fenced cancellation."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid5

from myems_jishiyun.execution import RunQueue, _uuid
from myems_jishiyun.persistence import Cursor, PersistenceError, _db_time, _positive_id


def register_batch(
    queue: RunQueue, task_id: int, request_key: str, start: datetime, end: datetime,
    requested_by: int, child_count: int,
) -> int:
    with queue.transaction() as cursor:
        _engine(cursor)
        cursor.execute(
            "SELECT id, data_source_id, task_id, requested_by_user_id, window_start_utc, window_end_utc, "
            "child_count, is_cancelled FROM myems_system_db.tbl_jishiyun_backfill_batches "
            "WHERE request_key = %s FOR UPDATE", (request_key,),
        )
        row = cursor.fetchone()
        if row is not None:
            if (
                row[1:4] != (queue.source_id, task_id, requested_by)
                or _db_time(row[4]) != start or _db_time(row[5]) != end or row[6] != child_count
            ):
                raise PersistenceError("Backfill parent identifier belongs to a different submission")
            if row[7] != 0:
                raise PersistenceError("Backfill batch is cancelled")
            return _positive_id(row[0], "Batch ID")
        cursor.execute(
            "INSERT INTO myems_system_db.tbl_jishiyun_backfill_batches "
            "(request_key, data_source_id, task_id, requested_by_user_id, window_start_utc, window_end_utc, child_count) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (request_key, queue.source_id, task_id, requested_by,
             start.replace(tzinfo=None), end.replace(tzinfo=None), child_count),
        )
        batch_id = _positive_id(cursor.lastrowid, "Batch ID")
        cursor.execute(
            "INSERT INTO myems_system_db.tbl_jishiyun_events "
            "(data_source_id, actor_user_id, event_type, details_json) VALUES (%s, %s, %s, %s)",
            (queue.source_id, requested_by, "backfill_batch_created",
             f'{{"batch_id":{batch_id},"child_count":{child_count}}}'),
        )
        return batch_id


def _engine(cursor: Cursor) -> None:
    cursor.execute(
        "SELECT ENGINE FROM information_schema.TABLES WHERE TABLE_SCHEMA = 'myems_system_db' "
        "AND TABLE_NAME = 'tbl_jishiyun_backfill_batches'",
    )
    row = cursor.fetchone()
    if row is None or row[0] != "InnoDB":
        raise PersistenceError("Backfill batch table must use InnoDB")


def batch_status(queue: RunQueue, request_key: str, *, cancel_by: int | None = None) -> dict[str, object]:
    request_key = _uuid(request_key)
    if cancel_by is not None:
        _positive_id(cancel_by, "Cancelling user ID")
    with queue.transaction() as cursor:
        _engine(cursor)
        cursor.execute(
            "SELECT id, task_id, child_count, is_cancelled FROM myems_system_db.tbl_jishiyun_backfill_batches "
            "WHERE request_key = %s AND data_source_id = %s FOR UPDATE", (request_key, queue.source_id),
        )
        row = cursor.fetchone()
        if row is None:
            raise PersistenceError("Backfill batch does not exist for this source")
        count = _positive_id(row[2], "Batch child count")
        if count > 31:
            raise PersistenceError("Backfill child count exceeds limit")
        keys = tuple(str(uuid5(UUID(request_key), f"backfill:{index}")) for index in range(count))
        markers = ", ".join(["%s"] * count)
        cursor.execute(
            "SELECT id, status FROM myems_system_db.tbl_jishiyun_runs "
            f"WHERE data_source_id = %s AND request_key IN ({markers}) ORDER BY id FOR UPDATE",
            (queue.source_id,) + keys,
        )
        children = cursor.fetchall()
        if cancel_by is not None and row[3] == 0:
            cursor.execute(
                "UPDATE myems_system_db.tbl_jishiyun_backfill_batches SET is_cancelled = TRUE, "
                "cancelled_by_user_id = %s, cancelled_datetime_utc = UTC_TIMESTAMP() WHERE id = %s",
                (cancel_by, row[0]),
            )
            cursor.execute(
                "INSERT INTO myems_system_db.tbl_jishiyun_events "
                "(data_source_id, actor_user_id, event_type, details_json) VALUES (%s, %s, %s, %s)",
                (queue.source_id, cancel_by, "backfill_batch_cancelled", f'{{"batch_id":{row[0]}}}'),
            )
            for child in children:
                if child[1] in ("queued", "running"):
                    cursor.execute(
                        "UPDATE myems_system_db.tbl_jishiyun_runs SET status = 'cancelled', "
                        "lease_token = NULL, lease_expires_datetime_utc = NULL, finished_datetime_utc = UTC_TIMESTAMP() "
                        "WHERE id = %s", (child[0],),
                    )
                    queue.event(cursor, _positive_id(child[0], "Child ID"), "backfill_child_cancelled", {
                        "batch_id": row[0], "actor_user_id": cancel_by,
                    })
            children = [(child[0], "cancelled" if child[1] in ("queued", "running") else child[1]) for child in children]
        statuses: dict[str, int] = {}
        for child in children:
            state = str(child[1])
            statuses[state] = statuses.get(state, 0) + 1
        cancelled = row[3] == 1 or cancel_by is not None
        return {
            "batch_id": row[0], "request_key": request_key, "task_id": row[1],
            "is_cancelled": cancelled, "expected_children": count, "submitted_children": len(children),
            "unsubmitted_children": count - len(children),
            "child_status_counts": statuses, "run_ids": [child[0] for child in children],
            "complete": not cancelled and len(children) == count and statuses.get("done", 0) == count,
        }
