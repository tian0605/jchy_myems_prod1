"""Audited first-write hourly processing; not an automatic historical rewriter."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import cast

from myems_jishiyun.execution import RunQueue
from myems_jishiyun.persistence import (
    Cursor, PersistenceError, ScopeMeter, db_time, positive_id, snapshot_scope, stored_decimal,
)


class BusinessGateError(PersistenceError):
    """A named gate failed; no business rows were committed."""


@dataclass(frozen=True)
class Register:
    timestamp: datetime
    value: Decimal
    revision: int


@dataclass(frozen=True)
class MeterHour:
    address: str
    meter_id: int
    point_id: int
    readings: tuple[Register, ...]
    consumption: Decimal

    def summary(self) -> dict[str, object]:
        return {
            "address": self.address, "meter_id": self.meter_id, "point_id": self.point_id,
            "registers": [
                {"timestamp_utc": r.timestamp.isoformat(), "value": str(r.value), "revision": r.revision}
                for r in self.readings
            ],
            "consumption_kwh": str(self.consumption),
        }


def _number(value: object) -> Decimal:
    try:
        number = stored_decimal(value)
    except (PersistenceError, InvalidOperation):
        raise BusinessGateError("invalid_register_or_limit") from None
    if number is None or number < 0 or number > Decimal("999999999999999.999999"):
        raise BusinessGateError("invalid_register_or_limit")
    if number != number.quantize(Decimal("0.000001")):
        raise BusinessGateError("unsupported_decimal_precision")
    return number.quantize(Decimal("0.000001"))


def calculate_hour(
    start: datetime, readings: tuple[Register, ...], low: Decimal, high: Decimal, register_high: Decimal,
) -> Decimal:
    expected = tuple(start + timedelta(minutes=15 * index) for index in range(5))
    if start.tzinfo is None or start.utcoffset() is None or start.minute or start.second or start.microsecond:
        raise BusinessGateError("hour_start_must_be_timezone_aware_and_hour_aligned")
    if tuple(r.timestamp for r in readings) != expected:
        raise BusinessGateError("five_quarter_hour_readings_including_end_boundary_required")
    low, high, register_high = _number(low), _number(high), _number(register_high)
    if low > high or register_high == 0:
        raise BusinessGateError("invalid_configured_limits")
    values = tuple(_number(r.value) for r in readings)
    if any(value > register_high for value in values):
        raise BusinessGateError("cumulative_register_limit_exceeded")
    if any(right < left for left, right in zip(values, values[1:])):
        raise BusinessGateError("register_decrease_requires_manual_review")
    consumption = values[-1] - values[0]
    if not low <= consumption <= high:
        raise BusinessGateError("hourly_consumption_outside_configured_limits")
    return consumption


def committed_hour_proofs(value: object) -> tuple[dict[str, object], ...]:
    if not isinstance(value, str):
        raise BusinessGateError("invalid_committed_business_proof")
    try:
        decoded: object = json.loads(value)
    except ValueError:
        raise BusinessGateError("invalid_committed_business_proof") from None
    if not isinstance(decoded, dict):
        raise BusinessGateError("invalid_committed_business_proof")
    stored = cast(dict[str, object], decoded)
    if stored.get("version") == 1:
        return (stored,)
    entries = stored.get("hours")
    if stored.get("version") != 2 or not isinstance(entries, list):
        raise BusinessGateError("invalid_committed_business_proof")
    entries = cast(list[object], entries)
    if not entries or any(not isinstance(entry, dict) for entry in entries):
        raise BusinessGateError("invalid_committed_business_proof")
    return tuple(cast(dict[str, object], entry) for entry in entries)


def ensure_business_tables(cursor: Cursor) -> None:
    tables: dict[str, tuple[str, ...]] = {
        "myems_system_db": ("tbl_meters", "tbl_points", "tbl_meters_points", "tbl_jishiyun_alerts"),
        "myems_historical_db": ("tbl_jishiyun_energy_readings", "tbl_energy_value"),
        "myems_energy_db": ("tbl_meter_hourly",),
        "myems_billing_db": ("tbl_meter_hourly",),
        "myems_carbon_db": ("tbl_meter_hourly",),
    }
    for schema, names in tables.items():
        cursor.execute(
            "SELECT TABLE_NAME, ENGINE FROM information_schema.TABLES "
            "WHERE TABLE_SCHEMA = %s AND TABLE_NAME IN (" + ",".join(["%s"] * len(names)) + ")",
            (schema, *names),
        )
        rows = cursor.fetchall()
        if len(rows) != len(names) or {r[0] for r in rows} != set(names) or any(r[1] != "InnoDB" for r in rows):
            raise BusinessGateError("business_tables_must_exist_and_use_innodb")


def load_meter_hour(cursor: Cursor, source_id: int, scope: ScopeMeter, start: datetime, end: datetime) -> MeterHour:
    if (
        scope.effective_start is None or scope.effective_start > start
        or (scope.effective_end is not None and scope.effective_end < end)
    ):
        raise BusinessGateError("run_scope_does_not_cover_full_hour")
    cursor.execute(
        "SELECT meter_id,point_id,external_id,configuration_status,is_enabled,archive_json "
        "FROM myems_system_db.tbl_jishiyun_meter_mappings "
        "WHERE data_source_id=%s AND external_address=%s FOR UPDATE", (source_id, scope.address),
    )
    mapping = cursor.fetchone()
    if mapping is None or mapping[2:5] != (scope.external_id, "ready", 1):
        raise BusinessGateError("mapping_must_remain_ready_enabled_and_match_run_identity")
    meter_id, point_id = positive_id(mapping[0], "Meter ID"), positive_id(mapping[1], "Point ID")
    cursor.execute(
        "SELECT hourly_low_limit,hourly_high_limit FROM myems_system_db.tbl_meters WHERE id=%s FOR UPDATE",
        (meter_id,),
    )
    limits = cursor.fetchone()
    cursor.execute(
        "SELECT data_source_id,object_type,units,ratio,offset_constant,is_virtual,is_trend,address,high_limit "
        "FROM myems_system_db.tbl_points WHERE id=%s FOR UPDATE", (point_id,),
    )
    point = cursor.fetchone()
    if limits is None or point is None or (
        point[:3] != (source_id, "JISHIYUN_REGISTER", "kWh")
        or stored_decimal(point[3]) != 1 or stored_decimal(point[4]) != 0 or point[5:7] != (0, 1)
    ):
        raise BusinessGateError("exclusive_staged_kwh_point_with_no_scaling_required")
    try:
        address = json.loads(point[7]) if isinstance(point[7], str) else None
        archive = json.loads(mapping[5]) if isinstance(mapping[5], str) else None
    except ValueError:
        raise BusinessGateError("invalid_mapping_ownership_metadata") from None
    if not isinstance(archive, dict):
        raise BusinessGateError("invalid_mapping_ownership_metadata")
    archive = cast(dict[str, object], archive)
    if (
        address != {"provider": "jishiyun", "address": scope.address, "field": "ZYGDNSZ"}
        or archive.get("Address") != scope.address
        or type(archive.get("ID")) is not int or archive["ID"] != scope.external_id
    ):
        raise BusinessGateError("mapping_ownership_metadata_changed")
    cursor.execute(
        "SELECT meter_id,point_id FROM myems_system_db.tbl_meters_points "
        "WHERE meter_id=%s OR point_id=%s FOR UPDATE", (meter_id, point_id),
    )
    if cursor.fetchall() != [(meter_id, point_id)]:
        raise BusinessGateError("exclusive_meter_point_association_required")
    lower, upper = start.replace(tzinfo=None), end.replace(tzinfo=None)
    cursor.execute(
        "SELECT id FROM myems_system_db.tbl_jishiyun_alerts "
        "WHERE data_source_id=%s AND external_address=%s AND status='active' "
        "AND reading_datetime_utc >= %s AND reading_datetime_utc <= %s LIMIT 1 FOR UPDATE",
        (source_id, scope.address, lower, upper),
    )
    if cursor.fetchone() is not None:
        raise BusinessGateError("unresolved_quality_alert_in_hour_or_boundary")
    cursor.execute(
        "SELECT reading_datetime_utc,forward_active_register,revision,quality,external_id "
        "FROM myems_historical_db.tbl_jishiyun_energy_readings "
        "WHERE data_source_id=%s AND external_address=%s AND date_type='mi15' AND value_type='SJZ' "
        "AND reading_datetime_utc >= %s AND reading_datetime_utc <= %s ORDER BY reading_datetime_utc FOR UPDATE",
        (source_id, scope.address, lower, upper),
    )
    rows = cursor.fetchall()
    if any(row[3:] != ("valid", scope.external_id) for row in rows):
        raise BusinessGateError("invalid_quality_or_external_identity")
    registers = tuple(Register(
        db_time(row[0]), _number(row[1]), positive_id(row[2], "Raw revision"),
    ) for row in rows)
    consumption = calculate_hour(start, registers, _number(limits[0]), _number(limits[1]), _number(point[8]))
    return MeterHour(scope.address, meter_id, point_id, registers, consumption)


def normalize_hour(
    queue: RunQueue, run_id: int, actor: int, *, confirm_timestamp_kwh: bool, apply: bool = False,
) -> dict[str, object]:
    """Preview or atomically first-write an accepted run's complete hours.

    Staged point ownership excludes legacy normalization. Source-row locking
    serializes this writer with acquisition and other business-hour commands.
    Corrections to already committed hours are rejected, never silently rewritten.
    """
    run_id, actor = positive_id(run_id, "Run ID"), positive_id(actor, "Actor ID")
    if confirm_timestamp_kwh is not True:
        raise BusinessGateError("confirm_timestamp_register_semantics_and_kwh_before_calculation")
    with queue.transaction() as cursor:
        ensure_business_tables(cursor)
        cursor.execute(
            "SELECT r.status,r.window_start_utc,r.window_end_utc,r.scope_snapshot_json,r.expected_count,"
            "r.valid_count,r.missing_count,r.invalid_count,r.unexpected_count "
            "FROM myems_system_db.tbl_jishiyun_runs r "
            "JOIN myems_system_db.tbl_jishiyun_tasks t ON t.id=r.task_id AND t.data_source_id=r.data_source_id "
            "WHERE r.id=%s AND r.data_source_id=%s "
            "AND t.task_type IN ('energy_collect','energy_backfill') FOR UPDATE",
            (run_id, queue.source_id),
        )
        run = cursor.fetchone()
        if run is None or run[0] != "done":
            raise BusinessGateError("completed_successful_energy_run_required")
        start, end = db_time(run[1]), db_time(run[2])
        duration = end - start
        if (
            start.minute or start.second or start.microsecond
            or end.minute or end.second or end.microsecond
            or not timedelta(hours=1) <= duration <= timedelta(days=1)
        ):
            raise BusinessGateError("aligned_whole_hour_run_up_to_one_day_required")
        hour_count = duration // timedelta(hours=1)
        scope = tuple(sorted(snapshot_scope(run[3]), key=lambda meter: meter.address))
        if not scope or run[4:] != (len(scope) * 4 * hour_count, len(scope) * 4 * hour_count, 0, 0, 0):
            raise BusinessGateError("run_counts_do_not_prove_complete_scope")
        slices = tuple(
            (start + timedelta(hours=index), tuple(
                load_meter_hour(cursor, queue.source_id, meter, start + timedelta(hours=index),
                       start + timedelta(hours=index + 1)) for meter in scope
            )) for index in range(hour_count)
        )
        proofs: list[dict[str, object]] = [{
            "version": 1, "start_utc": timestamp.isoformat(),
            "end_utc": (timestamp + timedelta(hours=1)).isoformat(),
            "timestamp_register_kwh_confirmed": True, "meters": [hour.summary() for hour in hours],
        } for timestamp, hours in slices]
        proof: dict[str, object] = proofs[0] if hour_count == 1 else {
            "version": 2, "start_utc": start.isoformat(), "end_utc": end.isoformat(), "hours": proofs,
        }
        encoded = json.dumps(proof, sort_keys=True)
        cursor.execute(
            "SELECT details_json FROM myems_system_db.tbl_jishiyun_events "
            "WHERE data_source_id=%s AND run_id=%s AND event_type='business_hour_committed' FOR UPDATE",
            (queue.source_id, run_id),
        )
        previous = cursor.fetchall()
        from myems_jishiyun.proofs import effective_proofs

        effective = effective_proofs(cursor, queue.source_id, start=start, end=end)
        if previous:
            previous = [(effective[(run_id, "business_hour_committed")],)]
        if previous and previous != [(encoded,)]:
            raise BusinessGateError("committed_input_changed_requires_explicit_historical_recalculation")
        cursor.execute(
            "SELECT e.run_id,e.details_json FROM myems_system_db.tbl_jishiyun_events e "
            "JOIN myems_system_db.tbl_jishiyun_runs r ON r.id=e.run_id AND r.data_source_id=e.data_source_id "
            "WHERE e.data_source_id=%s AND e.event_type='business_hour_committed' "
            "AND r.window_start_utc < %s AND r.window_end_utc > %s FOR UPDATE",
            (queue.source_id, end.replace(tzinfo=None), start.replace(tzinfo=None)),
        )
        trusted: set[str] = set()
        for prior_id, body in cursor.fetchall():
            body = effective[(positive_id(prior_id, "Energy audit run"), "business_hour_committed")]
            trusted.update(json.dumps(entry, sort_keys=True) for entry in committed_hour_proofs(body))
        missing_registers: dict[tuple[int, datetime], tuple[object, ...]] = {}
        missing_hours: list[tuple[object, ...]] = []
        for (timestamp, hours), hour_proof in zip(slices, proofs):
            owned = json.dumps(hour_proof, sort_keys=True) in trusted
            lower = timestamp.replace(tzinfo=None)
            for hour in hours:
                if not owned:
                    for schema in ("myems_billing_db", "myems_carbon_db"):
                        cursor.execute(
                            "SELECT id FROM " + schema + ".tbl_meter_hourly "
                            "WHERE meter_id=%s AND start_datetime_utc >= %s LIMIT 1 FOR UPDATE",
                            (hour.meter_id, lower),
                        )
                        if cursor.fetchone() is not None:
                            raise BusinessGateError("downstream_already_advanced_requires_historical_recalculation")
                for register in hour.readings:
                    register_time = register.timestamp.replace(tzinfo=None)
                    cursor.execute(
                        "SELECT actual_value,is_bad FROM myems_historical_db.tbl_energy_value "
                        "WHERE point_id=%s AND utc_date_time=%s FOR UPDATE", (hour.point_id, register_time),
                    )
                    existing = cursor.fetchall()
                    if existing and (
                        len(existing) != 1 or stored_decimal(existing[0][0]) != register.value or existing[0][1] != 0
                    ):
                        raise BusinessGateError("legacy_register_duplicate_or_value_conflict")
                    if not existing:
                        if owned:
                            raise BusinessGateError("committed_business_rows_missing_requires_manual_review")
                        missing_registers[hour.point_id, register_time] = (hour.point_id, register_time, register.value)
                cursor.execute(
                    "SELECT actual_value FROM myems_energy_db.tbl_meter_hourly "
                    "WHERE meter_id=%s AND start_datetime_utc=%s FOR UPDATE", (hour.meter_id, lower),
                )
                existing = cursor.fetchall()
                if existing:
                    if not owned or len(existing) != 1 or stored_decimal(existing[0][0]) != hour.consumption:
                        raise BusinessGateError("existing_hour_requires_same_run_proof_or_historical_recalculation")
                elif owned:
                    raise BusinessGateError("committed_business_rows_missing_requires_manual_review")
                else:
                    missing_hours.append((hour.meter_id, lower, hour.consumption))
        if previous and (missing_registers or missing_hours):
            raise BusinessGateError("committed_business_rows_missing_requires_manual_review")
        if apply and not previous:
            for parameters in missing_registers.values():
                cursor.execute(
                    "INSERT INTO myems_historical_db.tbl_energy_value "
                    "(point_id,utc_date_time,actual_value,is_bad,is_published) VALUES (%s,%s,%s,FALSE,FALSE)",
                    parameters,
                )
            for parameters in missing_hours:
                cursor.execute(
                    "INSERT INTO myems_energy_db.tbl_meter_hourly "
                    "(meter_id,start_datetime_utc,actual_value) VALUES (%s,%s,%s)", parameters,
                )
            cursor.execute(
                "INSERT INTO myems_system_db.tbl_jishiyun_events "
                "(data_source_id,run_id,actor_user_id,event_type,details_json) "
                "VALUES (%s,%s,%s,'business_hour_committed',%s)", (queue.source_id, run_id, actor, encoded),
            )
        return {
            "status": "already_committed" if previous else ("committed" if apply else "preview"),
            "source_id": queue.source_id, "run_id": run_id, **proof,
            "register_insert_count": len(missing_registers),
            "hour_insert_count": len(missing_hours),
            "automatic_business_processing": False, "billing_carbon_recalculated": False,
        }
