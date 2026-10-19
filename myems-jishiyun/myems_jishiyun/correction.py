"""Hash-approved, atomic correction of already audited cumulative-register hours."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Literal, cast

from myems_jishiyun.execution import RunQueue
from myems_jishiyun.normalization import (
    BusinessGateError, committed_hour_proofs, ensure_business_tables, load_meter_hour,
)
from myems_jishiyun.persistence import Cursor, ScopeMeter, positive_id, snapshot_scope, stored_decimal
from myems_jishiyun.proofs import (
    AffectedHour, BusinessWrite, CorrectionAudit, CorrectionPlan, HourProof, MeterProof, ProofUpdate,
    SettlementAudit, effective_proofs,
)
from myems_jishiyun.settlement import settlement_rate


WriteKey = tuple[str, int, str]
SlotKey = tuple[str, str]


def _encoded(value: object) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=True)


def _changed_slots(before: MeterProof, after: MeterProof) -> set[SlotKey]:
    if any(before[key] != after[key] for key in ("address", "meter_id", "point_id")):
        raise BusinessGateError("correction_mapping_changed")
    if len(before["registers"]) != 5 or len(after["registers"]) != 5:
        raise BusinessGateError("correction_requires_five_audited_registers")
    slots: set[SlotKey] = set()
    for old, new in zip(before["registers"], after["registers"]):
        if old["timestamp_utc"] != new["timestamp_utc"]:
            raise BusinessGateError("correction_register_times_changed")
        if old != new:
            if new["revision"] <= old["revision"]:
                raise BusinessGateError("correction_requires_increasing_raw_revision")
            slots.add((before["address"], old["timestamp_utc"]))
    return slots


def correct_run(queue: RunQueue, run_id: int, actor: int, *, expected_hash: str | None = None,
                apply: bool = False, test_only: bool = True) -> dict[str, object]:
    """Preview exact old/new values, then approve that snapshot hash in a second invocation.

    Original audits are never edited. One append-only event supersedes every
    overlapping run proof, and rows plus audit commit in one source-locked transaction.
    """
    run_id, actor = positive_id(run_id, "Run ID"), positive_id(actor, "Actor ID")
    with queue.transaction() as cursor:
        ensure_business_tables(cursor)
        effective = effective_proofs(cursor, queue.source_id)
        selected = effective.get((run_id, "business_hour_committed"))
        if selected is None:
            raise BusinessGateError("correction_requires_committed_energy_audit")
        energy: dict[int, dict[str, object]] = {
            identity: deepcopy(json.loads(body)) for (identity, kind), body in effective.items()
                  if kind == "business_hour_committed"}
        current: dict[tuple[int, str, str], MeterProof] = {}
        scopes: dict[int, dict[str, ScopeMeter]] = {}

        def live(identity: int, hour: HourProof, meter: MeterProof) -> MeterProof:
            if identity not in scopes:
                cursor.execute(
                    "SELECT scope_snapshot_json FROM myems_system_db.tbl_jishiyun_runs "
                    "WHERE id=%s AND data_source_id=%s AND status='done' FOR UPDATE", (identity, queue.source_id),
                )
                row = cursor.fetchone()
                if row is None:
                    raise BusinessGateError("correction_requires_completed_predecessor_run")
                scopes[identity] = {scope.address: scope for scope in snapshot_scope(row[0])}
            scope = scopes[identity].get(meter["address"])
            if scope is None:
                raise BusinessGateError("correction_mapping_changed")
            key = (identity, hour["start_utc"], meter["address"])
            if key not in current:
                start = datetime.fromisoformat(hour["start_utc"])
                current[key] = cast(MeterProof, load_meter_hour(
                    cursor, queue.source_id, scope,
                    start, start + timedelta(hours=1),
                ).summary())
            return current[key]

        slots: set[SlotKey] = set()
        selected_hours = cast(tuple[HourProof, ...], committed_hour_proofs(selected))
        for hour in selected_hours:
            for meter in hour["meters"]:
                slots.update(_changed_slots(meter, live(run_id, hour, meter)))
        if not slots:
            if apply:
                cursor.execute(
                    "SELECT details_json FROM myems_system_db.tbl_jishiyun_events "
                    "WHERE data_source_id=%s AND run_id=%s AND event_type='business_correction_committed' "
                    "ORDER BY id DESC FOR UPDATE", (queue.source_id, run_id),
                )
                for row in cursor.fetchall():
                    audit = cast(CorrectionAudit, json.loads(str(row[0])))
                    if audit["approval_hash"] == expected_hash:
                        _verify_applied(cursor, audit)
                        return {"status": "already_committed", "run_id": run_id,
                                "approval_hash": expected_hash, "updated_count": 0}
                raise BusinessGateError("correction_approval_not_found")
            return {"status": "no_changes", "run_id": run_id, "updated_count": 0}
        affected: dict[tuple[str, int], AffectedHour] = {}
        # A boundary belongs to both neighboring hours and all overlapping run proofs.
        expanded = True
        while expanded:
            expanded = False
            for identity, document in energy.items():
                for hour in cast(tuple[HourProof, ...], committed_hour_proofs(_encoded(document))):
                    for meter in hour["meters"]:
                        if not any((meter["address"], r["timestamp_utc"]) in slots for r in meter["registers"]):
                            continue
                        key = (hour["start_utc"], meter["meter_id"])
                        replacement = live(identity, hour, meter)
                        added = _changed_slots(meter, replacement) - slots
                        if added:
                            slots.update(added)
                            expanded = True
                        if key in affected and affected[key]["before"] != meter:
                            raise BusinessGateError("correction_conflicting_predecessor_audits")
                        affected[key] = {"before": meter, "after": replacement, "start_utc": hour["start_utc"]}
        for address, timestamp in slots:
            boundary = datetime.fromisoformat(timestamp)
            starts = [boundary.replace(minute=0)]
            if boundary.minute == 0:
                starts.append(boundary - timedelta(hours=1))
            meter_ids = {item["before"]["meter_id"] for item in affected.values()
                         if item["before"]["address"] == address}
            for meter_id in meter_ids:
                for start in starts:
                    if (start.isoformat(), meter_id) in affected:
                        continue
                    for schema in ("myems_energy_db", "myems_billing_db", "myems_carbon_db"):
                        cursor.execute(
                            "SELECT id FROM " + schema + ".tbl_meter_hourly "
                            "WHERE meter_id=%s AND start_datetime_utc=%s LIMIT 1 FOR UPDATE",
                            (meter_id, start.replace(tzinfo=None)),
                        )
                        if cursor.fetchone() is not None:
                            raise BusinessGateError("adjacent_hour_has_no_owned_audit")
        replacements: list[ProofUpdate] = []
        for identity, document in energy.items():
            changed = False
            hours = cast(tuple[HourProof, ...], committed_hour_proofs(_encoded(document)))
            for hour in hours:
                for index, meter in enumerate(hour["meters"]):
                    entry = affected.get((hour["start_utc"], meter["meter_id"]))
                    if entry is not None:
                        hour["meters"][index] = entry["after"]
                        changed = True
            if changed:
                updated: object = hours[0] if document.get("version") == 1 else {**document, "hours": list(hours)}
                replacements.append({"run_id": identity, "kind": "business_hour_committed",
                                     "before": effective[(identity, "business_hour_committed")],
                                     "after": _encoded(updated)})
        settled: set[tuple[str, int]] = set()
        writes: dict[WriteKey, BusinessWrite] = {}
        for (identity, kind), body in effective.items():
            if kind != "business_settlement_committed":
                continue
            settlement = cast(SettlementAudit, json.loads(body))
            changed = False
            if settlement["test_only"] != test_only:
                continue
            for entry in settlement["entries"]:
                key = (entry["start_utc"], entry["meter"]["meter_id"])
                if key not in affected:
                    continue
                item = affected[key]
                if entry["meter"] != item["before"]:
                    raise BusinessGateError("correction_settlement_predecessor_differs")
                rate = settlement_rate(cursor, key[1], datetime.fromisoformat(key[0]), test_only=test_only)
                if rate != entry["rate"]:
                    raise BusinessGateError("correction_rate_changed_requires_separate_review")
                settled.add(key)
                new_meter = item["after"]
                for kind_name, schema in (("billing", "myems_billing_db"), ("carbon", "myems_carbon_db")):
                    multiplier = rate["price_per_kwh" if kind_name == "billing" else "kgco2e_per_kwh"]
                    if not isinstance(multiplier, str):
                        raise BusinessGateError("invalid_correction_rate")
                    value = (Decimal(new_meter["consumption_kwh"]) * Decimal(multiplier)).quantize(
                        Decimal("0.000001"), rounding=ROUND_HALF_UP,
                    )
                    _write(writes, schema + ".tbl_meter_hourly", "meter_id", key[1], "start_datetime_utc",
                           key[0], entry["billing"] if kind_name == "billing" else entry["carbon"], str(value))
                    if kind_name == "billing":
                        entry["billing"] = str(value)
                    else:
                        entry["carbon"] = str(value)
                entry["meter"] = new_meter
                changed = True
            if changed:
                replacements.append({"run_id": identity, "kind": "business_settlement_committed",
                                     "before": body, "after": _encoded(settlement)})
        if settled != set(affected):
            raise BusinessGateError("correction_requires_audited_settlement_for_all_affected_hours")
        for (start, meter_id), item in sorted(affected.items()):
            _write(writes, "myems_energy_db.tbl_meter_hourly", "meter_id", meter_id, "start_datetime_utc",
                   start, item["before"]["consumption_kwh"], item["after"]["consumption_kwh"])
            for old, new in zip(item["before"]["registers"], item["after"]["registers"]):
                _write(writes, "myems_historical_db.tbl_energy_value", "point_id", item["before"]["point_id"],
                       "utc_date_time", old["timestamp_utc"], old["value"], new["value"], register=True)
        from myems_jishiyun.dimensions import correction_dimensions

        dimension_writes = correction_dimensions(cursor, queue.source_id, writes)
        writes.update(dimension_writes)
        plan: CorrectionPlan = {"run_id": run_id, "source_id": queue.source_id, "test_only": test_only,
                "affected_hours": [affected[key] for key in sorted(affected)],
                "proof_updates": sorted(replacements, key=lambda e: (e["run_id"], e["kind"])),
                "writes": [writes[key] for key in sorted(writes)]}
        for write in plan["writes"]:
            _verify_write(cursor, write, "before")
        digest = hashlib.sha256(_encoded(plan).encode()).hexdigest()
        if not apply:
            return {"status": "preview", "approval_hash": digest, **plan}
        if expected_hash != digest:
            raise BusinessGateError("correction_preview_stale_or_approval_hash_missing")
        for write in plan["writes"]:
            if write["before"] == write["after"]:
                continue
            timestamp = datetime.fromisoformat(write["timestamp_utc"]).replace(tzinfo=None)
            cursor.execute(
                "UPDATE " + write["table"] + " SET actual_value=%s WHERE " + write["id_column"] + "=%s AND "
                + write["time_column"] + "=%s",
                (Decimal(write["after"]), write["identity"], timestamp),
            )
            if cursor.rowcount != 1:
                raise BusinessGateError("correction_target_changed")
        audit: CorrectionAudit = {**plan, "approval_hash": digest}
        cursor.execute(
            "INSERT INTO myems_system_db.tbl_jishiyun_events "
            "(data_source_id,run_id,actor_user_id,event_type,details_json) "
            "VALUES (%s,%s,%s,'business_correction_committed',%s)",
            (queue.source_id, run_id, actor, _encoded(audit)),
        )
        return {"status": "committed", "run_id": run_id, "approval_hash": digest,
                "affected_hour_count": len(affected),
                "updated_count": sum(w["before"] != w["after"] for w in plan["writes"])}


def _write(writes: dict[WriteKey, BusinessWrite], table: str, id_column: str, identity: int, time_column: str,
           timestamp: str, before: str, after: str, *, register: bool = False) -> None:
    key = (table, identity, timestamp)
    write: BusinessWrite = {"table": table, "id_column": id_column, "identity": identity, "time_column": time_column,
             "timestamp_utc": timestamp, "before": str(before), "after": str(after), "register": register}
    if key in writes and writes[key] != write:
        raise BusinessGateError("correction_conflicting_target_proofs")
    writes[key] = write


def _verify_write(cursor: Cursor, write: BusinessWrite, side: Literal["before", "after"]) -> None:
    extra = ",is_bad" if write["register"] else ""
    if "energy_category_id" in write:
        extra += ",energy_category_id"
    cursor.execute(
        "SELECT actual_value" + extra + " FROM " + write["table"] + " WHERE "
        + write["id_column"] + "=%s AND " + write["time_column"] + "=%s FOR UPDATE",
        (write["identity"], datetime.fromisoformat(write["timestamp_utc"]).replace(tzinfo=None)),
    )
    rows = cursor.fetchall()
    if (
        len(rows) != 1 or stored_decimal(rows[0][0]) != Decimal(write[side])
        or (write["register"] and rows[0][1] != 0)
        or ("energy_category_id" in write and rows[0][-1] != write["energy_category_id"])
    ):
        raise BusinessGateError("correction_target_missing_duplicate_or_modified")


def _verify_applied(cursor: Cursor, audit: CorrectionAudit) -> None:
    for write in audit["writes"]:
        _verify_write(cursor, write, "after")
    for item in audit["affected_hours"]:
        meter = item["after"]
        for register in meter["registers"]:
            cursor.execute(
                "SELECT forward_active_register,revision,quality FROM myems_historical_db.tbl_jishiyun_energy_readings "
                "WHERE data_source_id=%s AND external_address=%s AND reading_datetime_utc=%s "
                "AND date_type='mi15' AND value_type='SJZ' FOR UPDATE",
                (audit["source_id"], meter["address"],
                 datetime.fromisoformat(register["timestamp_utc"]).replace(tzinfo=None)),
            )
            rows = cursor.fetchall()
            if (
                len(rows) != 1 or stored_decimal(rows[0][0]) != Decimal(register["value"])
                or rows[0][1:] != (register["revision"], "valid")
            ):
                raise BusinessGateError("correction_replay_inputs_changed")
