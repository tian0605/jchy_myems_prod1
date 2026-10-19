"""Independent company-2/device SQL, authenticated report and XLSX acceptance."""

import base64
import io
import json
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zipfile import ZipFile

from myems_jishiyun.worker import database_factory


def main():
    connection = database_factory()()
    cursor = connection.cursor()
    try:
        cursor.execute(
            "SELECT details_json FROM myems_system_db.tbl_jishiyun_events "
            "WHERE data_source_id=4 AND event_type='pilot_dimensions_configured'",
        )
        rows = cursor.fetchall()
        if len(rows) != 1:
            raise AssertionError("Expected one approved dimension configuration")
        config = json.loads(rows[0][0])
        credentials = json.loads(Path("/acceptance/state/login.json").read_text("utf-8-sig"))
        request = Request("http://api:8000/users/login", method="PUT",
                          data=json.dumps({"data": {"name": credentials["username"],
                                                     "password": credentials["password"]}}).encode(),
                          headers={"Content-Type": "application/json"})
        with urlopen(request, timeout=20) as response:
            session = json.loads(response.read())
        headers = {"User-UUID": session["uuid"], "Token": session["token"]}
        totals = {}
        sql_checks = report_checks = xlsx_checks = 0
        for kind in ("space", "equipment"):
            for metric, schema, report in (
                ("energy", "myems_energy_db", kind + "energycategory"),
                ("cost", "myems_billing_db", kind + "cost"),
                ("carbon", "myems_carbon_db", kind + "carbon"),
            ):
                total = Decimal(0)
                for index in range(24):
                    start = datetime(2026, 10, 16, 2) + timedelta(hours=index)
                    expected = Decimal(0)
                    for meter in config["meter_ids"]:
                        cursor.execute("SELECT actual_value FROM " + schema
                                       + ".tbl_meter_hourly WHERE meter_id=%s AND start_datetime_utc=%s",
                                       (meter, start))
                        values = cursor.fetchall()
                        if len(values) != 1:
                            raise AssertionError("Member hour missing or duplicated")
                        expected += values[0][0]
                    cursor.execute(
                        "SELECT actual_value FROM " + schema + ".tbl_" + kind
                        + "_input_category_hourly WHERE " + kind
                        + "_id=%s AND energy_category_id=%s AND start_datetime_utc=%s",
                        (config[kind + "_id"], config["energy_category_id"], start),
                    )
                    if cursor.fetchall() != [(expected,)]:
                        raise AssertionError("Stored dimension hour differs from exact member sum")
                    sql_checks += 1
                    total += expected
                params = {
                    kind + "id": config[kind + "_id"], "productid": config["product_id"],
                    "periodtype": "hourly", "language": "zh_CN", "quickmode": "true",
                    "reportingperiodstartdatetime": "2026-10-16T10:00:00",
                    "reportingperiodenddatetime": "2026-10-17T10:00:00",
                }
                for quick in ("true", "false"):
                    params["quickmode"] = quick
                    with urlopen(Request("http://api:8000/reports/" + report + "?" + urlencode(params),
                                         headers=headers), timeout=90) as response:
                        result = json.loads(response.read(), parse_float=Decimal)
                    period = result["reporting_period"]
                    actual = sum((Decimal(str(v)) for v in period["subtotals"]), Decimal(0)) \
                        if metric == "energy" else Decimal(str(period["total"]))
                    if actual != total:
                        raise AssertionError("Authenticated " + report + " differs from SQL")
                    if quick == "false":
                        workbook = base64.b64decode(result["excel_bytes_base64"], validate=True)
                        with ZipFile(io.BytesIO(workbook)) as archive:
                            if "xl/workbook.xml" not in archive.namelist():
                                raise AssertionError("Invalid dimension XLSX")
                        xlsx_checks += 1
                    report_checks += 1
                totals[kind + "_" + metric] = str(total)
        print(json.dumps({"status": "passed", "space_id": config["space_id"],
                          "equipment_id": config["equipment_id"], "product_id": config["product_id"],
                          "hourly_sql_checks": sql_checks, "authenticated_reports": report_checks,
                          "xlsx_checks": xlsx_checks, "test_only": True, "totals": totals}))
    finally:
        cursor.close()
        connection.close()


if __name__ == "__main__":
    main()
