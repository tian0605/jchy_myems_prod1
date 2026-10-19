"""Explicit, bounded nine-meter local test parameters; never production billing."""

import argparse
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

from myems_jishiyun.execution import RunQueue
from myems_jishiyun.worker import database_factory


ADDRESSES = frozenset((
    "021052052039", "322212000048", "322212000049", "322212000075", "432201212906",
    "432208180919", "432208181392", "432208181393", "432208181397",
))


def configure(queue, actor, start, end, *, apply=False, new_period=False):
    if start.utcoffset() is None or end.utcoffset() is None or start >= end or actor <= 0:
        raise ValueError("Explicit increasing timezone-aware dates and an operator are required")
    start, end = start.astimezone(timezone.utc).replace(tzinfo=None), end.astimezone(timezone.utc).replace(tzinfo=None)
    label = "PILOT TEST Jishiyun source " + str(queue.source_id)
    with queue.transaction() as cursor:
        cursor.execute(
            "SELECT j.external_address,j.meter_id,m.energy_category_id,m.cost_center_id "
            "FROM myems_system_db.tbl_jishiyun_meter_mappings j "
            "JOIN myems_system_db.tbl_meters m ON m.id=j.meter_id "
            "WHERE j.data_source_id=%s AND j.is_enabled=1 AND j.configuration_status='ready' "
            "ORDER BY j.external_address FOR UPDATE", (queue.source_id,),
        )
        meters = cursor.fetchall()
        if len(meters) != 9 or {row[0] for row in meters} != ADDRESSES:
            raise ValueError("Refusing to configure anything other than the approved nine enabled pilot meters")
        cursor.execute(
            "SELECT id,kgco2e,unit_of_measure FROM myems_system_db.tbl_energy_categories WHERE name=%s FOR UPDATE",
            (label,),
        )
        categories = cursor.fetchall()
        cursor.execute("SELECT id FROM myems_system_db.tbl_cost_centers WHERE name=%s FOR UPDATE", (label,))
        centers = cursor.fetchall()
        cursor.execute(
            "SELECT id,valid_from_datetime_utc,valid_through_datetime_utc FROM myems_system_db.tbl_tariffs "
            "WHERE name=%s FOR UPDATE", (label,),
        )
        tariffs = cursor.fetchall()
        if len(categories) > 1 or len(centers) > 1 or len(tariffs) > 1:
            raise ValueError("Duplicate pilot configuration requires manual review")
        if new_period or any((categories, centers, tariffs)):
            if not all((categories, centers, tariffs)):
                raise ValueError("Partial pilot configuration requires manual review")
            category_id, factor, unit = categories[0]
            center_id = centers[0][0]
            tariff_id, valid_from, valid_through = tariffs[0]
            if factor != Decimal("1.5") or unit != "kWh" or (
                not new_period and (valid_from, valid_through) != (start, end)
            ):
                raise ValueError("Existing pilot configuration differs; do not overwrite audited parameters")
            cursor.execute(
                "SELECT start_time_of_day,end_time_of_day,price FROM myems_system_db.tbl_tariffs_timeofuses "
                "WHERE tariff_id=%s", (tariff_id,),
            )
            if cursor.fetchall() != [(timedelta(0), timedelta(days=1), Decimal("2.3"))]:
                raise ValueError("Existing pilot rates differ")
            cursor.execute(
                "SELECT tariff_id FROM myems_system_db.tbl_cost_centers_tariffs WHERE cost_center_id=%s",
                (center_id,),
            )
            linked_ids = {row[0] for row in cursor.fetchall()}
            if (
                tariff_id not in linked_ids
                or any(row[2:] != (category_id, center_id) for row in meters)
            ):
                raise ValueError("Existing pilot ownership differs")
            if new_period:
                cursor.execute(
                    "SELECT id FROM myems_system_db.tbl_meters "
                    "WHERE energy_category_id=%s OR cost_center_id=%s FOR UPDATE", (category_id, center_id),
                )
                if {row[0] for row in cursor.fetchall()} != {row[1] for row in meters}:
                    raise ValueError("Pilot parameters must belong exclusively to the approved nine meters")
                return _new_period(
                    queue, cursor, actor, category_id, center_id, label, start, end, apply=apply,
                )
            _pilot_periods(cursor, category_id, center_id, label)
            return {"status": "already_configured", "test_only": True, "meter_count": 9}
        if not apply:
            return {"status": "preview", "meters": [row[1] for row in meters], "test_only": True,
                    "price_cny_per_kwh": "2.3", "kgco2e_per_kwh": "1.5",
                    "start_utc": start.isoformat(), "end_utc": end.isoformat()}
        cursor.execute(
            "INSERT INTO myems_system_db.tbl_energy_categories "
            "(name,uuid,unit_of_measure,kgce,kgco2e) VALUES (%s,%s,'kWh',0,1.5)", (label, str(uuid4())),
        )
        cursor.execute("SELECT LAST_INSERT_ID()")
        category_id = cursor.fetchone()[0]
        cursor.execute(
            "INSERT INTO myems_system_db.tbl_cost_centers (name,uuid) VALUES (%s,%s)", (label, str(uuid4())),
        )
        cursor.execute("SELECT LAST_INSERT_ID()")
        center_id = cursor.fetchone()[0]
        cursor.execute(
            "INSERT INTO myems_system_db.tbl_tariffs "
            "(name,uuid,energy_category_id,tariff_type,unit_of_price,valid_from_datetime_utc,valid_through_datetime_utc) "
            "VALUES (%s,%s,%s,'timeofuse','元/kWh',%s,%s)", (label, str(uuid4()), category_id, start, end),
        )
        cursor.execute("SELECT LAST_INSERT_ID()")
        tariff_id = cursor.fetchone()[0]
        cursor.execute(
            "INSERT INTO myems_system_db.tbl_tariffs_timeofuses "
            "(tariff_id,start_time_of_day,end_time_of_day,peak_type,price) VALUES (%s,'00:00:00','24:00:00','midpeak',2.3)",
            (tariff_id,),
        )
        cursor.execute(
            "INSERT INTO myems_system_db.tbl_cost_centers_tariffs (cost_center_id,tariff_id) VALUES (%s,%s)",
            (center_id, tariff_id),
        )
        for _, meter_id, _, _ in meters:
            cursor.execute(
                "UPDATE myems_system_db.tbl_meters SET energy_category_id=%s,cost_center_id=%s WHERE id=%s",
                (category_id, center_id, meter_id),
            )
        details = {
            "test_only": True, "price_cny_per_kwh": "2.3", "kgco2e_per_kwh": "1.5",
            "start_utc": start.isoformat(), "end_utc": end.isoformat(),
            "category_id": category_id, "cost_center_id": center_id, "tariff_id": tariff_id,
            "previous_meter_configuration": [list(row) for row in meters],
        }
        cursor.execute(
            "INSERT INTO myems_system_db.tbl_jishiyun_events "
            "(data_source_id,actor_user_id,event_type,details_json) "
            "VALUES (%s,%s,'pilot_test_parameters_configured',%s)",
            (queue.source_id, actor, json.dumps(details, sort_keys=True)),
        )
        return {"status": "configured", "test_only": True, "meter_count": 9,
                "category_id": category_id, "cost_center_id": center_id, "tariff_id": tariff_id}


def _pilot_periods(cursor, category_id, center_id, label):
    cursor.execute(
        "SELECT t.id,t.name,t.energy_category_id,t.valid_from_datetime_utc,t.valid_through_datetime_utc,"
        "t.tariff_type,t.unit_of_price FROM myems_system_db.tbl_tariffs t "
        "JOIN myems_system_db.tbl_cost_centers_tariffs x ON x.tariff_id=t.id "
        "WHERE x.cost_center_id=%s ORDER BY t.valid_from_datetime_utc FOR UPDATE", (center_id,),
    )
    periods = cursor.fetchall()
    if not periods:
        raise ValueError("Existing pilot tariff is required before renewal")
    for index, period in enumerate(periods):
        if (
            period[2] != category_id or period[3] >= period[4]
            or period[5:] != ("timeofuse", "元/kWh")
            or not (period[1] == label or period[1].startswith(label + " "))
            or (index and periods[index - 1][4] != period[3])
        ):
            raise ValueError("Existing pilot periods must be isolated and contiguous")
        cursor.execute(
            "SELECT start_time_of_day,end_time_of_day,price FROM myems_system_db.tbl_tariffs_timeofuses "
            "WHERE tariff_id=%s", (period[0],),
        )
        if cursor.fetchall() != [(timedelta(0), timedelta(days=1), Decimal("2.3"))]:
            raise ValueError("Existing pilot rates differ")
    return periods


def _new_period(queue, cursor, actor, category_id, center_id, label, start, end, *, apply):
    if start.minute or start.second or start.microsecond or end.minute or end.second or end.microsecond:
        raise ValueError("Pilot renewal must cover whole hours")
    period_label = label + " " + start.strftime("%Y%m%dT%H%MZ") + "-" + end.strftime("%Y%m%dT%H%MZ")
    periods = _pilot_periods(cursor, category_id, center_id, label)
    overlaps = [p for p in periods if p[3] < end and p[4] > start]
    if overlaps:
        if len(overlaps) == 1 and overlaps[0][1] == period_label and overlaps[0][3:5] == (start, end):
            return {"status": "already_configured", "test_only": True, "tariff_id": overlaps[0][0]}
        raise ValueError("Refusing an overlapping pilot period")
    if periods[-1][4] != start:
        raise ValueError("New period must begin at the current pilot expiry")
    details = {
        "test_only": True, "meter_count": 9, "price_cny_per_kwh": "2.3", "kgco2e_per_kwh": "1.5",
        "start_utc": start.isoformat(), "end_utc": end.isoformat(),
        "category_id": category_id, "cost_center_id": center_id,
        "previous_tariff_ids": [p[0] for p in periods],
    }
    if not apply:
        return {"status": "preview", **details}
    cursor.execute("SELECT id FROM myems_system_db.tbl_tariffs WHERE name=%s FOR UPDATE", (period_label,))
    if cursor.fetchall():
        raise ValueError("Unlinked renewal tariff requires manual review")
    cursor.execute(
        "INSERT INTO myems_system_db.tbl_tariffs "
        "(name,uuid,energy_category_id,tariff_type,unit_of_price,valid_from_datetime_utc,valid_through_datetime_utc) "
        "VALUES (%s,%s,%s,'timeofuse','元/kWh',%s,%s)",
        (period_label, str(uuid4()), category_id, start, end),
    )
    cursor.execute("SELECT LAST_INSERT_ID()")
    tariff_id = cursor.fetchone()[0]
    cursor.execute(
        "INSERT INTO myems_system_db.tbl_tariffs_timeofuses "
        "(tariff_id,start_time_of_day,end_time_of_day,peak_type,price) VALUES (%s,'00:00:00','24:00:00','midpeak',2.3)",
        (tariff_id,),
    )
    cursor.execute(
        "INSERT INTO myems_system_db.tbl_cost_centers_tariffs (cost_center_id,tariff_id) VALUES (%s,%s)",
        (center_id, tariff_id),
    )
    details["tariff_id"] = tariff_id
    cursor.execute(
        "INSERT INTO myems_system_db.tbl_jishiyun_events "
        "(data_source_id,actor_user_id,event_type,details_json) "
        "VALUES (%s,%s,'pilot_test_period_configured',%s)",
        (queue.source_id, actor, json.dumps(details, sort_keys=True)),
    )
    return {"status": "configured", **details}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-id", required=True, type=int)
    parser.add_argument("--actor", required=True, type=int)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--new-period", action="store_true", help="Append a separately approved contiguous test period")
    args = parser.parse_args()
    print(json.dumps(configure(
        RunQueue(database_factory(), args.source_id), args.actor,
        datetime.fromisoformat(args.start), datetime.fromisoformat(args.end),
        apply=args.apply, new_period=args.new_period,
    )))
