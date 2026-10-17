"""Append-only supersession of immutable first-write business audits."""

import json
from datetime import datetime
from typing import TypedDict, cast

from myems_jishiyun.persistence import Cursor, PersistenceError, positive_id


class RegisterProof(TypedDict):
    timestamp_utc: str
    value: str
    revision: int


class MeterProof(TypedDict):
    address: str
    meter_id: int
    point_id: int
    registers: list[RegisterProof]
    consumption_kwh: str


class HourProof(TypedDict):
    version: int
    start_utc: str
    end_utc: str
    timestamp_register_kwh_confirmed: bool
    meters: list[MeterProof]


class ProofUpdate(TypedDict):
    run_id: int
    kind: str
    before: str
    after: str


class BusinessWriteBase(TypedDict):
    table: str
    id_column: str
    identity: int
    time_column: str
    timestamp_utc: str
    before: str
    after: str
    register: bool


class BusinessWrite(BusinessWriteBase, total=False):
    energy_category_id: int


class AffectedHour(TypedDict):
    before: MeterProof
    after: MeterProof
    start_utc: str


class CorrectionPlan(TypedDict):
    run_id: int
    source_id: int
    test_only: bool
    affected_hours: list[AffectedHour]
    proof_updates: list[ProofUpdate]
    writes: list[BusinessWrite]


class CorrectionAudit(CorrectionPlan):
    approval_hash: str


class DimensionConfigBase(TypedDict):
    meter_ids: list[int]
    space_id: int
    equipment_id: int
    energy_category_id: int


class DimensionConfig(DimensionConfigBase, total=False):
    product_id: int


class DimensionEntry(TypedDict):
    table: str
    identity: int
    id_column: str
    timestamp_utc: str
    after: str


class DimensionAudit(TypedDict):
    configuration: DimensionConfig
    entries: list[DimensionEntry]


class SettlementEntry(TypedDict):
    start_utc: str
    meter: MeterProof
    rate: dict[str, object]
    billing: str
    carbon: str


class SettlementAudit(TypedDict):
    version: int
    test_only: bool
    entries: list[SettlementEntry]


def effective_proofs(cursor: Cursor, source_id: int, *, start: datetime | None = None,
                     end: datetime | None = None) -> dict[tuple[int, str], str]:
    if (start is None) != (end is None):
        raise PersistenceError("Both proof window boundaries are required")
    parameters: tuple[object, ...] = (source_id,)
    window = ""
    if start is not None and end is not None:
        if start >= end:
            raise PersistenceError("Proof window must increase")
        window = (" AND (e.event_type='business_correction_committed' OR "
                  "(r.window_start_utc < %s AND r.window_end_utc > %s))")
        parameters += (end.replace(tzinfo=None), start.replace(tzinfo=None))
    cursor.execute(
        "SELECT e.run_id,e.event_type,e.details_json FROM myems_system_db.tbl_jishiyun_events e "
        "LEFT JOIN myems_system_db.tbl_jishiyun_runs r ON r.id=e.run_id AND r.data_source_id=e.data_source_id "
        "WHERE e.data_source_id=%s AND e.event_type IN "
        "('business_hour_committed','business_settlement_committed','business_correction_committed') "
        + window + " ORDER BY e.id FOR UPDATE", parameters,
    )
    result: dict[tuple[int, str], str] = {}
    for run_id, kind, body in cursor.fetchall():
        if not isinstance(body, str):
            raise PersistenceError("Invalid business audit body")
        if kind != "business_correction_committed":
            if kind not in ("business_hour_committed", "business_settlement_committed"):
                raise PersistenceError("Invalid business audit kind")
            key = (positive_id(run_id, "Audit run ID"), kind)
            if key in result:
                raise PersistenceError("Duplicate first-write business audit")
            result[key] = body
            continue
        proof = cast(CorrectionAudit, json.loads(body))
        for entry in proof["proof_updates"]:
            key = (entry["run_id"], entry["kind"])
            if start is not None and key not in result:
                continue
            if key not in result or result[key] != entry["before"]:
                raise PersistenceError("Correction proof chain does not match its predecessor")
            result[key] = entry["after"]
    return result
