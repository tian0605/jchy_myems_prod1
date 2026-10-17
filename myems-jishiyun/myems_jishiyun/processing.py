"""Read-only phase status derived from current inputs, immutable proofs and target rows."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from decimal import Decimal
from typing import TypedDict, cast

from myems_jishiyun.normalization import BusinessGateError, committed_hour_proofs, load_meter_hour
from myems_jishiyun.persistence import Cursor, db_time, positive_id, snapshot_scope, stored_decimal
from myems_jishiyun.proofs import (
    DimensionConfig, HourProof, MeterProof, SettlementAudit, effective_proofs,
)
from myems_jishiyun.dimensions import (
    SCHEMAS, configuration, dimension_proofs, dimension_rows, member_hour_value,
)


class Phase(TypedDict):
    status: str
    reason: str | None


class HourStatus(TypedDict):
    start_utc: str
    end_utc: str
    energy: Phase
    settlement: Phase
    dimensions: Phase


class ReadingStatus(TypedDict):
    energy_status: str
    settlement_status: str
    dimensions_status: str
    processing_details: list[HourStatus]


def phase(status: str, reason: str | None = None) -> Phase:
    return {"status": status, "reason": reason}


def summarize(values: list[Phase]) -> str:
    states = {value["status"] for value in values}
    if len(states) == 1:
        return next(iter(states))
    if "processed" in states:
        return "partial"
    for state in ("review_required", "error", "blocked", "running", "queued", "pending"):
        if state in states:
            return state
    return "not_applicable"


def reading_processing(cursor: Cursor, source_id: int,
                       readings: list[tuple[str, datetime]]) -> list[ReadingStatus]:
    """Caller holds the source row lock; no flags or operator-supplied notes are persisted."""
    if not readings:
        return []
    times = [db_time(value) for _, value in readings]
    start = min(times).replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
    end = max(times).replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    effective = effective_proofs(cursor, source_id, start=start, end=end)
    cursor.execute(
        "SELECT r.id,r.window_start_utc,r.window_end_utc,r.scope_snapshot_json,j.status,j.gate_code "
        "FROM myems_system_db.tbl_jishiyun_runs r "
        "LEFT JOIN myems_system_db.tbl_jishiyun_business_jobs j ON j.run_id=r.id AND j.data_source_id=r.data_source_id "
        "WHERE r.data_source_id=%s AND r.window_start_utc < %s AND r.window_end_utc > %s "
        "ORDER BY r.id DESC",
        (source_id, end.replace(tzinfo=None), start.replace(tzinfo=None)),
    )
    runs = cursor.fetchall()
    cursor.execute(
        "SELECT external_address,reading_datetime_utc,quality FROM myems_historical_db.tbl_jishiyun_energy_readings "
        "WHERE data_source_id=%s AND reading_datetime_utc >= %s AND reading_datetime_utc <= %s "
        "AND date_type='mi15' AND value_type='SJZ'",
        (source_id, min(times).replace(tzinfo=None), max(times).replace(tzinfo=None)),
    )
    invalid = {(str(row[0]), db_time(row[1])) for row in cursor.fetchall() if row[2] != "valid"}
    energy: dict[tuple[str, datetime], list[tuple[int, MeterProof]]] = {}
    settlements: dict[tuple[str, datetime], list[tuple[MeterProof, str, str]]] = {}
    for (identity, kind), body in effective.items():
        if kind == "business_hour_committed":
            for hour in cast(tuple[HourProof, ...], committed_hour_proofs(body)):
                for meter in hour["meters"]:
                    energy.setdefault((meter["address"], datetime.fromisoformat(hour["start_utc"])), []).append((identity, meter))
        elif kind == "business_settlement_committed":
            for entry in cast(SettlementAudit, json.loads(body))["entries"]:
                settlements.setdefault((entry["meter"]["address"], datetime.fromisoformat(entry["start_utc"])), []).append(
                    (entry["meter"], entry["billing"], entry["carbon"]),
                )
    scopes = {positive_id(row[0], "Processing run"): snapshot_scope(row[3]) for row in runs}
    config: DimensionConfig | None = None
    config_gate: str | None = None
    try:
        config = configuration(cursor, source_id)
    except BusinessGateError as exc:
        config_gate = str(exc)
    committed_dimensions = dimension_proofs(cursor, source_id) if config is not None else {}
    hour_cache: dict[tuple[str, datetime], HourStatus] = {}
    dimension_cache: dict[datetime, Phase] = {}

    def inputs_match(meter: MeterProof, external_id: int, hour: datetime) -> bool:
        cursor.execute(
            "SELECT reading_datetime_utc,forward_active_register,revision,quality,external_id "
            "FROM myems_historical_db.tbl_jishiyun_energy_readings "
            "WHERE data_source_id=%s AND external_address=%s AND date_type='mi15' AND value_type='SJZ' "
            "AND reading_datetime_utc >= %s AND reading_datetime_utc <= %s ORDER BY reading_datetime_utc",
            (source_id, meter["address"], hour.replace(tzinfo=None),
             (hour + timedelta(hours=1)).replace(tzinfo=None)),
        )
        rows = cursor.fetchall()
        return len(rows) == 5 and len(meter["registers"]) == 5 and all(
            db_time(row[0]) == datetime.fromisoformat(register["timestamp_utc"])
            and stored_decimal(row[1]) == Decimal(register["value"])
            and row[2:] == (register["revision"], "valid", external_id)
            for row, register in zip(rows, meter["registers"])
        )

    def targets_match(meter: MeterProof, hour: datetime) -> bool:
        for register in meter["registers"]:
            cursor.execute(
                "SELECT actual_value,is_bad FROM myems_historical_db.tbl_energy_value "
                "WHERE point_id=%s AND utc_date_time=%s",
                (meter["point_id"], datetime.fromisoformat(register["timestamp_utc"]).replace(tzinfo=None)),
            )
            rows = cursor.fetchall()
            if len(rows) != 1 or stored_decimal(rows[0][0]) != Decimal(register["value"]) or rows[0][1] != 0:
                return False
        rows = dimension_rows(cursor, "myems_energy_db.tbl_meter_hourly", "meter_id", meter["meter_id"],
                     hour.replace(tzinfo=None))
        return len(rows) == 1 and stored_decimal(rows[0][0]) == Decimal(meter["consumption_kwh"])

    def dimension_status(hour: datetime) -> Phase:
        if hour in dimension_cache:
            return dimension_cache[hour]
        if config_gate is not None:
            return phase("review_required", config_gate)
        if config is None:
            return phase("not_applicable", "dimension_configuration_not_enabled")
        missing = False
        for schema in SCHEMAS:
            total = Decimal(0)
            for member in config["meter_ids"]:
                try:
                    total += member_hour_value(dimension_rows(cursor, schema + ".tbl_meter_hourly", "meter_id", member,
                                         hour.replace(tzinfo=None)))
                except BusinessGateError as exc:
                    return phase("blocked", str(exc))
            for kind, identity in (("space", config["space_id"]), ("equipment", config["equipment_id"])):
                table = schema + ".tbl_" + kind + "_input_category_hourly"
                key = (table, identity, hour.isoformat())
                try:
                    rows = dimension_rows(cursor, table, kind + "_id", identity, hour.replace(tzinfo=None),
                                 config["energy_category_id"])
                except BusinessGateError as exc:
                    return phase("review_required", str(exc))
                if not rows and key not in committed_dimensions:
                    missing = True
                elif len(rows) != 1 or stored_decimal(rows[0][0]) != total or committed_dimensions.get(key) != total:
                    return phase("review_required", "dimension_proof_or_target_mismatch")
        result = phase("pending", "dimension_aggregation_pending") if missing else phase("processed")
        dimension_cache[hour] = result
        return result

    def inspect(address: str, hour: datetime) -> HourStatus:
        key = (address, hour)
        if key in hour_cache:
            return hour_cache[key]
        candidates = [
            row for row in runs if db_time(row[1]) <= hour and db_time(row[2]) >= hour + timedelta(hours=1)
            and any(meter.address == address for meter in scopes[positive_id(row[0], "Processing run")])
        ]
        related = phase("pending", "no_full_hour_business_run")
        if candidates:
            latest = candidates[0]
            status = latest[4]
            reason = str(latest[5]) if latest[5] else None
            related = phase(str(status), reason) if status in ("blocked", "error", "running", "queued") \
                else phase("pending", "business_job_pending")
        energy_phase = related
        settlement_phase = phase("pending", "energy_processing_required")
        dimension_phase = phase("pending", "settlement_processing_required") if config is not None \
            else phase("review_required", config_gate) if config_gate else phase(
                "not_applicable", "dimension_configuration_not_enabled",
            )
        proofs = energy.get(key, [])
        if proofs:
            identity, meter = proofs[0]
            if any(proof != meter for _, proof in proofs):
                energy_phase = phase("review_required", "conflicting_effective_energy_proofs")
            else:
                scope = next(item for item in scopes[identity] if item.address == address)
                if not inputs_match(meter, scope.external_id, hour):
                    energy_phase = phase("review_required", "committed_input_changed_requires_explicit_historical_recalculation")
                elif not targets_match(meter, hour):
                    energy_phase = phase("review_required", "committed_business_rows_changed_or_missing")
                else:
                    energy_phase = phase("processed")
            if energy_phase["status"] == "processed":
                entries = settlements.get(key, [])
                if entries:
                    if any(entry != entries[0] for entry in entries) or entries[0][0] != meter:
                        settlement_phase = phase("review_required", "settlement_proof_input_mismatch")
                    else:
                        amounts = entries[0][1:]
                        matching = True
                        for schema, amount in zip(SCHEMAS[1:], amounts):
                            rows = dimension_rows(cursor, schema + ".tbl_meter_hourly", "meter_id", meter["meter_id"],
                                         hour.replace(tzinfo=None))
                            if len(rows) != 1 or stored_decimal(rows[0][0]) != Decimal(amount):
                                matching = False
                        settlement_phase = phase("processed") if matching else phase(
                            "review_required", "committed_settlement_changed_or_missing",
                        )
                else:
                    settlement_phase = related if related["status"] in ("blocked", "error", "running", "queued") \
                        else phase("pending", "settlement_processing_pending")
                if settlement_phase["status"] == "processed":
                    dimension_phase = dimension_status(hour)
        else:
            # A recorded failure is meaningful, but zero-filled rows or unaudited target rows never prove success.
            if candidates:
                scope = next(item for item in scopes[positive_id(candidates[0][0], "Processing run")]
                             if item.address == address)
                try:
                    load_meter_hour(cursor, source_id, scope, hour, hour + timedelta(hours=1))
                except BusinessGateError as exc:
                    energy_phase = phase("blocked", str(exc))
        result: HourStatus = {"start_utc": hour.isoformat(), "end_utc": (hour + timedelta(hours=1)).isoformat(),
                              "energy": energy_phase, "settlement": settlement_phase, "dimensions": dimension_phase}
        hour_cache[key] = result
        return result

    result: list[ReadingStatus] = []
    for (address, _), timestamp in zip(readings, times):
        hour = timestamp.replace(minute=0, second=0, microsecond=0)
        hours = [hour - timedelta(hours=1), hour] if timestamp == hour else [hour]
        details = [inspect(address, value) for value in hours]
        if (address, timestamp) in invalid:
            details = [{**detail, "energy": phase("blocked", "raw_quality_invalid_or_conflict"),
                        "settlement": phase("pending", "energy_processing_required"),
                        "dimensions": phase("pending", "energy_processing_required")
                        if config is not None else detail["dimensions"]} for detail in details]
        result.append({
            "energy_status": summarize([detail["energy"] for detail in details]),
            "settlement_status": summarize([detail["settlement"] for detail in details]),
            "dimensions_status": summarize([detail["dimensions"] for detail in details]),
            "processing_details": details,
        })
    return result
