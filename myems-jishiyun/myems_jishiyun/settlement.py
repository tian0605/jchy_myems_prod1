"""Controlled billing/carbon first writes for audited Jishiyun hours only."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import cast

from myems_jishiyun.execution import RunQueue
from myems_jishiyun.normalization import (
    BusinessGateError, committed_hour_proofs, ensure_business_tables, load_meter_hour,
)
from myems_jishiyun.persistence import Cursor, db_time, positive_id, snapshot_scope, stored_decimal
from myems_jishiyun.proofs import effective_proofs


def _amount(value: object) -> Decimal:
    number = stored_decimal(value)
    if number is None or not 0 <= number <= Decimal("999999999999999.999999"):
        raise BusinessGateError("invalid_settlement_rate_factor_or_amount")
    rounded = number.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
    if number != rounded:
        raise BusinessGateError("invalid_settlement_rate_factor_or_amount")
    return rounded


def settlement_rate(cursor: Cursor, meter_id: int, start: datetime, *, test_only: bool) -> dict[str, object]:
    end = start + timedelta(hours=1)
    cursor.execute(
        "SELECT m.energy_category_id,m.cost_center_id,c.name,c.unit_of_measure,c.kgco2e,cc.name "
        "FROM myems_system_db.tbl_meters m "
        "JOIN myems_system_db.tbl_energy_categories c ON c.id=m.energy_category_id "
        "JOIN myems_system_db.tbl_cost_centers cc ON cc.id=m.cost_center_id "
        "WHERE m.id=%s FOR UPDATE", (meter_id,),
    )
    category = cursor.fetchone()
    if category is None or category[3] != "kWh":
        raise BusinessGateError("settlement_kwh_category_and_cost_center_required")
    cursor.execute(
        "SELECT t.id,t.name,t.valid_from_datetime_utc,t.valid_through_datetime_utc,t.tariff_type,t.unit_of_price "
        "FROM myems_system_db.tbl_tariffs t "
        "JOIN myems_system_db.tbl_cost_centers_tariffs x ON x.tariff_id=t.id "
        "WHERE x.cost_center_id=%s AND t.energy_category_id=%s "
        "AND t.valid_from_datetime_utc < %s AND t.valid_through_datetime_utc > %s FOR UPDATE",
        (category[1], category[0], end.replace(tzinfo=None), start.replace(tzinfo=None)),
    )
    tariffs = cursor.fetchall()
    if len(tariffs) != 1:
        raise BusinessGateError("exactly_one_applicable_hourly_tariff_required")
    tariff = tariffs[0]
    if (
        db_time(tariff[2]) > start or db_time(tariff[3]) < end
        or tariff[4:] != ("timeofuse", "元/kWh")
    ):
        raise BusinessGateError("full_hour_cny_timeofuse_tariff_required")
    names = (category[2], category[5], tariff[1])
    if any(not isinstance(name, str) or name.startswith("PILOT TEST") != test_only for name in names):
        raise BusinessGateError("test_parameters_must_be_explicitly_labelled_and_isolated")
    cursor.execute(
        "SELECT TIME_TO_SEC(start_time_of_day),TIME_TO_SEC(end_time_of_day),price "
        "FROM myems_system_db.tbl_tariffs_timeofuses WHERE tariff_id=%s FOR UPDATE", (tariff[0],),
    )
    local = start.astimezone(timezone(timedelta(hours=8)))
    seconds = local.hour * 3600
    matches: list[Decimal] = []
    for low, high, price in cursor.fetchall():
        if type(low) is not int or type(high) is not int or not 0 <= low < high <= 86400:
            raise BusinessGateError("invalid_timeofuse_period")
        if low < seconds + 3600 and high > seconds:
            if low > seconds or high < seconds + 3600:
                raise BusinessGateError("intrahour_tariff_change_requires_quarter_hour_billing")
            matches.append(_amount(price))
    if len(matches) != 1:
        raise BusinessGateError("exactly_one_full_hour_rate_required")
    return {
        "energy_category_id": positive_id(category[0], "Energy category"),
        "cost_center_id": positive_id(category[1], "Cost center"),
        "tariff_id": positive_id(tariff[0], "Tariff"),
        "tariff_name": tariff[1], "unit_of_price": tariff[5], "utc_offset": "+08:00",
        "price_per_kwh": str(matches[0]), "kgco2e_per_kwh": str(_amount(category[4])),
        "test_only": test_only,
    }


def settle_run(
    queue: RunQueue, run_id: int, actor: int, *, test_only: bool, apply: bool = False,
) -> dict[str, object]:
    """Verify live inputs against their audit, then atomically write both downstream tables.

    This is a trusted-operator command, not a replacement for a whole-system
    aggregation daemon. It never adopts unowned rows or rewrites historical values.
    """
    run_id, actor = positive_id(run_id, "Run ID"), positive_id(actor, "Actor ID")
    if type(test_only) is not bool:
        raise BusinessGateError("explicit_test_parameter_mode_required")
    with queue.transaction() as cursor:
        ensure_business_tables(cursor)
        tables = ("tbl_energy_categories", "tbl_cost_centers", "tbl_tariffs",
                  "tbl_tariffs_timeofuses", "tbl_cost_centers_tariffs")
        cursor.execute(
            "SELECT TABLE_NAME,ENGINE FROM information_schema.TABLES "
            "WHERE TABLE_SCHEMA='myems_system_db' AND TABLE_NAME IN ("
            + ",".join(["%s"] * len(tables)) + ")", tables,
        )
        engines = cursor.fetchall()
        if {row[0] for row in engines} != set(tables) or any(row[1] != "InnoDB" for row in engines):
            raise BusinessGateError("settlement_configuration_must_use_innodb")
        cursor.execute(
            "SELECT scope_snapshot_json,window_start_utc,window_end_utc FROM myems_system_db.tbl_jishiyun_runs "
            "WHERE id=%s AND data_source_id=%s AND status='done' FOR UPDATE", (run_id, queue.source_id),
        )
        run = cursor.fetchone()
        if run is None:
            raise BusinessGateError("completed_successful_energy_run_required")
        scope = tuple(sorted(snapshot_scope(run[0]), key=lambda meter: meter.address))
        cursor.execute(
            "SELECT details_json FROM myems_system_db.tbl_jishiyun_events "
            "WHERE data_source_id=%s AND run_id=%s AND event_type='business_hour_committed' FOR UPDATE",
            (queue.source_id, run_id),
        )
        audits = cursor.fetchall()
        if len(audits) != 1:
            raise BusinessGateError("audited_energy_hours_required_before_settlement")
        effective = effective_proofs(cursor, queue.source_id, start=db_time(run[1]), end=db_time(run[2]))
        proofs = committed_hour_proofs(effective[(run_id, "business_hour_committed")])
        entries: list[dict[str, object]] = []
        for proof in proofs:
            start_value = proof.get("start_utc")
            if not isinstance(start_value, str):
                raise BusinessGateError("invalid_committed_business_proof")
            start = datetime.fromisoformat(start_value)
            hours = tuple(load_meter_hour(cursor, queue.source_id, meter, start, start + timedelta(hours=1))
                          for meter in scope)
            if proof.get("meters") != [hour.summary() for hour in hours]:
                raise BusinessGateError("committed_input_changed_requires_explicit_historical_recalculation")
            for hour in hours:
                for register in hour.readings:
                    cursor.execute(
                        "SELECT actual_value,is_bad FROM myems_historical_db.tbl_energy_value "
                        "WHERE point_id=%s AND utc_date_time=%s FOR UPDATE",
                        (hour.point_id, register.timestamp.replace(tzinfo=None)),
                    )
                    stored = cursor.fetchall()
                    if len(stored) != 1 or stored_decimal(stored[0][0]) != register.value or stored[0][1] != 0:
                        raise BusinessGateError("committed_register_changed_or_missing")
                cursor.execute(
                    "SELECT actual_value FROM myems_energy_db.tbl_meter_hourly "
                    "WHERE meter_id=%s AND start_datetime_utc=%s FOR UPDATE",
                    (hour.meter_id, start.replace(tzinfo=None)),
                )
                energy = cursor.fetchall()
                if len(energy) != 1 or stored_decimal(energy[0][0]) != hour.consumption:
                    raise BusinessGateError("committed_energy_changed_or_missing")
                rate = settlement_rate(cursor, hour.meter_id, start, test_only=test_only)
                price = _amount(rate["price_per_kwh"])
                factor = _amount(rate["kgco2e_per_kwh"])
                entries.append({
                    "start_utc": start.isoformat(), "meter": hour.summary(), "rate": rate,
                    "billing": str(_amount((hour.consumption * price).quantize(
                        Decimal("0.000001"), rounding=ROUND_HALF_UP))),
                    "carbon": str(_amount((hour.consumption * factor).quantize(
                        Decimal("0.000001"), rounding=ROUND_HALF_UP))),
                })
        encoded = json.dumps({"version": 1, "test_only": test_only, "entries": entries}, sort_keys=True)
        cursor.execute(
            "SELECT e.run_id,e.details_json FROM myems_system_db.tbl_jishiyun_events e "
            "JOIN myems_system_db.tbl_jishiyun_runs r ON r.id=e.run_id AND r.data_source_id=e.data_source_id "
            "WHERE e.data_source_id=%s AND e.event_type='business_settlement_committed' "
            "AND r.window_start_utc < %s AND r.window_end_utc > %s FOR UPDATE",
            (queue.source_id, run[2], run[1]),
        )
        previous = False
        trusted: set[str] = set()
        for prior_run, body in cursor.fetchall():
            prior_run = positive_id(prior_run, "Settlement audit run")
            body = effective[(prior_run, "business_settlement_committed")]
            stored_proof: object = json.loads(body)
            if not isinstance(stored_proof, dict):
                raise BusinessGateError("invalid_settlement_proof")
            old_entries = cast(dict[str, object], stored_proof).get("entries")
            if not isinstance(old_entries, list):
                raise BusinessGateError("invalid_settlement_proof")
            trusted.update(json.dumps(entry, sort_keys=True) for entry in cast(list[object], old_entries))
            if prior_run == run_id:
                if previous or body != encoded:
                    raise BusinessGateError("settlement_inputs_changed_requires_historical_recalculation")
                previous = True
        pending: list[tuple[str, tuple[object, ...]]] = []
        for entry in entries:
            meter = cast(dict[str, object], entry["meter"])
            meter_id = positive_id(meter["meter_id"], "Meter ID")
            start = datetime.fromisoformat(str(entry["start_utc"])).replace(tzinfo=None)
            owned = json.dumps(entry, sort_keys=True) in trusted
            for kind, schema in (("billing", "myems_billing_db"), ("carbon", "myems_carbon_db")):
                cursor.execute(
                    "SELECT actual_value FROM " + schema + ".tbl_meter_hourly "
                    "WHERE meter_id=%s AND start_datetime_utc=%s FOR UPDATE", (meter_id, start),
                )
                rows = cursor.fetchall()
                if owned:
                    if len(rows) != 1 or stored_decimal(rows[0][0]) != _amount(entry[kind]):
                        raise BusinessGateError("committed_settlement_changed_or_missing")
                elif rows:
                    raise BusinessGateError("unowned_settlement_rows_require_manual_review")
                else:
                    pending.append((schema, (meter_id, start, _amount(entry[kind]))))
        if apply and not previous:
            for schema, parameters in pending:
                cursor.execute(
                    "INSERT INTO " + schema + ".tbl_meter_hourly "
                    "(meter_id,start_datetime_utc,actual_value) VALUES (%s,%s,%s)", parameters,
                )
            cursor.execute(
                "INSERT INTO myems_system_db.tbl_jishiyun_events "
                "(data_source_id,run_id,actor_user_id,event_type,details_json) "
                "VALUES (%s,%s,%s,'business_settlement_committed',%s)",
                (queue.source_id, run_id, actor, encoded),
            )
        return {
            "status": "already_committed" if previous else ("committed" if apply else "preview"),
            "run_id": run_id, "test_only": test_only, "entries": entries,
            "billing_insert_count": sum(schema == "myems_billing_db" for schema, _ in pending),
            "carbon_insert_count": sum(schema == "myems_carbon_db" for schema, _ in pending),
        }
