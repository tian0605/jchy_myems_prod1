"""Read-only real pilot reconciliation; credentials and tokens are never printed."""

import argparse
import base64
import io
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zipfile import ZipFile

from myems_jishiyun.worker import database_factory
from configure_pilot import ADDRESSES


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", type=int, default=17)
    parser.add_argument("--start", default="2026-10-16T10:00:00+08:00")
    parser.add_argument("--end", default="2026-10-16T18:00:00+08:00")
    args = parser.parse_args()
    local = timezone(timedelta(hours=8))
    start_local, end_local = datetime.fromisoformat(args.start), datetime.fromisoformat(args.end)
    if any(
        value.utcoffset() != timedelta(hours=8) or value.minute or value.second or value.microsecond
        for value in (start_local, end_local)
    ):
        parser.error("Use whole-hour timezone-aware UTC+8 boundaries")
    hours = (end_local - start_local).total_seconds() / 3600
    if args.run_id <= 0 or not 1 <= hours <= 24 or not hours.is_integer():
        parser.error("A positive run ID and a 1-to-24-hour window are required")
    hours = int(hours)
    start_utc = start_local.astimezone(timezone.utc).replace(tzinfo=None)
    end_utc = end_local.astimezone(timezone.utc).replace(tzinfo=None)
    connection = database_factory()()
    cursor = connection.cursor()
    try:
        cursor.execute(
            "SELECT external_address,meter_id FROM myems_system_db.tbl_jishiyun_meter_mappings "
            "WHERE data_source_id=4 AND is_enabled=1 ORDER BY external_address",
        )
        meters = cursor.fetchall()
        if len(meters) != 9 or {row[0] for row in meters} != ADDRESSES:
            raise AssertionError("Expected exactly nine active pilot meters")
        cursor.execute(
            "SELECT id,status,gate_code FROM myems_system_db.tbl_jishiyun_business_jobs WHERE run_id=%s",
            (args.run_id,),
        )
        job = cursor.fetchone()
        if job is None or job[1:] != ("done", None):
            raise AssertionError("Selected pilot business job is not done")
        cursor.execute(
            "SELECT status,window_start_utc,window_end_utc,expected_count,valid_count,missing_count,"
            "invalid_count,unexpected_count FROM myems_system_db.tbl_jishiyun_runs "
            "WHERE id=%s AND data_source_id=4", (args.run_id,),
        )
        if cursor.fetchone() != ("done", start_utc, end_utc, 9 * hours * 4, 9 * hours * 4, 0, 0, 0):
            raise AssertionError("Selected run is not complete for the exact requested acceptance window")
        credentials = json.loads(Path("/acceptance/state/login.json").read_text("utf-8-sig"))
        request = Request(
            "http://api:8000/users/login", method="PUT",
            data=json.dumps({"data": {"name": credentials["username"], "password": credentials["password"]}}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urlopen(request, timeout=20) as response:
            session = json.loads(response.read())
        headers = {"User-UUID": session["uuid"], "Token": session["token"]}
        totals = [Decimal(0), Decimal(0), Decimal(0)]
        rows = []
        export_checks = 0
        for address, meter_id in meters:
            expected = [Decimal(0), Decimal(0), Decimal(0)]
            for index in range(hours):
                start = start_utc + timedelta(hours=index)
                end = start + timedelta(hours=1)
                cursor.execute(
                    "SELECT reading_datetime_utc,forward_active_register,quality,revision "
                    "FROM myems_historical_db.tbl_jishiyun_energy_readings "
                    "WHERE data_source_id=4 AND external_address=%s AND date_type='mi15' AND value_type='SJZ' "
                    "AND reading_datetime_utc >= %s AND reading_datetime_utc <= %s ORDER BY reading_datetime_utc",
                    (address, start, end),
                )
                raw = cursor.fetchall()
                if len(raw) != 5 or any(
                    record[0] != start + timedelta(minutes=15 * i) or record[2] != "valid"
                    for i, record in enumerate(raw)
                ):
                    raise AssertionError("Missing or invalid five-point hour")
                consumption = raw[-1][1] - raw[0][1]
                if any(right[1] < left[1] for left, right in zip(raw, raw[1:])):
                    raise AssertionError("Register decrease within an accepted hour")
                values = (consumption, consumption * Decimal("2.3"), consumption * Decimal("1.5"))
                for kind, value in enumerate(values):
                    schema = ("myems_energy_db", "myems_billing_db", "myems_carbon_db")[kind]
                    cursor.execute(
                        "SELECT actual_value FROM " + schema + ".tbl_meter_hourly "
                        "WHERE meter_id=%s AND start_datetime_utc=%s", (meter_id, start),
                    )
                    stored = cursor.fetchall()
                    if stored != [(value,)]:
                        raise AssertionError("Missing, duplicated or inconsistent business hour")
                    expected[kind] += value
            for kind, report in enumerate(("meterenergy", "metercost", "metercarbon")):
                params = urlencode({
                    "meterid": meter_id, "periodtype": "hourly", "language": "zh_CN", "quickmode": "true",
                    "reportingperiodstartdatetime": start_local.astimezone(local).replace(tzinfo=None).isoformat(),
                    "reportingperiodenddatetime": end_local.astimezone(local).replace(tzinfo=None).isoformat(),
                })
                request = Request("http://api:8000/reports/" + report + "?" + params, headers=headers)
                with urlopen(request, timeout=30) as response:
                    result = json.loads(response.read(), parse_float=Decimal)
                actual = Decimal(str(result["reporting_period"]["total_in_category"]))
                if actual != expected[kind] or not result["meter"]["energy_category_name"].startswith("PILOT TEST"):
                    raise AssertionError("Authenticated report differs from independent SQL or loses test label")
                totals[kind] += expected[kind]
                if meter_id == meters[0][1]:
                    export_params = params.replace("quickmode=true", "quickmode=false")
                    with urlopen(Request(
                        "http://api:8000/reports/" + report + "?" + export_params, headers=headers,
                    ), timeout=60) as response:
                        exported = json.loads(response.read(), parse_float=Decimal)
                    if Decimal(str(exported["reporting_period"]["total_in_category"])) != expected[kind]:
                        raise AssertionError("Export response total differs")
                    workbook = base64.b64decode(exported["excel_bytes_base64"], validate=True)
                    with ZipFile(io.BytesIO(workbook)) as archive:
                        if "xl/workbook.xml" not in archive.namelist() or not any(
                            b"PILOT TEST" in archive.read(name) for name in archive.namelist()
                            if name.startswith("xl/") and name.endswith(".xml")
                        ):
                            raise AssertionError("Export is not a labelled pilot XLSX")
                    export_checks += 1
            rows.append({"address": address, "meter_id": meter_id, "kwh": str(expected[0]),
                         "test_cny": str(expected[1]), "test_kgco2e": str(expected[2])})
        print(json.dumps({
            "status": "passed", "test_only": True, "run_id": args.run_id,
            "window_beijing": start_local.isoformat() + ".." + end_local.isoformat(),
            "hours_per_meter": hours, "meter_count": 9, "verified_hour_keys": 9 * hours,
            "authenticated_report_checks": 27, "rows": rows,
            "labelled_xlsx_export_checks": export_checks,
            "total_kwh": str(totals[0]), "test_total_cny": str(totals[1]),
            "test_total_kgco2e": str(totals[2]),
        }))
    finally:
        cursor.close()
        connection.close()


if __name__ == "__main__":
    main()
