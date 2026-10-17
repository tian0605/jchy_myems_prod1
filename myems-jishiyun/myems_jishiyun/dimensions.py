"""Explicit direct-meter pilot dimensions, never incomplete or double-counted sums."""

import json
from datetime import datetime, timedelta
from decimal import Decimal
from typing import cast

from myems_jishiyun.execution import RunQueue
from myems_jishiyun.normalization import BusinessGateError
from myems_jishiyun.persistence import Cursor, positive_id, stored_decimal
from myems_jishiyun.proofs import BusinessWrite, CorrectionAudit, DimensionAudit, DimensionConfig, DimensionEntry


SCHEMAS = ("myems_energy_db", "myems_billing_db", "myems_carbon_db")


def configuration(cursor: Cursor, source_id: int) -> DimensionConfig | None:
    cursor.execute(
        "SELECT details_json FROM myems_system_db.tbl_jishiyun_events "
        "WHERE data_source_id=%s AND event_type='pilot_dimensions_configured' ORDER BY id FOR UPDATE", (source_id,),
    )
    rows = cursor.fetchall()
    if not rows:
        return None
    if len(rows) != 1:
        raise BusinessGateError("ambiguous_pilot_dimension_configuration")
    config = cast(DimensionConfig, json.loads(str(rows[0][0])))
    meters = config["meter_ids"]
    if "product_id" in config:
        marker = "JISHIYUN_PILOT_SOURCE:" + str(source_id)
        for table, identity in (("tbl_spaces", config["space_id"]), ("tbl_equipments", config["equipment_id"])):
            cursor.execute("SELECT description,is_input_counted,is_output_counted FROM myems_system_db."
                           + table + " WHERE id=%s FOR UPDATE", (identity,))
            if cursor.fetchone() != (marker, 0, 0):
                raise BusinessGateError("dimension_writer_ownership_or_parent_counting_changed")
        cursor.execute(
            "SELECT meter_id FROM myems_system_db.tbl_spaces_meters sm JOIN myems_system_db.tbl_meters m "
            "ON m.id=sm.meter_id WHERE sm.space_id=%s AND m.product_id=%s FOR UPDATE",
            (config["space_id"], config["product_id"]),
        )
        if sorted(positive_id(row[0], "Product member") for row in cursor.fetchall()) != sorted(meters):
            raise BusinessGateError("dimension_product_membership_changed")
    cursor.execute(
        "SELECT meter_id FROM myems_system_db.tbl_jishiyun_meter_mappings "
        "WHERE data_source_id=%s AND is_enabled=1 AND configuration_status='ready' FOR UPDATE", (source_id,),
    )
    if sorted(positive_id(row[0], "Dimension meter") for row in cursor.fetchall()) != sorted(meters):
        raise BusinessGateError("dimension_meter_scope_changed")
    for kind in ("space", "equipment"):
        identity = config["space_id"] if kind == "space" else config["equipment_id"]
        cursor.execute(
            "SELECT meter_id" + (",is_output" if kind == "equipment" else "")
            + " FROM myems_system_db.tbl_" + kind + "s_meters WHERE " + kind + "_id=%s FOR UPDATE",
            (identity,),
        )
        links = cursor.fetchall()
        if sorted(positive_id(row[0], "Dimension member") for row in links) != sorted(meters) or (
            kind == "equipment" and any(row[1] != 0 for row in links)
        ):
            raise BusinessGateError("dimension_membership_changed")
        # No indirect inputs: a meter must have exactly one path into each sum.
        relations = ("virtual_meters", "offline_meters") if kind == "equipment" else (
            "virtual_meters", "offline_meters", "equipments", "combined_equipments", "shopfloors", "stores", "tenants",
        )
        for relation in relations:
            if kind == "space" and relation == "equipments":
                cursor.execute(
                    "SELECT e.id,e.is_input_counted FROM myems_system_db.tbl_equipments e "
                    "JOIN myems_system_db.tbl_spaces_equipments x ON x.equipment_id=e.id "
                    "WHERE x.space_id=%s FOR UPDATE", (identity,),
                )
                if any(row != (config["equipment_id"], 0) for row in cursor.fetchall()):
                    raise BusinessGateError("dimension_equipment_path_would_double_count")
                continue
            cursor.execute(
                "SELECT id FROM myems_system_db.tbl_" + kind + "s_" + relation
                + " WHERE " + kind + "_id=%s LIMIT 1 FOR UPDATE", (identity,),
            )
            if cursor.fetchone() is not None:
                raise BusinessGateError("dimension_indirect_input_requires_topology_review")
        if kind == "space":
            cursor.execute("SELECT id FROM myems_system_db.tbl_spaces WHERE parent_space_id=%s LIMIT 1 FOR UPDATE",
                           (identity,))
            if cursor.fetchone() is not None:
                raise BusinessGateError("dimension_child_space_requires_topology_review")
    cursor.execute(
        "SELECT id,energy_category_id FROM myems_system_db.tbl_meters WHERE id IN ("
        + ",".join(["%s"] * len(meters)) + ") FOR UPDATE", tuple(meters),
    )
    if sorted(cursor.fetchall()) != sorted((meter, config["energy_category_id"]) for meter in meters):
        raise BusinessGateError("dimension_energy_category_changed")
    for schema in SCHEMAS:
        for kind in ("space", "equipment"):
            table = "tbl_" + kind + "_input_category_hourly"
            cursor.execute(
                "SELECT ENGINE FROM information_schema.TABLES WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s",
                (schema, table),
            )
            if cursor.fetchone() != ("InnoDB",):
                raise BusinessGateError("dimension_targets_must_use_innodb")
    return config


def _proofs(cursor: Cursor, source_id: int) -> dict[tuple[str, int, str], Decimal]:
    cursor.execute(
        "SELECT event_type,details_json FROM myems_system_db.tbl_jishiyun_events WHERE data_source_id=%s "
        "AND event_type IN ('dimension_hours_committed','business_correction_committed') ORDER BY id FOR UPDATE",
        (source_id,),
    )
    result: dict[tuple[str, int, str], Decimal] = {}
    for kind, body in cursor.fetchall():
        if kind == "dimension_hours_committed":
            entries = cast(DimensionAudit, json.loads(str(body)))["entries"]
        else:
            entries = cast(CorrectionAudit, json.loads(str(body)))["writes"]
        for entry in entries:
            if "_input_category_hourly" in entry["table"]:
                key = (entry["table"], entry["identity"], entry["timestamp_utc"])
                result[key] = Decimal(entry["after"])
    return result


def _rows(cursor: Cursor, table: str, column: str, identity: int, start: datetime,
          category: int | None = None) -> list[tuple[object, ...]]:
    cursor.execute("SELECT actual_value" + (",energy_category_id" if category is not None else "")
                   + " FROM " + table + " WHERE " + column
                   + "=%s AND start_datetime_utc=%s FOR UPDATE", (identity, start))
    rows = cursor.fetchall()
    if category is not None and any(row[1] != category for row in rows):
        raise BusinessGateError("dimension_target_category_changed")
    return rows


def _value(rows: list[tuple[object, ...]]) -> Decimal:
    if len(rows) != 1:
        raise BusinessGateError("dimension_requires_every_member_hour_without_duplicates")
    value = stored_decimal(rows[0][0])
    if value is None or value < 0:
        raise BusinessGateError("dimension_requires_every_member_hour_without_duplicates")
    return value


dimension_proofs = _proofs
dimension_rows = _rows
member_hour_value = _value


def aggregate_dimensions(queue: RunQueue, actor: int, start: datetime, end: datetime, *,
                         apply: bool = False) -> dict[str, object]:
    actor = positive_id(actor, "Actor ID")
    if (
        start.utcoffset() is None or end.utcoffset() is None or start >= end
        or any(value.minute or value.second or value.microsecond for value in (start, end))
        or end - start > timedelta(days=1)
    ):
        raise BusinessGateError("dimension_aligned_window_up_to_one_day_required")
    from datetime import timezone

    start, end = start.astimezone(timezone.utc), end.astimezone(timezone.utc)
    with queue.transaction() as cursor:
        config = configuration(cursor, queue.source_id)
        if config is None:
            raise BusinessGateError("approved_dimension_configuration_required")
        proofs = _proofs(cursor, queue.source_id)
        entries: list[DimensionEntry] = []
        hour = start
        while hour < end:
            for schema in SCHEMAS:
                total = Decimal(0)
                for meter in config["meter_ids"]:
                    rows = _rows(cursor, schema + ".tbl_meter_hourly", "meter_id", meter, hour.replace(tzinfo=None))
                    total += _value(rows)
                for kind in ("space", "equipment"):
                    table = schema + ".tbl_" + kind + "_input_category_hourly"
                    identity = config["space_id"] if kind == "space" else config["equipment_id"]
                    key = (table, identity, hour.isoformat())
                    rows = _rows(cursor, table, kind + "_id", identity, hour.replace(tzinfo=None),
                                 config["energy_category_id"])
                    if key in proofs:
                        if len(rows) != 1 or stored_decimal(rows[0][0]) != total or proofs[key] != total:
                            raise BusinessGateError("dimension_proof_changed_requires_correction")
                    elif rows:
                        raise BusinessGateError("dimension_unowned_rows_require_review")
                    else:
                        entries.append({"table": table, "identity": identity, "id_column": kind + "_id",
                                        "timestamp_utc": hour.isoformat(), "after": str(total)})
            hour += timedelta(hours=1)
        if apply and entries:
            for entry in entries:
                cursor.execute(
                    "INSERT INTO " + entry["table"] + " (" + entry["id_column"]
                    + ",energy_category_id,start_datetime_utc,actual_value) VALUES (%s,%s,%s,%s)",
                    (entry["identity"], config["energy_category_id"],
                     datetime.fromisoformat(entry["timestamp_utc"]).replace(tzinfo=None), Decimal(entry["after"])),
                )
            cursor.execute(
                "INSERT INTO myems_system_db.tbl_jishiyun_events "
                "(data_source_id,actor_user_id,event_type,details_json) "
                "VALUES (%s,%s,'dimension_hours_committed',%s)",
                (queue.source_id, actor, json.dumps({"configuration": config, "entries": entries}, sort_keys=True)),
            )
        return {"status": "committed" if apply and entries else ("already_committed" if not entries else "preview"),
                "insert_count": len(entries), "start_utc": start.isoformat(), "end_utc": end.isoformat()}


def aggregate_run_dimensions(queue: RunQueue, actor: int, run_id: int) -> None:
    from myems_jishiyun.persistence import db_time

    with queue.transaction() as cursor:
        if configuration(cursor, queue.source_id) is None:
            return
        cursor.execute(
            "SELECT window_start_utc,window_end_utc FROM myems_system_db.tbl_jishiyun_runs "
            "WHERE id=%s AND data_source_id=%s AND status='done'", (run_id, queue.source_id),
        )
        row = cursor.fetchone()
        if row is None:
            raise BusinessGateError("completed_dimension_run_required")
        start, end = db_time(row[0]), db_time(row[1])
    aggregate_dimensions(queue, actor, start, end, apply=True)


def correction_dimensions(cursor: Cursor, source_id: int, writes: dict[tuple[str, int, str], BusinessWrite]
                          ) -> dict[tuple[str, int, str], BusinessWrite]:
    config = configuration(cursor, source_id)
    meter_ids = {entry["identity"] for entry in writes.values() if entry["id_column"] == "meter_id"}
    for table in ("tbl_variables", "tbl_combined_equipments_meters", "tbl_shopfloors_meters",
                  "tbl_stores_meters", "tbl_tenants_meters"):
        cursor.execute(
            "SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA='myems_system_db' AND TABLE_NAME=%s",
            (table,),
        )
        if cursor.fetchone() is None:
            continue
        for meter in meter_ids:
            cursor.execute(
                "SELECT id FROM myems_system_db." + table + " WHERE meter_id=%s"
                + (" AND meter_type='meter'" if table == "tbl_variables" else "") + " LIMIT 1 FOR UPDATE", (meter,),
            )
            if cursor.fetchone() is not None:
                raise BusinessGateError("unsupported_derived_dependency_requires_review")
    # Refuse an unmodeled dependency rather than silently leaving an old space/device report.
    for kind in ("space", "equipment"):
        for meter in meter_ids:
            cursor.execute("SELECT " + kind + "_id FROM myems_system_db.tbl_" + kind
                           + "s_meters WHERE meter_id=%s FOR UPDATE", (meter,))
            for row in cursor.fetchall():
                if config is not None and row[0] == (config["space_id"] if kind == "space" else config["equipment_id"]):
                    continue
                for schema in SCHEMAS:
                    cursor.execute("SELECT id FROM " + schema + ".tbl_" + kind + "_input_category_hourly WHERE "
                                   + kind + "_id=%s LIMIT 1 FOR UPDATE", (row[0],))
                    if cursor.fetchone() is not None:
                        raise BusinessGateError("unmodeled_dimension_dependency_requires_review")
    if config is None:
        return {}
    for schema in SCHEMAS:
        for kind in ("space", "equipment"):
            identity = config["space_id"] if kind == "space" else config["equipment_id"]
            for suffix in ("input_item_hourly", "output_category_hourly"):
                table = "tbl_" + kind + "_" + suffix
                cursor.execute("SELECT TABLE_NAME FROM information_schema.TABLES "
                               "WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s", (schema, table))
                if cursor.fetchone() is None:
                    continue
                cursor.execute("SELECT id FROM " + schema + "." + table + " WHERE " + kind
                               + "_id=%s LIMIT 1 FOR UPDATE", (identity,))
                if cursor.fetchone() is not None:
                    raise BusinessGateError("unsupported_dimension_output_or_item_history")
    proofs = _proofs(cursor, source_id)
    pending: dict[tuple[str, int, str], BusinessWrite] = {}
    for schema in SCHEMAS:
        table = schema + ".tbl_meter_hourly"
        hours = {entry["timestamp_utc"] for entry in writes.values() if entry["table"] == table}
        for timestamp in hours:
            total_before = total_after = Decimal(0)
            for meter in config["meter_ids"]:
                rows = _rows(cursor, table, "meter_id", meter, datetime.fromisoformat(timestamp).replace(tzinfo=None))
                value = _value(rows)
                total_before += value
                replacement = writes.get((table, meter, timestamp))
                total_after += Decimal(replacement["after"]) if replacement else value
            for kind in ("space", "equipment"):
                target = schema + ".tbl_" + kind + "_input_category_hourly"
                identity = config["space_id"] if kind == "space" else config["equipment_id"]
                key = (target, identity, timestamp)
                rows = _rows(cursor, target, kind + "_id", identity,
                             datetime.fromisoformat(timestamp).replace(tzinfo=None), config["energy_category_id"])
                if not rows and key not in proofs:
                    continue
                if len(rows) != 1 or stored_decimal(rows[0][0]) != total_before or proofs.get(key) != total_before:
                    raise BusinessGateError("dimension_target_differs_from_audited_members")
                pending[key] = {"table": target, "id_column": kind + "_id", "identity": identity,
                                "time_column": "start_datetime_utc", "timestamp_utc": timestamp,
                                "before": str(total_before), "after": str(total_after), "register": False,
                                "energy_category_id": config["energy_category_id"]}
    return pending
