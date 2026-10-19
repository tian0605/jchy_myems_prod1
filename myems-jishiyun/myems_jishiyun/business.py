"""Durable opt-in first-write business jobs; no provider calls or historical rewrites."""

from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

from myems_jishiyun.execution import RunQueue
from myems_jishiyun.normalization import BusinessGateError, normalize_hour
from myems_jishiyun.persistence import Cursor, PersistenceError, positive_id
from myems_jishiyun.settlement import settle_run


LEASE = timedelta(minutes=5)
RETRY = timedelta(minutes=5)


def _schema(cursor: Cursor) -> None:
    cursor.execute(
        "SELECT ENGINE FROM information_schema.TABLES "
        "WHERE TABLE_SCHEMA='myems_system_db' AND TABLE_NAME='tbl_jishiyun_business_jobs'",
    )
    if cursor.fetchone() != ("InnoDB",):
        raise PersistenceError("Install the transactional business job migration before enabling business scheduling")


def process_business(
    queue: RunQueue, actor: int, *, confirm_timestamp_kwh: bool, pilot_test_settlement: bool = False,
) -> dict[str, object] | None:
    """Discover at most 100 completed energy runs and execute one due job.

    Claim and finish are short source-locked transactions. If the process dies
    after energy commit, lease recovery replays the immutable business proof.
    Gate failures remain visible and retry after five minutes; raw collection
    and the next successful hour are not blocked by one incomplete hour.
    """
    actor = positive_id(actor, "Business scheduler actor")
    if confirm_timestamp_kwh is not True:
        raise BusinessGateError("confirm_timestamp_register_semantics_and_kwh_before_calculation")
    with queue.transaction() as cursor:
        _schema(cursor)
        cursor.execute(
            "SELECT r.id FROM myems_system_db.tbl_jishiyun_runs r "
            "JOIN myems_system_db.tbl_jishiyun_tasks t ON t.id=r.task_id AND t.data_source_id=r.data_source_id "
            "LEFT JOIN myems_system_db.tbl_jishiyun_business_jobs j ON j.run_id=r.id "
            "WHERE r.data_source_id=%s AND r.status='done' "
            "AND t.task_type IN ('energy_collect','energy_backfill') AND j.id IS NULL "
            "ORDER BY r.id LIMIT 100", (queue.source_id,),
        )
        for row in cursor.fetchall():
            cursor.execute(
                "INSERT INTO myems_system_db.tbl_jishiyun_business_jobs "
                "(data_source_id,run_id,actor_user_id) VALUES (%s,%s,%s)",
                (queue.source_id, positive_id(row[0], "Energy run"), actor),
            )
        now = queue.now(cursor)
        cursor.execute(
            "SELECT id,run_id,actor_user_id FROM myems_system_db.tbl_jishiyun_business_jobs "
            "WHERE data_source_id=%s AND ("
            "(status IN ('queued','blocked') AND (next_attempt_datetime_utc IS NULL OR next_attempt_datetime_utc <= %s)) "
            "OR (status='running' AND lease_expires_datetime_utc <= %s)) "
            "ORDER BY id LIMIT 1 FOR UPDATE",
            (queue.source_id, now.replace(tzinfo=None), now.replace(tzinfo=None)),
        )
        job = cursor.fetchone()
        if job is None:
            return None
        job_id, run_id, job_actor = (positive_id(value, "Business job identity") for value in job)
        token = str(uuid4())
        cursor.execute(
            "UPDATE myems_system_db.tbl_jishiyun_business_jobs SET status='running',"
            "lease_token=%s,lease_expires_datetime_utc=%s,attempt_count=attempt_count+1,"
            "next_attempt_datetime_utc=NULL WHERE id=%s",
            (token, (now + LEASE).replace(tzinfo=None), job_id),
        )
    status, gate, registers, hours = "done", None, 0, 0
    try:
        result = normalize_hour(queue, run_id, job_actor, confirm_timestamp_kwh=True, apply=True)
        register_count, hour_count = result["register_insert_count"], result["hour_insert_count"]
        if type(register_count) is not int or type(hour_count) is not int or register_count < 0 or hour_count < 0:
            raise PersistenceError("Business result counts must be nonnegative integers")
        registers, hours = register_count, hour_count
        if pilot_test_settlement:
            try:
                settle_run(queue, run_id, job_actor, test_only=True, apply=True)
            except BusinessGateError as exc:
                raise BusinessGateError("settlement:" + str(exc)) from None
            try:
                from myems_jishiyun.dimensions import aggregate_run_dimensions

                aggregate_run_dimensions(queue, job_actor, run_id)
            except BusinessGateError as exc:
                raise BusinessGateError("dimensions:" + str(exc)) from None
    except BusinessGateError as exc:
        status, gate = "blocked", str(exc)
    except Exception:
        # Persist a fixed non-sensitive error, then fail explicitly to the worker.
        _finish(queue, job_id, token, "error", "business_execution_failed", registers, hours)
        raise
    _finish(queue, job_id, token, status, gate, registers, hours)
    return {
        "business_job_id": job_id, "run_id": run_id, "status": status, "gate_code": gate,
        "register_insert_count": registers, "hour_insert_count": hours,
    }


def _finish(
    queue: RunQueue, job_id: int, token: str, status: str, gate: str | None,
    registers: int, hours: int,
) -> None:
    with queue.transaction() as cursor:
        now = queue.now(cursor)
        cursor.execute(
            "UPDATE myems_system_db.tbl_jishiyun_business_jobs SET status=%s,gate_code=%s,"
            "register_insert_count=%s,hour_insert_count=%s,next_attempt_datetime_utc=%s,"
            "lease_token=NULL,lease_expires_datetime_utc=NULL "
            "WHERE id=%s AND data_source_id=%s AND status='running' AND lease_token=%s "
            "AND lease_expires_datetime_utc > %s",
            (status, gate, registers, hours,
             (now + RETRY).replace(tzinfo=None) if status == "blocked" else None,
             job_id, queue.source_id, token, now.replace(tzinfo=None)),
        )
        if cursor.rowcount != 1:
            raise PersistenceError("Business job lease expired or changed; completion was not acknowledged")
