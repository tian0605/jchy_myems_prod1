"""Administrator-only acquisition observability; provider calls remain in the worker."""

import logging
from contextlib import contextmanager
from datetime import date, datetime, timezone
from decimal import Decimal
import json
from uuid import UUID

import falcon
import mysql.connector
from myems_jishiyun.contracts import ContractError
from myems_jishiyun.batches import batch_status
from myems_jishiyun.execution import RunQueue
from myems_jishiyun.initialization import ArchiveInitializer, configuration_from_json
from myems_jishiyun.persistence import PersistenceError
from myems_jishiyun.processing import reading_processing
from myems_jishiyun.scheduling import enqueue_backfill, scheduled_window, split_backfill

import config
from core.useractivity import admin_control, get_request_context_value


logger = logging.getLogger(__name__)
COLLECTIONS = {
    "business": ("tbl_jishiyun_business_jobs", (
        "id", "run_id", "actor_user_id", "status", "attempt_count", "gate_code",
        "register_insert_count", "hour_insert_count", "next_attempt_datetime_utc", "updated_datetime_utc",
    )),
    "batches": ("tbl_jishiyun_backfill_batches", (
        "id", "task_id", "requested_by_user_id", "window_start_utc", "window_end_utc",
        "child_count", "is_cancelled", "cancelled_by_user_id", "created_datetime_utc",
    )),
    "tasks": ("tbl_jishiyun_tasks", (
        "id", "name", "task_type", "is_enabled", "schedule_minute", "delay_minutes",
        "lookback_hours", "provider_utc_offset_minutes", "updated_datetime_utc",
    )),
    "runs": ("tbl_jishiyun_runs", (
        "id", "task_id", "status", "window_start_utc", "window_end_utc", "requested_datetime_utc",
        "started_datetime_utc", "finished_datetime_utc",
        "attempt_count", "request_count", "expected_count", "returned_count", "valid_count",
        "missing_count", "invalid_count", "unexpected_count", "error_code", "next_attempt_datetime_utc",
    )),
    "alerts": ("tbl_jishiyun_alerts", (
        "id", "external_address", "reading_datetime_utc", "alert_type", "status",
        "first_seen_datetime_utc", "last_seen_datetime_utc", "recovered_datetime_utc",
        "acknowledged_by_user_id", "acknowledged_datetime_utc",
    )),
    "events": ("tbl_jishiyun_events", (
        "id", "run_id", "mapping_id", "alert_id", "actor_user_id", "event_type", "created_datetime_utc",
    )),
    "mappings": ("tbl_jishiyun_meter_mappings", (
        "id", "external_address", "external_id", "meter_id", "point_id", "configuration_status",
        "is_enabled", "collection_start_utc", "collection_end_utc", "last_complete_datetime_utc",
    )),
}
ITEM_COLUMNS = (
    "id", "external_address", "external_id", "status", "meter_id", "point_id", "error_message",
)
READING_COLUMNS = (
    "id", "external_address", "external_id", "reading_datetime_utc", "forward_active_register",
    "quality", "revision", "first_run_id", "last_run_id",
)
TASK_FIELDS = (
    "name", "task_type", "schedule_minute", "delay_minutes",
    "lookback_hours", "provider_utc_offset_minutes", "encoding",
)


def task_configuration(body):
    if not isinstance(body, dict) or set(body) != set(TASK_FIELDS):
        raise falcon.HTTPBadRequest(title="Invalid task configuration fields")
    if not isinstance(body["name"], str) or not body["name"].strip() or body["name"] != body["name"].strip() or len(body["name"]) > 255:
        raise falcon.HTTPBadRequest(title="Task name must contain 1 to 255 characters without surrounding whitespace")
    if body["task_type"] not in ("meter_initialize", "energy_collect", "energy_backfill"):
        raise falcon.HTTPBadRequest(title="Unsupported task type")
    if body["encoding"] not in ("json", "form"):
        raise falcon.HTTPBadRequest(title="Unsupported provider encoding")
    try:
        scheduled_window(
            datetime.now().astimezone(), body["provider_utc_offset_minutes"], body["schedule_minute"],
            body["delay_minutes"], body["lookback_hours"],
        )
    except ContractError as exc:
        raise falcon.HTTPBadRequest(title="Invalid task settings", description=str(exc)) from None
    return {key: body[key] for key in TASK_FIELDS}


def task_snapshot(cursor, source_id, task_id):
    cursor.execute(
        "SELECT name, task_type, schedule_minute, delay_minutes, lookback_hours, "
        "provider_utc_offset_minutes, parameters_json, is_enabled FROM tbl_jishiyun_tasks "
        "WHERE id=%s AND data_source_id=%s FOR UPDATE", (task_id, source_id),
    )
    row = cursor.fetchone()
    if row is None:
        raise falcon.HTTPNotFound(title="Task not found in this source")
    try:
        parameters = json.loads(row[6])
    except (ValueError, TypeError):
        raise falcon.HTTPConflict(title="Stored task configuration is invalid") from None
    # Do not discard future/legacy settings or expose arbitrary parameters to clients.
    if not isinstance(parameters, dict) or set(parameters) != {"encoding"}:
        raise falcon.HTTPConflict(title="Task has settings not supported by this editor")
    configuration = dict(zip(TASK_FIELDS, row[:6] + (parameters["encoding"],)))
    return task_configuration(configuration), bool(row[7])


def task_used(cursor, source_id, task_id):
    cursor.execute("SELECT id FROM tbl_jishiyun_runs WHERE task_id=%s AND data_source_id=%s LIMIT 1",
                   (task_id, source_id))
    used = cursor.fetchone() is not None
    cursor.execute("SELECT id FROM tbl_jishiyun_backfill_batches WHERE task_id=%s AND data_source_id=%s LIMIT 1",
                   (task_id, source_id))
    return cursor.fetchone() is not None or used


def positive(value, label="identifier"):
    if isinstance(value, str) and value.isascii() and value.isdecimal() and len(value) <= 19:
        value = int(value)
    if type(value) is not int or not 0 < value <= 9223372036854775807:
        raise falcon.HTTPBadRequest(title="Invalid acquisition request", description="Invalid " + label)
    return value


def actor(req):
    return positive(get_request_context_value(req, "user_id"), "authenticated user")


def serialize(value):
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return value


@contextmanager
def database(source_id, write=False, extra_tables=()):
    connection = None
    cursor = None
    try:
        connection = mysql.connector.connect(**dict(config.myems_system_db, autocommit=False))
        connection.start_transaction()
        cursor = connection.cursor()
        if write:
            tables = ("tbl_data_sources", "tbl_jishiyun_tasks", "tbl_jishiyun_runs",
                      "tbl_jishiyun_alerts", "tbl_jishiyun_events", "tbl_jishiyun_backfill_batches") + extra_tables
            cursor.execute(
                "SELECT TABLE_NAME, ENGINE FROM information_schema.TABLES "
                "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME IN (" + ",".join(["%s"] * len(tables)) + ")",
                tables,
            )
            rows = cursor.fetchall()
            if len(rows) != len(tables) or {row[0] for row in rows} != set(tables) or any(row[1] != "InnoDB" for row in rows):
                raise falcon.HTTPConflict(title="Acquisition schema unavailable", description="Transactional extension tables required")
        cursor.execute("SELECT id FROM tbl_data_sources WHERE id = %s" + (" FOR UPDATE" if write else ""), (source_id,))
        if cursor.fetchone() is None:
            raise falcon.HTTPNotFound(title="Data source not found")
        yield cursor
        connection.commit()
    except falcon.HTTPError:
        if connection is not None:
            connection.rollback()
        raise
    except mysql.connector.Error:
        if connection is not None:
            connection.rollback()
        # Driver exceptions may contain connection details. Never format them.
        logger.error("Jishiyun administrator database operation failed")
        raise falcon.HTTPInternalServerError(
            title="Acquisition operation failed", description="Check database schema and API service health",
        ) from None
    finally:
        try:
            if cursor is not None:
                cursor.close()
        finally:
            if connection is not None:
                connection.close()


def audit(cursor, source_id, user_id, kind, details, run_id=None, alert_id=None, mapping_id=None):
    cursor.execute(
        "INSERT INTO tbl_jishiyun_events "
        "(data_source_id, run_id, alert_id, actor_user_id, event_type, details_json, mapping_id) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s)",
        (source_id, run_id, alert_id, user_id, kind, json.dumps(details, sort_keys=True), mapping_id),
    )


class Workbench:
    @staticmethod
    def on_options(req, resp, id_, **kwargs):
        resp.status = falcon.HTTP_200

    @staticmethod
    def on_post(req, resp, id_, collection):
        admin_control(req)
        if collection != "tasks":
            raise falcon.HTTPMethodNotAllowed(["GET", "OPTIONS"])
        source_id, user_id = positive(id_), actor(req)
        body = initialization_body(req, ("request_key", "configuration"))
        configuration = task_configuration(body["configuration"])
        try:
            key = str(UUID(body["request_key"])) if isinstance(body["request_key"], str) else None
        except ValueError:
            key = None
        if key is None:
            raise falcon.HTTPBadRequest(title="Request key must be a UUID string")
        with database(source_id, write=True) as cursor:
            cursor.execute(
                "SELECT details_json, actor_user_id FROM tbl_jishiyun_events "
                "WHERE data_source_id=%s AND event_type='admin_task_created' "
                "AND JSON_UNQUOTE(JSON_EXTRACT(details_json, '$.request_key'))=%s ORDER BY id LIMIT 1",
                (source_id, key),
            )
            previous = cursor.fetchone()
            if previous is not None:
                details = json.loads(previous[0])
                if details["configuration"] != configuration or previous[1] != user_id:
                    raise falcon.HTTPConflict(title="Task request key belongs to a different submission")
                task_id = details["task_id"]
            else:
                cursor.execute(
                    "INSERT INTO tbl_jishiyun_tasks "
                    "(data_source_id,name,task_type,is_enabled,schedule_minute,delay_minutes,lookback_hours,"
                    "provider_utc_offset_minutes,parameters_json,created_by_user_id,updated_by_user_id) "
                    "VALUES (%s,%s,%s,FALSE,%s,%s,%s,%s,%s,%s,%s)",
                    (source_id,) + tuple(configuration[key] for key in TASK_FIELDS[:-1]) +
                    (json.dumps({"encoding": configuration["encoding"]}), user_id, user_id),
                )
                task_id = cursor.lastrowid
                audit(cursor, source_id, user_id, "admin_task_created", {
                    "request_key": key, "task_id": task_id, "configuration": configuration,
                })
        resp.status = falcon.HTTP_201
        resp.media = {"id": task_id, "data_source_id": source_id}

    @staticmethod
    def on_get(req, resp, id_, collection):
        admin_control(req)
        source_id = positive(id_)
        if collection not in COLLECTIONS and collection != "readings":
            raise falcon.HTTPNotFound(title="Unknown acquisition collection")
        limit = positive(req.get_param("limit") or "50", "page limit")
        if limit > 200:
            raise falcon.HTTPBadRequest(title="Page size exceeds 200")
        before = req.get_param("before_id")
        if collection == "readings":
            if req.get_param("status") is not None:
                raise falcon.HTTPBadRequest(title="Raw quality is not a downstream processing status")
            columns = ("id", "external_address", "meter_name") + READING_COLUMNS[2:]
            query = (
                "SELECT r.id,r.external_address,m.name AS meter_name,"
                + ",".join("r." + column for column in READING_COLUMNS[2:])
                + " FROM myems_historical_db.tbl_jishiyun_energy_readings r "
                "LEFT JOIN tbl_jishiyun_meter_mappings j "
                "ON j.data_source_id=r.data_source_id AND j.external_address=r.external_address "
                "AND j.external_id=r.external_id "
                "LEFT JOIN tbl_meters m ON m.id=j.meter_id WHERE r.data_source_id=%s"
            )
            params = [source_id]
            if before is not None:
                query += " AND r.id < %s"
                params.append(positive(before, "page cursor"))
            query += " ORDER BY r.id DESC LIMIT %s"
            with database(source_id, write=True) as cursor:
                cursor.execute(query, tuple(params + [limit + 1]))
                rows = cursor.fetchall()
                try:
                    processing = reading_processing(cursor, source_id, [(row[1], row[4]) for row in rows[:limit]])
                except PersistenceError:
                    logger.error("Jishiyun reading processing audit verification failed")
                    raise falcon.HTTPConflict(
                        title="Processing status unavailable",
                        description="Stored audit or timestamp contract is invalid; check API service logs",
                    ) from None
            items = [dict(zip(columns, (serialize(value) for value in row))) for row in rows[:limit]]
            for item, status in zip(items, processing):
                item.update(status)
            resp.media = {
                "items": items, "has_more": len(rows) > limit,
                "next_before_id": items[-1]["id"] if items else None,
            }
            return
        params = [source_id]
        table, columns = COLLECTIONS[collection]
        query = "SELECT " + ", ".join(columns) + " FROM " + table + " WHERE data_source_id = %s"
        if before is not None:
            query += " AND id < %s"
            params.append(positive(before, "page cursor"))
        status = req.get_param("status")
        if status is not None:
            allowed = {
                "runs": {"preview", "queued", "running", "partial", "done", "error", "cancelled"},
                "alerts": {"active", "recovered"},
                "business": {"queued", "running", "blocked", "done", "error"},
            }.get(collection, set())
            if status not in allowed:
                raise falcon.HTTPBadRequest(title="Invalid acquisition status filter")
            query += " AND status = %s"
            params.append(status)
        query += " ORDER BY id DESC LIMIT %s"
        params.append(limit + 1)
        with database(source_id) as cursor:
            cursor.execute(query, tuple(params))
            rows = cursor.fetchall()
            task_history = {}
            if collection == "tasks" and rows[:limit]:
                identities = [row[0] for row in rows[:limit]]
                cursor.execute(
                    "SELECT t.id,"
                    "(SELECT MAX(r.finished_datetime_utc) FROM tbl_jishiyun_runs r "
                    "WHERE r.data_source_id=t.data_source_id AND r.task_id=t.id AND r.status='done'),"
                    "(SELECT r.id FROM tbl_jishiyun_runs r WHERE r.data_source_id=t.data_source_id "
                    "AND r.task_id=t.id ORDER BY r.id DESC LIMIT 1),"
                    "(SELECT r.status FROM tbl_jishiyun_runs r WHERE r.data_source_id=t.data_source_id "
                    "AND r.task_id=t.id ORDER BY r.id DESC LIMIT 1),"
                    "(SELECT r.finished_datetime_utc FROM tbl_jishiyun_runs r WHERE r.data_source_id=t.data_source_id "
                    "AND r.task_id=t.id ORDER BY r.id DESC LIMIT 1) "
                    "FROM tbl_jishiyun_tasks t WHERE t.data_source_id=%s AND t.id IN ("
                    + ",".join(["%s"] * len(identities)) + ")",
                    tuple([source_id] + identities),
                )
                task_history = {row[0]: row[1:] for row in cursor.fetchall()}
        items = [dict(zip(columns, (serialize(value) for value in row))) for row in rows[:limit]]
        if collection == "tasks":
            for item in items:
                item.update(dict(zip(
                    ("last_success_finished_datetime_utc", "latest_run_id", "latest_run_status",
                     "latest_run_finished_datetime_utc"),
                    (serialize(value) for value in task_history[item["id"]]),
                )))
        resp.media = {
            "items": items, "has_more": len(rows) > limit,
            "next_before_id": items[-1]["id"] if items else None,
        }


class RunItems:
    on_options = staticmethod(Workbench.on_options)

    @staticmethod
    def on_get(req, resp, id_, run_id):
        admin_control(req)
        source_id, run_id = positive(id_), positive(run_id)
        limit = positive(req.get_param("limit") or "50", "page limit")
        if limit > 200:
            raise falcon.HTTPBadRequest(title="Page size exceeds 200")
        before = req.get_param("before_id")
        with database(source_id) as cursor:
            cursor.execute(
                "SELECT id,window_start_utc,window_end_utc FROM tbl_jishiyun_runs WHERE id=%s AND data_source_id=%s",
                (run_id, source_id),
            )
            run = cursor.fetchone()
            if run is None:
                raise falcon.HTTPNotFound(title="Run not found in this source")
            energy = run[1] is not None and run[2] is not None
            columns = READING_COLUMNS if energy else ITEM_COLUMNS
            if energy:
                query = ("SELECT " + ", ".join(columns) +
                         " FROM myems_historical_db.tbl_jishiyun_energy_readings WHERE data_source_id=%s "
                         "AND reading_datetime_utc >= %s AND reading_datetime_utc < %s")
                params = [source_id, run[1], run[2]]
            else:
                query = "SELECT " + ", ".join(columns) + " FROM tbl_jishiyun_run_items WHERE run_id=%s"
                params = [run_id]
            if before is not None:
                query += " AND id < %s"
                params.append(positive(before, "page cursor"))
            query += " ORDER BY id DESC LIMIT %s"
            cursor.execute(query, tuple(params + [limit + 1]))
            rows = cursor.fetchall()
        items = [dict(zip(columns, (serialize(value) for value in row))) for row in rows[:limit]]
        resp.media = {"items": items, "has_more": len(rows) > limit,
                      "next_before_id": items[-1]["id"] if items else None,
                      "detail_kind": "energy_current_window" if energy else "archive_items"}


class TaskConfiguration:
    on_options = staticmethod(Workbench.on_options)

    @staticmethod
    def on_get(req, resp, id_, item_id):
        admin_control(req)
        source_id, task_id = positive(id_), positive(item_id)
        with database(source_id, write=True) as cursor:
            configuration, enabled = task_snapshot(cursor, source_id, task_id)
            used = task_used(cursor, source_id, task_id)
        resp.media = {"id": task_id, "configuration": configuration, "is_enabled": enabled, "has_runs": used}

    @staticmethod
    def on_post(req, resp, id_, item_id):
        admin_control(req)
        source_id, task_id, user_id = positive(id_), positive(item_id), actor(req)
        body = initialization_body(req, ("configuration", "expected_configuration"))
        configuration = task_configuration(body["configuration"])
        expected = task_configuration(body["expected_configuration"])
        with database(source_id, write=True) as cursor:
            previous, enabled = task_snapshot(cursor, source_id, task_id)
            if previous == configuration:
                resp.media = {"id": task_id, "changed": False}
                return
            if previous != expected:
                raise falcon.HTTPConflict(title="Task changed; reload before editing")
            if enabled:
                raise falcon.HTTPConflict(title="Pause the task before editing")
            if previous["task_type"] != configuration["task_type"]:
                raise falcon.HTTPConflict(title="Task type is immutable; create a new task")
            cursor.execute(
                "SELECT id FROM tbl_jishiyun_runs WHERE task_id=%s AND data_source_id=%s "
                "AND status IN ('preview','queued','running') LIMIT 1", (task_id, source_id),
            )
            if cursor.fetchone() is not None:
                raise falcon.HTTPConflict(title="Cancel or finish open runs before editing")
            used = task_used(cursor, source_id, task_id)
            if used and any(previous[key] != configuration[key] for key in TASK_FIELDS if key != "name"):
                raise falcon.HTTPConflict(title="Execution settings of used tasks are immutable; create a new task")
            cursor.execute(
                "UPDATE tbl_jishiyun_tasks SET name=%s,schedule_minute=%s,delay_minutes=%s,lookback_hours=%s,"
                "provider_utc_offset_minutes=%s,parameters_json=%s,updated_by_user_id=%s,"
                "updated_datetime_utc=UTC_TIMESTAMP() WHERE id=%s AND data_source_id=%s",
                tuple(configuration[key] for key in ("name", "schedule_minute", "delay_minutes",
                                                     "lookback_hours", "provider_utc_offset_minutes")) +
                (json.dumps({"encoding": configuration["encoding"]}), user_id, task_id, source_id),
            )
            audit(cursor, source_id, user_id, "admin_task_updated",
                  {"task_id": task_id, "before": previous, "after": configuration})
        resp.media = {"id": task_id, "changed": True}


MAPPING_TABLES = ("tbl_jishiyun_meter_mappings", "tbl_meters", "tbl_points", "tbl_meters_points")
MAPPING_COLUMNS = (
    "id", "external_address", "external_id", "meter_id", "point_id", "configuration_status", "is_enabled",
    "collection_start_utc", "collection_end_utc",
)


def mapping_snapshot(cursor, source_id, mapping_id):
    cursor.execute(
        "SELECT " + ",".join(MAPPING_COLUMNS) + ",archive_json FROM tbl_jishiyun_meter_mappings "
        "WHERE id=%s AND data_source_id=%s FOR UPDATE", (mapping_id, source_id),
    )
    row = cursor.fetchone()
    if row is None:
        raise falcon.HTTPNotFound(title="Mapping not found in this source")
    snapshot = dict(zip(MAPPING_COLUMNS, (serialize(value) for value in row[:-1])))
    cursor.execute("SELECT name FROM tbl_meters WHERE id=%s FOR UPDATE", (snapshot["meter_id"],))
    meter = cursor.fetchone()
    cursor.execute(
        "SELECT name,data_source_id,object_type,address,ratio,offset_constant,is_virtual,is_trend "
        "FROM tbl_points WHERE id=%s FOR UPDATE", (snapshot["point_id"],),
    )
    point = cursor.fetchone()
    if meter is None or point is None:
        raise falcon.HTTPConflict(title="Mapping has missing local entities")
    try:
        address = json.loads(point[3])
        archive = json.loads(row[-1])
    except (ValueError, TypeError):
        raise falcon.HTTPConflict(title="Mapping contains invalid ownership metadata") from None
    if (
        point[1] != source_id or point[2] != "JISHIYUN_REGISTER"
        or address != {"provider": "jishiyun", "address": snapshot["external_address"], "field": "ZYGDNSZ"}
        or point[4] != 1 or point[5] != 0 or point[6] != 0 or point[7] != 1
        or not isinstance(archive, dict) or archive.get("Address") != snapshot["external_address"]
        or type(archive.get("ID")) is not int or archive["ID"] != snapshot["external_id"]
    ):
        raise falcon.HTTPConflict(title="Mapping ownership or staged register configuration has changed")
    cursor.execute(
        "SELECT meter_id,point_id FROM tbl_meters_points WHERE meter_id=%s OR point_id=%s FOR UPDATE",
        (snapshot["meter_id"], snapshot["point_id"]),
    )
    if cursor.fetchall() != [(snapshot["meter_id"], snapshot["point_id"])]:
        raise falcon.HTTPConflict(title="Mapping requires exactly one exclusive meter-point association")
    return snapshot, {"meter_name": meter[0], "point_name": point[0], "ownership_verified": True,
                      "activation_available": False}


def collection_time(value, required=False):
    if value is None and not required:
        return None
    if not isinstance(value, str) or not 1 <= len(value) <= 40:
        raise falcon.HTTPBadRequest(title="Collection time must be an explicit timezone-aware ISO timestamp")
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError()
        timestamp = timestamp.astimezone(timezone.utc).replace(tzinfo=None)
    except (ValueError, OverflowError):
        raise falcon.HTTPBadRequest(title="Collection time must be an explicit timezone-aware ISO timestamp") from None
    if timestamp.year < 1000 or timestamp.minute % 15 or timestamp.second or timestamp.microsecond:
        raise falcon.HTTPBadRequest(title="Collection time must align to a 15-minute UTC slot within MySQL date range")
    return timestamp


class MappingConfiguration:
    on_options = staticmethod(Workbench.on_options)

    @staticmethod
    def on_get(req, resp, id_, item_id):
        admin_control(req)
        source_id, mapping_id = positive(id_), positive(item_id)
        with database(source_id, write=True, extra_tables=MAPPING_TABLES) as cursor:
            snapshot, detail = mapping_snapshot(cursor, source_id, mapping_id)
        resp.media = {"configuration": snapshot, **detail}

    @staticmethod
    def on_post(req, resp, id_, item_id):
        admin_control(req)
        source_id, mapping_id, user_id = positive(id_), positive(item_id), actor(req)
        body = initialization_body(req, ("collection_start_utc", "collection_end_utc", "expected_configuration"))
        start = collection_time(body["collection_start_utc"], required=True)
        end = collection_time(body["collection_end_utc"])
        if end is not None and end <= start:
            raise falcon.HTTPBadRequest(title="Collection end must be later than start and is exclusive")
        expected = body["expected_configuration"]
        if not isinstance(expected, dict) or set(expected) != set(MAPPING_COLUMNS):
            raise falcon.HTTPBadRequest(title="Expected mapping snapshot is required")
        with database(source_id, write=True, extra_tables=MAPPING_TABLES) as cursor:
            changed = update_mapping_range(cursor, source_id, mapping_id, user_id, start, end, expected)
        resp.media = {"id": mapping_id, "changed": changed, "activation_available": False}


def update_mapping_range(cursor, source_id, mapping_id, user_id, start, end, expected):
    previous, _ = mapping_snapshot(cursor, source_id, mapping_id)
    if previous["is_enabled"] or previous["configuration_status"] not in ("pending", "ready"):
        raise falcon.HTTPConflict(title="Mapping must be disabled and not in conflict before configuration")
    next_range = {"collection_start_utc": start.isoformat(), "collection_end_utc": end.isoformat() if end else None}
    if all(previous[key] == value for key, value in next_range.items()):
        return False
    if previous != expected:
        raise falcon.HTTPConflict(title="Mapping changed; reload before editing")
    cursor.execute(
        "SELECT r.id FROM tbl_jishiyun_runs r LEFT JOIN tbl_jishiyun_tasks t ON t.id=r.task_id "
        "AND t.data_source_id=r.data_source_id WHERE r.data_source_id=%s AND "
        "(t.id IS NULL OR t.task_type IN ('energy_collect','energy_backfill') "
        "OR r.status IN ('preview','queued','running')) "
        "LIMIT 1", (source_id,),
    )
    if cursor.fetchone() is not None:
        raise falcon.HTTPConflict(
            title="Collection range is frozen for this source",
            description="Open runs or energy history require a future audited correction/recomputation workflow",
        )
    cursor.execute("SELECT id FROM tbl_jishiyun_backfill_batches WHERE data_source_id=%s LIMIT 1", (source_id,))
    if cursor.fetchone() is not None:
        raise falcon.HTTPConflict(title="Backfill history freezes collection ranges for this source")
    cursor.execute(
        "UPDATE tbl_jishiyun_meter_mappings SET collection_start_utc=%s,collection_end_utc=%s,"
        "updated_datetime_utc=UTC_TIMESTAMP() WHERE id=%s AND data_source_id=%s",
        (start, end, mapping_id, source_id),
    )
    audit(cursor, source_id, user_id, "admin_mapping_range_updated",
          {"before": previous, "after": dict(previous, **next_range), "activation_available": False},
          mapping_id=mapping_id)
    return True


class MappingCollection:
    on_options = staticmethod(Workbench.on_options)

    @staticmethod
    def on_post(req, resp, id_, item_id):
        admin_control(req)
        source_id, mapping_id, user_id = positive(id_), positive(item_id), actor(req)
        body = initialization_body(req, ("action", "expected_configuration", "confirm_kwh_direct"))
        if body["action"] not in ("enable_raw", "pause_raw"):
            raise falcon.HTTPBadRequest(title="Unsupported raw collection action")
        if body["confirm_kwh_direct"] is not True:
            raise falcon.HTTPBadRequest(title="Confirm ZYGDNSZ is kWh without scaling")
        expected = body["expected_configuration"]
        if not isinstance(expected, dict) or set(expected) != set(MAPPING_COLUMNS):
            raise falcon.HTTPBadRequest(title="Expected mapping snapshot is required")
        enabled = body["action"] == "enable_raw"
        with database(source_id, write=True, extra_tables=MAPPING_TABLES) as cursor:
            changed = update_mapping_collection(cursor, source_id, mapping_id, user_id, enabled, expected)
        resp.media = {"id": mapping_id, "changed": changed, "business_activation": False}


def update_mapping_collection(cursor, source_id, mapping_id, user_id, enabled, expected):
    previous, _ = mapping_snapshot(cursor, source_id, mapping_id)
    if previous["configuration_status"] not in ("pending", "ready"):
        raise falcon.HTTPConflict(title="Resolve mapping conflict before raw collection")
    cursor.execute("SELECT units FROM tbl_points WHERE id=%s FOR UPDATE", (previous["point_id"],))
    if cursor.fetchone() != ("kWh",):
        raise falcon.HTTPConflict(title="Register unit must be kWh; no implicit conversion is supported")
    if enabled:
        if previous["collection_start_utc"] is None:
            raise falcon.HTTPConflict(title="Configure the collection start before enabling raw collection")
        start = collection_time(previous["collection_start_utc"] + "Z", required=True)
        end = collection_time(previous["collection_end_utc"] + "Z") if previous["collection_end_utc"] else None
        if end is not None and end <= start:
            raise falcon.HTTPConflict(title="Stored collection range is invalid")
    after = dict(previous, is_enabled=int(enabled))
    if enabled:
        after["configuration_status"] = "ready"
    if previous == after:
        return False
    if previous != expected:
        raise falcon.HTTPConflict(title="Mapping changed; reload before changing raw collection")
    cursor.execute(
        "SELECT id FROM tbl_jishiyun_runs WHERE data_source_id=%s "
        "AND status IN ('preview','queued','running') LIMIT 1", (source_id,),
    )
    if cursor.fetchone() is not None:
        raise falcon.HTTPConflict(title="Finish or cancel open source runs before changing raw collection")
    cursor.execute(
        "SELECT id FROM tbl_jishiyun_backfill_batches WHERE data_source_id=%s "
        "AND is_cancelled=FALSE LIMIT 1", (source_id,),
    )
    if cursor.fetchone() is not None:
        raise falcon.HTTPConflict(
            title="Cancel retained backfill batches before changing raw collection",
            description="Changing scope must not change a later batch resume; cancellation preserves committed data",
        )
    cursor.execute(
        "UPDATE tbl_jishiyun_meter_mappings SET configuration_status=%s,is_enabled=%s,"
        "updated_datetime_utc=UTC_TIMESTAMP() WHERE id=%s AND data_source_id=%s",
        (after["configuration_status"], after["is_enabled"], mapping_id, source_id),
    )
    audit(cursor, source_id, user_id, "admin_mapping_" + ("enable_raw" if enabled else "pause_raw"),
          {"before": previous, "after": after, "confirm_kwh_direct": True, "business_activation": False},
          mapping_id=mapping_id)
    return True


class MappingBatch:
    on_options = staticmethod(Workbench.on_options)

    @staticmethod
    def on_post(req, resp, id_):
        admin_control(req)
        source_id, user_id = positive(id_), actor(req)
        body = initialization_body(req, ("action", "items", "collection_start_utc",
                                         "collection_end_utc", "confirm_kwh_direct"))
        if body["action"] not in ("edit_range", "enable_raw", "pause_raw"):
            raise falcon.HTTPBadRequest(title="Unsupported mapping batch action")
        if not isinstance(body["items"], list) or not 1 <= len(body["items"]) <= 200:
            raise falcon.HTTPBadRequest(title="Select between 1 and 200 mappings")
        items = []
        for item in body["items"]:
            if not isinstance(item, dict) or set(item) != {"id", "expected_configuration"}:
                raise falcon.HTTPBadRequest(title="Invalid mapping batch item")
            mapping_id = positive(item["id"])
            expected = item["expected_configuration"]
            if not isinstance(expected, dict) or set(expected) != set(MAPPING_COLUMNS):
                raise falcon.HTTPBadRequest(title="Expected mapping snapshot is required")
            items.append((mapping_id, expected))
        if len({item[0] for item in items}) != len(items):
            raise falcon.HTTPBadRequest(title="Duplicate mappings are not allowed")
        start = end = None
        if body["action"] == "edit_range":
            if body["confirm_kwh_direct"] is not False:
                raise falcon.HTTPBadRequest(title="Range editing must not enable raw collection")
            start = collection_time(body["collection_start_utc"], required=True)
            end = collection_time(body["collection_end_utc"])
            if end is not None and end <= start:
                raise falcon.HTTPBadRequest(title="Collection end must be later than start")
        elif (body["confirm_kwh_direct"] is not True or body["collection_start_utc"] is not None
              or body["collection_end_utc"] is not None):
            raise falcon.HTTPBadRequest(title="Confirm direct kWh values; collection controls cannot edit ranges")
        results = []
        with database(source_id, write=True, extra_tables=MAPPING_TABLES) as cursor:
            for mapping_id, expected in sorted(items):
                try:
                    if body["action"] == "edit_range":
                        changed = update_mapping_range(cursor, source_id, mapping_id, user_id, start, end, expected)
                    else:
                        changed = update_mapping_collection(
                            cursor, source_id, mapping_id, user_id, body["action"] == "enable_raw", expected,
                        )
                except falcon.HTTPError as exc:
                    raise falcon.HTTPConflict(
                        title="Mapping batch rejected; no changes committed",
                        description="Mapping " + str(mapping_id) + ": " + exc.title,
                    ) from None
                results.append({"id": mapping_id, "changed": changed})
        resp.media = {"items": results, "business_activation": False}


class Control:
    on_options = staticmethod(Workbench.on_options)

    @staticmethod
    def on_post(req, resp, id_, collection, item_id):
        admin_control(req)
        user_id = actor(req)
        source_id, item_id = positive(id_), positive(item_id)
        body = req.media
        if not isinstance(body, dict) or set(body) != {"action"}:
            raise falcon.HTTPBadRequest(title="Expected only an action field")
        action = body["action"]
        allowed = {"tasks": ("enable", "pause"), "runs": ("cancel",), "alerts": ("acknowledge",),
                   "business": ("retry_business",)}
        if collection not in allowed or action not in allowed[collection]:
            raise falcon.HTTPBadRequest(title="Unsupported acquisition action")
        with database(source_id, write=True,
                      extra_tables=("tbl_jishiyun_business_jobs",) if collection == "business" else ()) as cursor:
            if collection == "business":
                cursor.execute(
                    "SELECT status FROM tbl_jishiyun_business_jobs WHERE id=%s AND data_source_id=%s FOR UPDATE",
                    (item_id, source_id),
                )
                row = cursor.fetchone()
                if row is None:
                    raise falcon.HTTPNotFound(title="Business job not found in this source")
                if row[0] not in ("blocked", "error", "queued"):
                    raise falcon.HTTPConflict(title="Only blocked or failed business jobs may be retried")
                if row[0] != "queued":
                    cursor.execute(
                        "UPDATE tbl_jishiyun_business_jobs SET status='queued',next_attempt_datetime_utc=NULL,"
                        "lease_token=NULL,lease_expires_datetime_utc=NULL WHERE id=%s", (item_id,),
                    )
                    audit(cursor, source_id, user_id, "admin_business_retry", {"business_job_id": item_id})
            elif collection == "tasks":
                cursor.execute(
                    "SELECT is_enabled FROM tbl_jishiyun_tasks WHERE id=%s AND data_source_id=%s FOR UPDATE",
                    (item_id, source_id),
                )
                row = cursor.fetchone()
                if row is None:
                    raise falcon.HTTPNotFound(title="Task not found in this source")
                target = action == "enable"
                if bool(row[0]) != target:
                    cursor.execute(
                        "UPDATE tbl_jishiyun_tasks SET is_enabled=%s, updated_by_user_id=%s WHERE id=%s",
                        (target, user_id, item_id),
                    )
                    audit(cursor, source_id, user_id, "admin_task_" + action, {"task_id": item_id})
            elif collection == "runs":
                cursor.execute("SELECT status FROM tbl_jishiyun_runs WHERE id=%s AND data_source_id=%s FOR UPDATE", (item_id, source_id))
                row = cursor.fetchone()
                if row is None:
                    raise falcon.HTTPNotFound(title="Run not found in this source")
                if row[0] not in ("preview", "queued", "running", "cancelled"):
                    raise falcon.HTTPConflict(title="Completed runs cannot be cancelled")
                if row[0] != "cancelled":
                    cursor.execute(
                        "UPDATE tbl_jishiyun_runs SET status='cancelled', lease_token=NULL, "
                        "lease_expires_datetime_utc=NULL, next_attempt_datetime_utc=NULL, "
                        "finished_datetime_utc=UTC_TIMESTAMP() WHERE id=%s", (item_id,),
                    )
                    audit(cursor, source_id, user_id, "admin_run_cancelled", {"previous_status": row[0]}, run_id=item_id)
            else:
                cursor.execute(
                    "SELECT status, acknowledged_by_user_id FROM tbl_jishiyun_alerts "
                    "WHERE id=%s AND data_source_id=%s FOR UPDATE", (item_id, source_id),
                )
                row = cursor.fetchone()
                if row is None:
                    raise falcon.HTTPNotFound(title="Alert not found in this source")
                if row[0] != "active":
                    raise falcon.HTTPConflict(title="Recovered alerts cannot be acknowledged")
                if row[1] is None:
                    cursor.execute(
                        "UPDATE tbl_jishiyun_alerts SET acknowledged_by_user_id=%s, "
                        "acknowledged_datetime_utc=UTC_TIMESTAMP() WHERE id=%s", (user_id, item_id),
                    )
                    audit(cursor, source_id, user_id, "admin_alert_acknowledged", {}, alert_id=item_id)
        resp.media = {"action": action, "id": item_id, "data_source_id": source_id}


@contextmanager
def acquisition_queue(source_id):
    def connect():
        if config.myems_system_db.get("database") != "myems_system_db":
            raise PersistenceError("Acquisition requires the standard system schema")
        return mysql.connector.connect(**dict(
            config.myems_system_db, autocommit=False, buffered=True, use_pure=True, connection_timeout=10,
        ))

    try:
        yield RunQueue(connect, source_id)
    except ContractError as exc:
        raise falcon.HTTPBadRequest(title="Invalid acquisition request", description=str(exc)) from None
    except PersistenceError as exc:
        logger.warning("Jishiyun acquisition request conflicts with stored state")
        raise falcon.HTTPConflict(title="Acquisition state conflict", description=str(exc)) from None
    except mysql.connector.Error:
        logger.error("Jishiyun acquisition database operation failed")
        raise falcon.HTTPInternalServerError(
            title="Acquisition operation failed", description="Check database schema and API service health",
        ) from None


@contextmanager
def initialization(source_id):
    with acquisition_queue(source_id) as queue:
        yield ArchiveInitializer(queue)


def backfill_snapshot(cursor, source_id, batch_id):
    cursor.execute(
        "SELECT request_key,task_id,requested_by_user_id,window_start_utc,window_end_utc "
        "FROM tbl_jishiyun_backfill_batches WHERE id=%s AND data_source_id=%s", (batch_id, source_id),
    )
    row = cursor.fetchone()
    if row is None:
        raise falcon.HTTPNotFound(title="Backfill batch not found in this source")
    return row


class BackfillSubmit:
    on_options = staticmethod(Workbench.on_options)

    @staticmethod
    def on_post(req, resp, id_):
        admin_control(req)
        user_id, source_id = actor(req), positive(id_)
        body = initialization_body(req, ("task_id", "request_key", "window_start_utc", "window_end_utc"))
        task_id = positive(body["task_id"], "backfill task")
        if not isinstance(body["request_key"], str):
            raise falcon.HTTPBadRequest(title="Request key must be a UUID string")
        try:
            request_key = str(UUID(body["request_key"]))
        except ValueError:
            raise falcon.HTTPBadRequest(title="Request key must be a UUID string") from None
        start = collection_time(body["window_start_utc"], required=True).replace(tzinfo=timezone.utc)
        end = collection_time(body["window_end_utc"], required=True).replace(tzinfo=timezone.utc)
        try:
            split_backfill(start, end)
        except ContractError as exc:
            raise falcon.HTTPBadRequest(title="Invalid backfill window", description=str(exc)) from None
        with acquisition_queue(source_id) as queue:
            # Children commit individually. Retrying this same UUID resumes a visible prefix.
            try:
                tuple(enqueue_backfill(queue, task_id, request_key, start, end, user_id))
            except ContractError as exc:
                # Scope validation may fail after a committed parent/child prefix.
                raise PersistenceError(str(exc)) from None
            resp.media = batch_status(queue, request_key)
        resp.status = falcon.HTTP_202


class BackfillDetail:
    on_options = staticmethod(Workbench.on_options)

    @staticmethod
    def on_get(req, resp, id_, item_id):
        admin_control(req)
        source_id, batch_id = positive(id_), positive(item_id)
        with database(source_id) as cursor:
            row = backfill_snapshot(cursor, source_id, batch_id)
        with acquisition_queue(source_id) as queue:
            resp.media = batch_status(queue, row[0])
        resp.media.update({"window_start_utc": serialize(row[3]), "window_end_utc": serialize(row[4]),
                           "requested_by_user_id": row[2]})


class BackfillControl:
    on_options = staticmethod(Workbench.on_options)

    @staticmethod
    def on_post(req, resp, id_, item_id, operation):
        admin_control(req)
        user_id, source_id, batch_id = actor(req), positive(id_), positive(item_id)
        initialization_body(req, ())
        if operation not in ("resume", "cancel"):
            raise falcon.HTTPBadRequest(title="Unsupported backfill operation")
        with database(source_id) as cursor:
            row = backfill_snapshot(cursor, source_id, batch_id)
        if operation == "resume" and row[2] != user_id:
            raise falcon.HTTPConflict(title="Only the original submitting administrator can resume this batch")
        with acquisition_queue(source_id) as queue:
            if operation == "resume":
                start = collection_time(serialize(row[3]) + "Z", required=True).replace(tzinfo=timezone.utc)
                end = collection_time(serialize(row[4]) + "Z", required=True).replace(tzinfo=timezone.utc)
                tuple(enqueue_backfill(queue, row[1], row[0], start, end, user_id))
            resp.media = batch_status(queue, row[0], **({"cancel_by": user_id} if operation == "cancel" else {}))
        resp.status = falcon.HTTP_202 if operation == "resume" else falcon.HTTP_200


def initialization_body(req, fields):
    if req.content_length is not None and req.content_length > 262144:
        raise falcon.HTTPContentTooLarge(title="Initialization request exceeds 256 KiB")
    body = req.media
    if not isinstance(body, dict) or set(body) != set(fields):
        raise falcon.HTTPBadRequest(title="Invalid initialization request fields")
    return body


class InitializationFetch:
    on_options = staticmethod(Workbench.on_options)

    @staticmethod
    def on_post(req, resp, id_):
        admin_control(req)
        user_id, source_id = actor(req), positive(id_)
        body = initialization_body(req, ("task_id", "request_key"))
        task_id = positive(body["task_id"], "initialization task")
        if not isinstance(body["request_key"], str):
            raise falcon.HTTPBadRequest(title="Request key must be a UUID string")
        with initialization(source_id) as service:
            run_id = service.queue.enqueue_archive(task_id, body["request_key"], user_id)
        resp.status = falcon.HTTP_202
        resp.media = {"run_id": run_id}


class InitializationPreview:
    on_options = staticmethod(Workbench.on_options)

    @staticmethod
    def on_get(req, resp, id_, run_id):
        admin_control(req)
        source_id, run_id = positive(id_), positive(run_id)
        limit = positive(req.get_param("limit") or "50", "page limit")
        before = req.get_param("before_id")
        with initialization(source_id) as service:
            resp.media = service.preview_page(
                run_id, limit=limit, before_id=positive(before, "page cursor") if before is not None else None,
            )


class InitializationApproval:
    on_options = staticmethod(Workbench.on_options)

    @staticmethod
    def on_post(req, resp, id_, run_id):
        admin_control(req)
        user_id, source_id, run_id = actor(req), positive(id_), positive(run_id)
        body = initialization_body(req, ("items",))
        items = body["items"]
        if not isinstance(items, list) or not 1 <= len(items) <= 200:
            raise falcon.HTTPBadRequest(title="Approve between 1 and 200 items")
        with initialization(source_id) as service:
            approved = []
            for item in items:
                if not isinstance(item, dict) or set(item) != {"address", "configuration"}:
                    raise falcon.HTTPBadRequest(title="Invalid approval item fields")
                approved.append((item["address"], configuration_from_json(json.dumps(item["configuration"]))))
            service.approve_many(run_id, tuple(approved), user_id)
        resp.media = {"run_id": run_id, "approved_count": len(approved)}


class InitializationSubmit:
    on_options = staticmethod(Workbench.on_options)

    @staticmethod
    def on_post(req, resp, id_, run_id):
        admin_control(req)
        user_id, source_id, run_id = actor(req), positive(id_), positive(run_id)
        body = initialization_body(req, ("addresses",))
        addresses = body["addresses"]
        if not isinstance(addresses, list) or not 1 <= len(addresses) <= 200:
            raise falcon.HTTPBadRequest(title="Select between 1 and 200 approved addresses")
        if any(not isinstance(address, str) for address in addresses):
            raise falcon.HTTPBadRequest(title="Addresses must be strings")
        with initialization(source_id) as service:
            service.submit_approved(run_id, tuple(addresses), user_id)
        resp.status = falcon.HTTP_202
        resp.media = {"run_id": run_id}
