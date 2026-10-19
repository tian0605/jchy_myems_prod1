"""Real MySQL checks and synthetic-provider acceptance, inside the worker image."""

import argparse
import json
import os
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4
from urllib.request import Request, urlopen
from urllib.error import HTTPError

from myems_jishiyun.execution import RunQueue
from myems_jishiyun.batches import register_batch
from myems_jishiyun.initialization import ArchiveInitializer, MeterConfiguration
from myems_jishiyun.persistence import PersistenceError
from myems_jishiyun.worker import database_factory


STATE = Path("/acceptance/state")
START = datetime(2026, 10, 1, tzinfo=timezone.utc)
END = START + timedelta(hours=1)


def scalar(query, parameters=()):
    connection = database_factory()()
    cursor = connection.cursor()
    try:
        cursor.execute(query, parameters)
        return cursor.fetchone()[0]
    finally:
        cursor.close()
        connection.close()


def wait_run(run_id, expected, timeout=180):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = scalar("SELECT status FROM tbl_jishiyun_runs WHERE id=%s", (run_id,))
        if status == expected:
            return
        if status in ("error", "cancelled", "done", "partial") and status != expected:
            raise AssertionError(f"Run {run_id} unexpectedly ended with {status}")
        time.sleep(2)
    raise AssertionError(f"Run {run_id} did not reach {expected}")


def check_schema():
    connection = database_factory()()
    cursor = connection.cursor()
    try:
        cursor.execute(
            "SELECT TABLE_SCHEMA,TABLE_NAME,ENGINE FROM information_schema.TABLES "
            "WHERE TABLE_SCHEMA IN ('myems_system_db','myems_historical_db') "
            "AND TABLE_NAME LIKE 'tbl_jishiyun_%'",
        )
        rows = cursor.fetchall()
        assert len(rows) == 11 and all(row[2] == "InnoDB" for row in rows), rows
        # Real separate connections, not simulated GET_LOCK functions.
        other = database_factory()()
        second = other.cursor()
        name = "jishiyun_local_acceptance_" + uuid4().hex
        try:
            cursor.execute("SELECT GET_LOCK(%s,0)", (name,))
            assert cursor.fetchone()[0] == 1
            second.execute("SELECT GET_LOCK(%s,0)", (name,))
            assert second.fetchone()[0] == 0
            cursor.execute("SELECT RELEASE_LOCK(%s)", (name,))
            assert cursor.fetchone()[0] == 1
            second.execute("SELECT GET_LOCK(%s,0)", (name,))
            assert second.fetchone()[0] == 1
            second.execute("SELECT RELEASE_LOCK(%s)", (name,))
            assert second.fetchone()[0] == 1
        finally:
            second.close()
            other.close()
        connection.rollback()
        cursor.execute(
            "INSERT INTO tbl_jishiyun_events(data_source_id,event_type,details_json) "
            "VALUES(1,'local_rollback_probe','{}')",
        )
        cursor.execute(
            "INSERT INTO myems_historical_db.tbl_jishiyun_energy_readings "
            "(data_source_id,external_address,external_id,reading_datetime_utc,date_type,value_type,"
            "forward_active_register,quality,raw_record_json,first_run_id,last_run_id) "
            "VALUES(1,'rollback',999,'2026-01-01','mi15','SJZ',1,'valid','{}',1,1)",
        )
        connection.rollback()
        assert scalar("SELECT COUNT(*) FROM tbl_jishiyun_events WHERE event_type='local_rollback_probe'") == 0
        assert scalar("SELECT COUNT(*) FROM myems_historical_db.tbl_jishiyun_energy_readings WHERE external_address='rollback'") == 0
    finally:
        cursor.close()
        connection.close()
    print("PASS: 11 real InnoDB extension tables, two-session advisory lock exclusion, cross-schema rollback", flush=True)


def initialize(queue):
    service = ArchiveInitializer(queue)
    run_id = queue.enqueue_archive(1, str(uuid4()), 1)
    wait_run(run_id, "preview")
    addresses = tuple(f"{index:04}" for index in range(1, 71))
    for index, address in enumerate(addresses, 1):
        service.approve(run_id, address, MeterConfiguration(
            f"LOCAL Synthetic meter {index:02}", 1, 1, True,
            Decimal("0"), Decimal("999999"), "kWh", Decimal("999999999"),
        ), 1)
    service.submit_approved(run_id, addresses, 1)
    wait_run(run_id, "done")
    assert scalar("SELECT COUNT(*) FROM tbl_jishiyun_meter_mappings") == 70
    assert scalar("SELECT COUNT(*) FROM tbl_points WHERE object_type='JISHIYUN_REGISTER'") == 70
    assert scalar("SELECT COUNT(*) FROM tbl_jishiyun_meter_mappings WHERE is_enabled=1") == 0
    print("PASS: real HTTPS archive fetch -> 70 approvals -> worker initialization; mappings disabled", flush=True)
    # Acceptance-only raw collection scope. Never switches the point type to ENERGY_VALUE.
    connection = database_factory()()
    cursor = connection.cursor()
    try:
        cursor.execute(
            "UPDATE tbl_jishiyun_meter_mappings SET configuration_status='ready',is_enabled=1,collection_start_utc=%s",
            (START.replace(tzinfo=None),),
        )
        cursor.execute(
            "INSERT INTO tbl_jishiyun_events(data_source_id,actor_user_id,event_type,details_json) "
            "VALUES(1,1,'local_synthetic_scope_enabled','{\"acceptance_only\":true}')",
        )
        connection.commit()
    finally:
        cursor.close()
        connection.close()
    return run_id


def collect(queue, expected):
    request_key = str(uuid4())
    run_id = queue.enqueue(2, request_key, START, END, 1)
    assert queue.enqueue(2, request_key, START, END, 1) == run_id
    wait_run(run_id, expected)
    return run_id


def workflow(resume_after_missing=False):
    queue = RunQueue(database_factory(), 1)
    check_schema()
    if resume_after_missing:
        assert scalar("SELECT COUNT(*) FROM tbl_jishiyun_runs") == 2, "Resume requires exactly the archive and missing-data runs"
        archive_id = scalar("SELECT id FROM tbl_jishiyun_runs WHERE task_id=1 AND status='done'")
        missing_id = scalar("SELECT id FROM tbl_jishiyun_runs WHERE task_id=2 AND status='partial'")
        assert scalar("SELECT COUNT(*) FROM tbl_jishiyun_meter_mappings") == 70
        assert scalar("SELECT COUNT(*) FROM myems_historical_db.tbl_jishiyun_energy_readings") == 264
    else:
        assert scalar("SELECT COUNT(*) FROM tbl_jishiyun_runs") == 0, "Workflow needs an unused synthetic fixture"
        archive_id = initialize(queue)
        missing_id = collect(queue, "partial")
    assert scalar("SELECT expected_count FROM tbl_jishiyun_runs WHERE id=%s", (missing_id,)) == 280
    assert scalar("SELECT valid_count FROM tbl_jishiyun_runs WHERE id=%s", (missing_id,)) == 264
    assert scalar("SELECT missing_count FROM tbl_jishiyun_runs WHERE id=%s", (missing_id,)) == 16
    assert scalar("SELECT COUNT(*) FROM tbl_jishiyun_alerts WHERE status='active'") == 16
    (STATE / "scenario.json").write_text('{"missing":false}', encoding="utf-8")
    recovery_id = collect(queue, "done")
    assert scalar("SELECT COUNT(*) FROM myems_historical_db.tbl_jishiyun_energy_readings") == 280
    assert scalar("SELECT COUNT(*) FROM tbl_jishiyun_alerts WHERE status='active'") == 0
    assert scalar("SELECT COUNT(*) FROM tbl_jishiyun_alerts WHERE status='recovered'") == 16
    duplicate_id = collect(queue, "done")
    assert scalar("SELECT duplicate_count FROM tbl_jishiyun_runs WHERE id=%s", (duplicate_id,)) == 280
    assert scalar("SELECT COUNT(*) FROM myems_historical_db.tbl_jishiyun_energy_readings") == 280
    assert scalar("SELECT COUNT(*) FROM myems_historical_db.tbl_energy_value") == 0
    assert scalar("SELECT COUNT(*) FROM tbl_points WHERE object_type='ENERGY_VALUE'") == 0
    # Leave a visible gap set for manual workbench testing, without deleting valid raw history.
    (STATE / "scenario.json").write_text('{"missing":true}', encoding="utf-8")
    final_id = collect(queue, "partial")
    assert scalar("SELECT COUNT(*) FROM tbl_jishiyun_alerts WHERE status='active'") == 16
    assert scalar("SELECT COUNT(*) FROM myems_historical_db.tbl_jishiyun_energy_readings") == 280
    # Stop synthetic eligibility after acceptance. The live worker remains idle/healthy.
    connection = database_factory()()
    cursor = connection.cursor()
    try:
        cursor.execute("UPDATE tbl_jishiyun_meter_mappings SET is_enabled=0")
        cursor.execute(
            "INSERT INTO tbl_jishiyun_events(data_source_id,actor_user_id,event_type,details_json) "
            "VALUES(1,1,'local_synthetic_scope_disabled','{\"acceptance_only\":true}')",
        )
        connection.commit()
    finally:
        cursor.close()
        connection.close()
    result = {"status": "passed", "meters": 70, "expected_hour_slots": 280, "missing_slots": 16,
              "raw_rows": 280, "duplicate_count": 280, "archive_run_id": archive_id,
              "missing_run_id": missing_id, "recovery_run_id": recovery_id,
              "duplicate_run_id": duplicate_id, "visible_partial_run_id": final_id,
              "business_writes": 0, "mapping_collection_enabled": False}
    (STATE / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result), flush=True)


def cancellation():
    """Run with the polling worker stopped so this test owns the queued claim."""
    queue = RunQueue(database_factory(), 1)
    run_id = queue.enqueue_archive(1, str(uuid4()), 1)
    claimed = queue.claim()
    assert claimed is not None and claimed.id == run_id, "Dedicated cancellation claim not obtained"
    session = json.loads((STATE / "session.json").read_text("utf-8-sig"))
    url = f"http://api:8000/datasources/1/acquisition/runs/{run_id}/control"
    for _ in range(2):
        request = Request(url, data=b'{"action":"cancel"}', method="POST", headers={
            "Content-Type": "application/json", "User-UUID": session["uuid"], "Token": session["token"],
        })
        with urlopen(request, timeout=15) as response:
            assert response.status == 200
    try:
        queue.renew(claimed)
    except PersistenceError:
        pass
    else:
        raise AssertionError("Cancelled run accepted its stale lease token")
    assert scalar("SELECT status FROM tbl_jishiyun_runs WHERE id=%s", (run_id,)) == "cancelled"
    assert scalar("SELECT COUNT(*) FROM tbl_jishiyun_events WHERE run_id=%s AND event_type='admin_run_cancelled'", (run_id,)) == 1
    assert scalar("SELECT COUNT(*) FROM tbl_jishiyun_meter_mappings") == 70
    assert scalar("SELECT COUNT(*) FROM myems_historical_db.tbl_jishiyun_energy_readings") == 280
    result_path = STATE / "result.json"
    result = json.loads(result_path.read_text())
    result["cancelled_run_id"] = run_id
    result["cancelled_token_rejected"] = True
    result_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print("PASS: authenticated cancellation, repeated control has one audit event, stale token rejected, committed data retained")


def authenticated_api(prefix):
    assert scalar("SELECT name FROM tbl_data_sources WHERE id=1") == "LOCAL SYNTHETIC Jishiyun"
    login = json.loads((STATE / "login.json").read_text("utf-8-sig"))
    request = Request("http://api:8000/users/login", method="PUT",
                      data=json.dumps({"data": {"name": login["username"], "password": login["password"]}}).encode(),
                      headers={"Content-Type": "application/json"})
    with urlopen(request, timeout=15) as response:
        session = json.loads(response.read())
    (STATE / "session.json").write_text(json.dumps(session), encoding="utf-8")

    def api(path, body=None, *, expected=200, authenticated=True, source_id=1):
        headers = {"Content-Type": "application/json"}
        if authenticated:
            headers.update({"User-UUID": session["uuid"], "Token": session["token"]})
        request = Request(f"http://api:8000/datasources/{source_id}/acquisition/" + prefix + path,
                          data=json.dumps(body).encode() if body is not None else None, headers=headers)
        try:
            response = urlopen(request, timeout=20)
        except HTTPError as error:
            assert error.code == expected, f"Unexpected HTTP error {error.code}, expected {expected}"
            return None
        with response:
            assert response.status == expected
            return json.loads(response.read())
    return api


def http_initialization():
    """Exercise only the isolated synthetic source; reuse its existing 70 mappings."""
    assert scalar("SELECT COUNT(*) FROM tbl_jishiyun_meter_mappings") == 70
    assert scalar("SELECT COUNT(*) FROM tbl_jishiyun_meter_mappings WHERE is_enabled=1") == 0
    api = authenticated_api("initialization")

    body = {"task_id": 1, "request_key": str(uuid4())}
    run_id = api("", body, expected=202)["run_id"]
    assert api("", body, expected=202)["run_id"] == run_id
    wait_run(run_id, "preview")
    api(f"/{run_id}", authenticated=False, expected=400)
    first = api(f"/{run_id}?limit=50")
    second = api(f"/{run_id}?limit=50&before_id={first['next_before_id']}")
    assert len(first["items"]) == 50 and len(second["items"]) == 20 and not second["has_more"]
    rows = first["items"] + second["items"]
    assert all(row["existing_meter_id"] and "archive_json" not in row for row in rows)
    items = [{
        "address": row["external_address"], "configuration": {
            "name": f"LOCAL Synthetic meter {int(row['external_address']):02}", "energy_category_id": 1,
            "cost_center_id": 1, "is_counted": True, "hourly_low_limit": "0",
            "hourly_high_limit": "999999", "units": "kWh", "point_high_limit": "999999999",
        },
    } for row in rows]
    api(f"/{run_id}/approve", {"items": [items[0], dict(items[1], address="missing")]}, expected=409)
    assert scalar("SELECT COUNT(*) FROM tbl_jishiyun_run_items WHERE run_id=%s AND status='pending'", (run_id,)) == 0
    for _ in range(2):
        assert api(f"/{run_id}/approve", {"items": items})["approved_count"] == 70
    assert scalar(
        "SELECT COUNT(*) FROM tbl_jishiyun_events WHERE run_id=%s AND event_type='archive_item_approved' AND actor_user_id=1",
        (run_id,),
    ) == 70
    for _ in range(2):
        api(f"/{run_id}/submit", {"addresses": [item["address"] for item in items]}, expected=202)
    wait_run(run_id, "done")
    assert scalar("SELECT duplicate_count FROM tbl_jishiyun_runs WHERE id=%s", (run_id,)) == 70
    assert scalar("SELECT COUNT(*) FROM tbl_meters") == 70
    assert scalar("SELECT COUNT(*) FROM tbl_jishiyun_meter_mappings WHERE is_enabled=1") == 0
    assert scalar("SELECT COUNT(*) FROM myems_historical_db.tbl_jishiyun_energy_readings") == 280
    result_path = STATE / "result.json"
    result = json.loads(result_path.read_text("utf-8-sig"))
    result["http_initialization"] = {"run_id": run_id, "reused": 70, "page_sizes": [50, 20],
                                     "batch_approval_rollback": True}
    result_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(f"PASS: authenticated HTTP fetch -> 50+20 preview -> atomic/idempotent 70 approvals -> "
          f"worker reused 70 mappings; run {run_id}; mappings disabled, raw rows unchanged")


def http_tasks():
    api = authenticated_api("tasks")
    configuration = {
        "name": "LOCAL HTTP task editor probe", "task_type": "energy_backfill", "schedule_minute": 40,
        "delay_minutes": 40, "lookback_hours": 3, "provider_utc_offset_minutes": 480, "encoding": "json",
    }
    submission = {"request_key": str(uuid4()), "configuration": configuration}
    task_id = api("", submission, expected=201)["id"]
    assert api("", submission, expected=201)["id"] == task_id
    api("", dict(submission, configuration=dict(configuration, name="Changed request")), expected=409)
    snapshot = api(f"/{task_id}")
    assert snapshot["configuration"] == configuration and not snapshot["is_enabled"]
    changed = dict(configuration, name="LOCAL HTTP edited task (paused)", lookback_hours=2)
    assert api(f"/{task_id}", {"configuration": changed, "expected_configuration": configuration})["changed"]
    assert not api(f"/{task_id}", {"configuration": changed, "expected_configuration": configuration})["changed"]
    api(f"/{task_id}", {"configuration": dict(changed, name="Stale edit"),
                       "expected_configuration": configuration}, expected=409)
    api(f"/{task_id}", {"configuration": dict(changed, task_type="meter_initialize"),
                       "expected_configuration": changed}, expected=409)
    api(f"/{task_id}", authenticated=False, expected=400)
    assert scalar("SELECT is_enabled FROM tbl_jishiyun_tasks WHERE id=%s", (task_id,)) == 0
    assert scalar(
        "SELECT COUNT(*) FROM tbl_jishiyun_events WHERE event_type IN ('admin_task_created','admin_task_updated') "
        "AND actor_user_id=1 AND JSON_UNQUOTE(JSON_EXTRACT(details_json,'$.task_id'))=%s", (str(task_id),),
    ) == 2
    assert scalar("SELECT COUNT(*) FROM tbl_jishiyun_meter_mappings WHERE is_enabled=1") == 0
    result_path = STATE / "result.json"
    result = json.loads(result_path.read_text("utf-8-sig"))
    result["http_task_configuration"] = {"task_id": task_id, "paused": True, "audits": 2,
                                          "idempotency_and_stale_conflicts": True}
    result_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(f"PASS: real MySQL task {task_id} create/replay/edit/replay; stale/type changes rejected; "
          "two actor-attributed events; task remains paused")


def http_mappings():
    """Exercise real MySQL range writes in a disposable, synthetic-only source."""
    api = authenticated_api("mappings")
    first = api("?limit=50")
    second = api(f"?limit=50&before_id={first['next_before_id']}")
    assert len(first["items"]) == 50 and len(second["items"]) == 20 and not second["has_more"]
    existing_id = first["items"][0]["id"]
    previous = api(f"/{existing_id}")["configuration"]
    proposed = datetime.fromisoformat(previous["collection_start_utc"]) - timedelta(minutes=15) \
        if previous["collection_start_utc"] else datetime(2026, 10, 1)
    body = {"expected_configuration": previous, "collection_start_utc": proposed.isoformat() + "Z",
            "collection_end_utc": None}
    api(f"/{existing_id}", body, expected=409)  # Existing source history is immutable.
    api(f"/{existing_id}", authenticated=False, expected=400)
    baseline = scalar("SELECT COUNT(*) FROM tbl_jishiyun_meter_mappings")
    marker = "LOCAL disposable mapping probe " + uuid4().hex
    source_id = meter_id = point_id = mapping_id = None
    connection = database_factory()()
    cursor = connection.cursor()
    try:
        cursor.execute(
            "INSERT INTO tbl_data_sources(name,uuid,gateway_id,protocol,connection) "
            "SELECT %s,%s,gateway_id,protocol,'{}' FROM tbl_data_sources WHERE id=1",
            (marker, str(uuid4())),
        )
        source_id = cursor.lastrowid
        cursor.execute(
            "INSERT INTO tbl_meters(name,uuid,energy_category_id,is_counted,hourly_low_limit,"
            "hourly_high_limit,cost_center_id) VALUES(%s,%s,1,0,0,999999,1)", (marker, str(uuid4())),
        )
        meter_id = cursor.lastrowid
        cursor.execute(
            "INSERT INTO tbl_points(name,data_source_id,object_type,units,high_limit,low_limit,ratio,"
            "offset_constant,is_trend,is_virtual,address) VALUES(%s,%s,'JISHIYUN_REGISTER','kWh',999999,0,1,0,1,0,%s)",
            (marker, source_id, json.dumps({"provider": "jishiyun", "address": "0001", "field": "ZYGDNSZ"})),
        )
        point_id = cursor.lastrowid
        cursor.execute("INSERT INTO tbl_meters_points(meter_id,point_id) VALUES(%s,%s)", (meter_id, point_id))
        cursor.execute(
            "INSERT INTO tbl_jishiyun_meter_mappings(data_source_id,external_id,external_address,meter_id,"
            "point_id,initialization_run_id,archive_json,last_archive_sync_datetime_utc) "
            "VALUES(%s,1,'0001',%s,%s,1,%s,UTC_TIMESTAMP())",
            (source_id, meter_id, point_id, json.dumps({"ID": 1, "Address": "0001"})),
        )
        mapping_id = cursor.lastrowid
        connection.commit()
        api(f"/{mapping_id}", expected=404)
        snapshot = api(f"/{mapping_id}", source_id=source_id)
        assert snapshot["ownership_verified"] and not snapshot["activation_available"]
        body = {"expected_configuration": snapshot["configuration"],
                "collection_start_utc": "2026-10-01T08:00:00+08:00", "collection_end_utc": None}
        assert api(f"/{mapping_id}", body, source_id=source_id)["changed"]
        assert not api(f"/{mapping_id}", body, source_id=source_id)["changed"]
        api(f"/{mapping_id}", dict(body, collection_start_utc="2026-10-02T00:00:00Z"),
            expected=409, source_id=source_id)
        api(f"/{mapping_id}", dict(body, is_enabled=True), expected=400, source_id=source_id)
        current = api(f"/{mapping_id}", source_id=source_id)["configuration"]
        assert current["collection_start_utc"] == "2026-10-01T00:00:00"
        assert not current["is_enabled"] and current["configuration_status"] == "pending"
        assert scalar(
            "SELECT COUNT(*) FROM tbl_jishiyun_events WHERE mapping_id=%s "
            "AND actor_user_id=1 AND event_type='admin_mapping_range_updated'", (mapping_id,),
        ) == 1
        cursor.execute("UPDATE tbl_points SET ratio=120 WHERE id=%s", (point_id,))
        connection.commit()
        api(f"/{mapping_id}", expected=409, source_id=source_id)
    finally:
        connection.rollback()
        # Only IDs created above are disposable; never touch the retained 70-meter fixture.
        if source_id is not None:
            cursor.execute("SELECT name FROM tbl_data_sources WHERE id=%s", (source_id,))
            row = cursor.fetchone()
            if row is not None:
                assert row[0] == marker
                cursor.execute("DELETE FROM tbl_jishiyun_events WHERE data_source_id=%s", (source_id,))
                cursor.execute("DELETE FROM tbl_jishiyun_meter_mappings WHERE id=%s AND data_source_id=%s",
                               (mapping_id, source_id))
                cursor.execute("DELETE FROM tbl_meters_points WHERE meter_id=%s AND point_id=%s", (meter_id, point_id))
                cursor.execute("DELETE FROM tbl_points WHERE id=%s AND data_source_id=%s", (point_id, source_id))
                cursor.execute("DELETE FROM tbl_meters WHERE id=%s AND name=%s", (meter_id, marker))
                cursor.execute("DELETE FROM tbl_data_sources WHERE id=%s AND name=%s", (source_id, marker))
                connection.commit()
        cursor.close()
        connection.close()
    assert scalar("SELECT COUNT(*) FROM tbl_jishiyun_meter_mappings") == baseline == 70
    assert scalar("SELECT COUNT(*) FROM tbl_jishiyun_meter_mappings WHERE is_enabled=1") == 0
    assert scalar("SELECT COUNT(*) FROM myems_historical_db.tbl_jishiyun_energy_readings") == 280
    result_path = STATE / "result.json"
    result = json.loads(result_path.read_text("utf-8-sig"))
    result["http_mapping_configuration"] = {
        "page_sizes": [50, 20], "exclusive_ownership": True, "range_write_and_replay": True,
        "authenticated_audit": True, "history_and_stale_rejected": True, "disposable_fixture_removed": True,
    }
    result_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print("PASS: real MySQL mapping ownership and 50+20 pages; disposable source range edit/replay, "
          "audit, stale/cross-source/activation/ownership rejection; fixture removed; retained mappings disabled")


def http_backfill():
    """Queue/cancel only in a disposable source with no worker assigned to it."""
    api = authenticated_api("")
    marker = "LOCAL disposable backfill probe " + uuid4().hex
    source_id = meter_id = point_id = task_id = None
    retained_runs = scalar("SELECT COUNT(*) FROM tbl_jishiyun_runs WHERE data_source_id=1")
    connection = database_factory()()
    cursor = connection.cursor()
    try:
        cursor.execute(
            "INSERT INTO tbl_data_sources(name,uuid,gateway_id,protocol,connection) "
            "SELECT %s,%s,gateway_id,protocol,'{}' FROM tbl_data_sources WHERE id=1", (marker, str(uuid4())),
        )
        source_id = cursor.lastrowid
        cursor.execute(
            "INSERT INTO tbl_meters(name,uuid,energy_category_id,is_counted,hourly_low_limit,"
            "hourly_high_limit,cost_center_id) VALUES(%s,%s,1,0,0,999999,1)", (marker, str(uuid4())),
        )
        meter_id = cursor.lastrowid
        cursor.execute(
            "INSERT INTO tbl_points(name,data_source_id,object_type,units,high_limit,low_limit,ratio,"
            "offset_constant,is_trend,is_virtual,address) VALUES(%s,%s,'JISHIYUN_REGISTER','kWh',999999,0,1,0,1,0,%s)",
            (marker, source_id, json.dumps({"provider": "jishiyun", "address": "0001", "field": "ZYGDNSZ"})),
        )
        point_id = cursor.lastrowid
        cursor.execute("INSERT INTO tbl_meters_points(meter_id,point_id) VALUES(%s,%s)", (meter_id, point_id))
        cursor.execute(
            "INSERT INTO tbl_jishiyun_meter_mappings(data_source_id,external_id,external_address,meter_id,"
            "point_id,initialization_run_id,archive_json,last_archive_sync_datetime_utc,configuration_status,"
            "is_enabled,collection_start_utc,collection_end_utc) "
            "VALUES(%s,1,'0001',%s,%s,1,%s,UTC_TIMESTAMP(),'ready',0,'2026-10-01','2026-10-04')",
            (source_id, meter_id, point_id, json.dumps({"ID": 1, "Address": "0001"})),
        )
        cursor.execute(
            "INSERT INTO tbl_jishiyun_tasks(data_source_id,name,task_type,is_enabled,provider_utc_offset_minutes,"
            "parameters_json,created_by_user_id,updated_by_user_id) "
            "VALUES(%s,%s,'energy_backfill',1,480,'{\"encoding\":\"json\"}',1,1)", (source_id, marker),
        )
        task_id = cursor.lastrowid
        connection.commit()
        queue = RunQueue(database_factory(), source_id)
        parent = str(uuid4())
        start, end = START, START + timedelta(days=2)
        # Model a submission interrupted after committing the first of two children.
        batch_id = register_batch(queue, task_id, parent, start, end, 1, 2)
        from uuid import UUID, uuid5
        first = queue.enqueue(task_id, str(uuid5(UUID(parent), "backfill:0")), start,
                              start + timedelta(days=1), 1, batch_key=parent)
        detail = api(f"batches/{batch_id}", source_id=source_id)
        assert detail["submitted_children"] == 1 and detail["unsubmitted_children"] == 1 and not detail["complete"]
        api(f"batches/{batch_id}", expected=404)  # Source isolation.
        listing = api("batches?limit=1", source_id=source_id)
        assert listing["items"][0]["id"] == batch_id and not listing["has_more"]
        resumed = api(f"batches/{batch_id}/resume", {}, source_id=source_id, expected=202)
        assert resumed["submitted_children"] == 2 and resumed["run_ids"][0] == first
        body = {"task_id": task_id, "request_key": parent, "window_start_utc": start.isoformat(),
                "window_end_utc": end.isoformat()}
        assert api("backfill", body, source_id=source_id, expected=202)["run_ids"] == resumed["run_ids"]
        api("backfill", dict(body, window_end_utc=(end + timedelta(days=1)).isoformat()),
            source_id=source_id, expected=409)
        claimed = queue.claim()
        assert claimed is not None
        cancelled = api(f"batches/{batch_id}/cancel", {}, source_id=source_id)
        assert cancelled["is_cancelled"] and cancelled["child_status_counts"] == {"cancelled": 2}
        api(f"batches/{batch_id}/cancel", {}, source_id=source_id)
        assert scalar(
            "SELECT COUNT(*) FROM tbl_jishiyun_events WHERE data_source_id=%s "
            "AND event_type='backfill_batch_cancelled' AND actor_user_id=1", (source_id,),
        ) == 1
        try:
            queue.renew(claimed)
        except PersistenceError:
            pass
        else:
            raise AssertionError("Cancelled child lease was accepted")
        api(f"batches/{batch_id}/resume", {}, source_id=source_id, expected=409)
        api("backfill", body, source_id=source_id, expected=409)
        assert scalar("SELECT COUNT(*) FROM tbl_jishiyun_runs WHERE data_source_id=%s", (source_id,)) == 2
    finally:
        connection.rollback()
        if source_id is not None:
            cursor.execute("SELECT name FROM tbl_data_sources WHERE id=%s", (source_id,))
            row = cursor.fetchone()
            if row is not None:
                assert row[0] == marker
                for table in ("tbl_jishiyun_events", "tbl_jishiyun_runs", "tbl_jishiyun_backfill_batches",
                              "tbl_jishiyun_worker_state", "tbl_jishiyun_meter_mappings", "tbl_jishiyun_tasks"):
                    cursor.execute("DELETE FROM " + table + " WHERE data_source_id=%s", (source_id,))
                cursor.execute("DELETE FROM tbl_meters_points WHERE meter_id=%s AND point_id=%s", (meter_id, point_id))
                cursor.execute("DELETE FROM tbl_points WHERE id=%s AND data_source_id=%s", (point_id, source_id))
                cursor.execute("DELETE FROM tbl_meters WHERE id=%s AND name=%s", (meter_id, marker))
                cursor.execute("DELETE FROM tbl_data_sources WHERE id=%s AND name=%s", (source_id, marker))
                connection.commit()
        cursor.close()
        connection.close()
    assert scalar("SELECT COUNT(*) FROM tbl_jishiyun_runs WHERE data_source_id=1") == retained_runs
    assert scalar("SELECT COUNT(*) FROM tbl_jishiyun_meter_mappings") == 70
    assert scalar("SELECT COUNT(*) FROM tbl_jishiyun_meter_mappings WHERE is_enabled=1") == 0
    assert scalar("SELECT COUNT(*) FROM myems_historical_db.tbl_jishiyun_energy_readings") == 280
    result_path = STATE / "result.json"
    result = json.loads(result_path.read_text("utf-8-sig"))
    result["http_backfill"] = {"resumed_children": 2, "cancelled_children": 2, "stale_lease_rejected": True,
                                "replay_and_scope_verified": True, "disposable_fixture_removed": True}
    result_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print("PASS: real MySQL/HTTP partial backfill resume/replay, scope/conflict guards, batch cancellation "
          "and stale-lease fencing; disposable fixture removed; retained 70 mappings disabled/raw280 unchanged")


def http_mapping_batch():
    """Test atomic mapping writes without provider calls or retained-source changes."""
    api = authenticated_api("mapping-batch")
    details = authenticated_api("mappings")
    baseline = scalar("SELECT COUNT(*) FROM tbl_jishiyun_meter_mappings")
    marker = "LOCAL disposable mapping batch " + uuid4().hex
    source_id = None
    entities = []
    connection = database_factory()()
    cursor = connection.cursor()
    try:
        cursor.execute(
            "INSERT INTO tbl_data_sources(name,uuid,gateway_id,protocol,connection) "
            "SELECT %s,%s,gateway_id,'JISHIYUN','{}' FROM tbl_data_sources WHERE id=1",
            (marker, str(uuid4())),
        )
        source_id = cursor.lastrowid
        for index in (1, 2):
            address = f"000{index}"
            cursor.execute(
                "INSERT INTO tbl_meters(name,uuid,energy_category_id,is_counted,hourly_low_limit,"
                "hourly_high_limit,cost_center_id) VALUES(%s,%s,1,0,0,999999,1)",
                (marker + str(index), str(uuid4())),
            )
            meter_id = cursor.lastrowid
            cursor.execute(
                "INSERT INTO tbl_points(name,data_source_id,object_type,units,high_limit,low_limit,ratio,"
                "offset_constant,is_trend,is_virtual,address) "
                "VALUES(%s,%s,'JISHIYUN_REGISTER','kWh',999999,0,1,0,1,0,%s)",
                (marker + str(index), source_id,
                 json.dumps({"provider": "jishiyun", "address": address, "field": "ZYGDNSZ"})),
            )
            point_id = cursor.lastrowid
            entities.append((meter_id, point_id))
            cursor.execute("INSERT INTO tbl_meters_points(meter_id,point_id) VALUES(%s,%s)", (meter_id, point_id))
            cursor.execute(
                "INSERT INTO tbl_jishiyun_meter_mappings(data_source_id,external_id,external_address,"
                "meter_id,point_id,initialization_run_id,archive_json,last_archive_sync_datetime_utc) "
                "VALUES(%s,%s,%s,%s,%s,1,%s,UTC_TIMESTAMP())",
                (source_id, index, address, meter_id, point_id, json.dumps({"ID": index, "Address": address})),
            )
        connection.commit()
        rows = details("", source_id=source_id)["items"]
        body = {"action": "edit_range", "confirm_kwh_direct": False,
                "collection_start_utc": "2026-10-01T00:00:00+08:00", "collection_end_utc": None,
                "items": [{"id": row["id"], "expected_configuration": details(
                    f"/{row['id']}", source_id=source_id)["configuration"]} for row in rows]}
        assert len(api("", body, source_id=source_id)["items"]) == 2
        assert all(not item["changed"] for item in api("", body, source_id=source_id)["items"])
        current = details("", source_id=source_id)["items"]
        assert all(row["collection_start_utc"] == "2026-09-30T16:00:00" for row in current)
        body.update(action="enable_raw", confirm_kwh_direct=True,
                    collection_start_utc=None, collection_end_utc=None)
        body["items"] = [{"id": row["id"], "expected_configuration": details(
            f"/{row['id']}", source_id=source_id)["configuration"]} for row in current]
        # First mapping is updated before the second fails, then the entire transaction must roll back.
        cursor.execute("UPDATE tbl_points SET units='Wh' WHERE id=%s", (entities[1][1],))
        connection.commit()
        api("", body, source_id=source_id, expected=409)
        assert scalar("SELECT SUM(is_enabled) FROM tbl_jishiyun_meter_mappings WHERE data_source_id=%s",
                      (source_id,)) == 0
        assert scalar("SELECT COUNT(*) FROM tbl_jishiyun_events WHERE data_source_id=%s "
                      "AND event_type='admin_mapping_enable_raw'", (source_id,)) == 0
        cursor.execute("UPDATE tbl_points SET units='kWh' WHERE id=%s", (entities[1][1],))
        connection.commit()
        assert all(item["changed"] for item in api("", body, source_id=source_id)["items"])
        assert all(not item["changed"] for item in api("", body, source_id=source_id)["items"])
        assert scalar("SELECT COUNT(*) FROM tbl_jishiyun_events WHERE data_source_id=%s "
                      "AND event_type='admin_mapping_enable_raw' AND actor_user_id=1", (source_id,)) == 2
    finally:
        connection.rollback()
        if source_id is not None:
            cursor.execute("SELECT name FROM tbl_data_sources WHERE id=%s", (source_id,))
            row = cursor.fetchone()
            if row is not None:
                assert row[0] == marker
                cursor.execute("DELETE FROM tbl_jishiyun_events WHERE data_source_id=%s", (source_id,))
                cursor.execute("DELETE FROM tbl_jishiyun_meter_mappings WHERE data_source_id=%s", (source_id,))
                for meter_id, point_id in entities:
                    cursor.execute("DELETE FROM tbl_meters_points WHERE meter_id=%s AND point_id=%s",
                                   (meter_id, point_id))
                    cursor.execute("DELETE FROM tbl_points WHERE id=%s AND data_source_id=%s", (point_id, source_id))
                    cursor.execute("DELETE FROM tbl_meters WHERE id=%s AND name LIKE %s", (meter_id, marker + "%"))
                cursor.execute("DELETE FROM tbl_data_sources WHERE id=%s AND name=%s", (source_id, marker))
                connection.commit()
        cursor.close()
        connection.close()
    assert scalar("SELECT COUNT(*) FROM tbl_jishiyun_meter_mappings") == baseline
    print("PASS: real MySQL/HTTP batch range UTC+8 conversion, replay, all-or-none enablement, audit; "
          "disposable source removed; no provider calls")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("schema", "workflow", "resume-after-missing", "cancellation", "http-initialization", "http-tasks", "http-mappings", "http-backfill", "http-mapping-batch"))
    args = parser.parse_args()
    if args.command == "schema":
        check_schema()
    elif args.command == "cancellation":
        cancellation()
    elif args.command == "http-initialization":
        http_initialization()
    elif args.command == "http-tasks":
        http_tasks()
    elif args.command == "http-mappings":
        http_mappings()
    elif args.command == "http-backfill":
        http_backfill()
    elif args.command == "http-mapping-batch":
        http_mapping_batch()
    else:
        workflow(resume_after_missing=args.command == "resume-after-missing")
