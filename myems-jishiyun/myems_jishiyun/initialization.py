"""Approved archive initialization transactions, separate from HTTP and energy jobs."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import timezone
from decimal import Decimal
from typing import Literal, cast
from uuid import uuid4

from myems_jishiyun.contracts import ContractError, MeterArchive, _decimal, parse_archives
from myems_jishiyun.execution import ArchiveOutcome, ClaimedRun, RunQueue, _settings_from_task, _uuid
from myems_jishiyun.persistence import Cursor, PersistenceError, _address, _db_time, _json, _positive_id


PENDING_OBJECT_TYPE = "JISHIYUN_REGISTER"


class InitializationConflict(PersistenceError):
    """Requires explicit operator reconciliation; never name-based auto-binding."""


@dataclass(frozen=True)
class MeterConfiguration:
    name: str
    energy_category_id: int
    cost_center_id: int
    is_counted: bool
    hourly_low_limit: Decimal
    hourly_high_limit: Decimal
    units: str
    point_high_limit: Decimal

    def serialized(self) -> str:
        if not self.name.strip() or self.name != self.name.strip() or len(self.name) > 255:
            raise ContractError("Meter name must contain 1 to 255 characters without surrounding whitespace")
        _positive_id(self.energy_category_id, "Energy category ID")
        _positive_id(self.cost_center_id, "Cost center ID")
        if type(self.is_counted) is not bool:
            raise ContractError("Meter is_counted must be boolean")
        if not self.units.strip() or self.units != self.units.strip() or len(self.units) > 32:
            raise ContractError("Units must contain 1 to 32 characters without surrounding whitespace")
        low = _decimal(self.hourly_low_limit, "Hourly low limit")
        high = _decimal(self.hourly_high_limit, "Hourly high limit")
        point_high = _decimal(self.point_high_limit, "Point high limit")
        if high < low or point_high <= 0:
            raise ContractError("Limits must be increasing and register high limit positive")
        return _json({
            "name": self.name, "energy_category_id": self.energy_category_id,
            "cost_center_id": self.cost_center_id, "is_counted": self.is_counted,
            "hourly_low_limit": low, "hourly_high_limit": high,
            "units": self.units, "point_high_limit": point_high,
        })


def configuration_from_json(body: object) -> MeterConfiguration:
    if not isinstance(body, str):
        raise PersistenceError("Approved configuration must be JSON text")
    try:
        parsed = json.loads(body)
    except ValueError:
        raise PersistenceError("Approved configuration is invalid JSON") from None
    if not isinstance(parsed, dict):
        raise PersistenceError("Approved configuration must be an object")
    data = cast(dict[str, object], parsed)
    if set(data) != {
        "name", "energy_category_id", "cost_center_id", "is_counted",
        "hourly_low_limit", "hourly_high_limit", "units", "point_high_limit",
    }:
        raise PersistenceError("Approved configuration has unsupported fields")
    if not isinstance(data["name"], str) or not isinstance(data["units"], str) or type(data["is_counted"]) is not bool:
        raise PersistenceError("Approved configuration has invalid text or boolean fields")
    result = MeterConfiguration(
        data["name"], _positive_id(data["energy_category_id"], "Energy category ID"),
        _positive_id(data["cost_center_id"], "Cost center ID"), data["is_counted"],
        _decimal(data["hourly_low_limit"], "Hourly low limit"),
        _decimal(data["hourly_high_limit"], "Hourly high limit"), data["units"],
        _decimal(data["point_high_limit"], "Point high limit"),
    )
    result.serialized()
    return result


class ArchiveInitializer:
    def __init__(self, queue: RunQueue) -> None:
        self.queue = queue

    @staticmethod
    def _engines(cursor: Cursor) -> None:
        names = (
            "tbl_jishiyun_run_items", "tbl_jishiyun_meter_mappings", "tbl_meters", "tbl_points",
            "tbl_meters_points", "tbl_energy_categories", "tbl_cost_centers",
        )
        cursor.execute(
            "SELECT TABLE_NAME, ENGINE FROM information_schema.TABLES WHERE TABLE_SCHEMA = 'myems_system_db' "
            "AND TABLE_NAME IN (" + ", ".join(["%s"] * len(names)) + ")", names,
        )
        rows = cursor.fetchall()
        if len(rows) != len(names) or {row[0] for row in rows} != set(names) or any(row[1] != "InnoDB" for row in rows):
            raise PersistenceError("Archive initialization requires all participating tables using InnoDB")

    def _run(self, cursor: Cursor, run_id: int, lease: ClaimedRun | None = None) -> None:
        _positive_id(run_id, "Initialization run ID")
        cursor.execute(
            "SELECT r.status, t.task_type, r.lease_token, r.lease_expires_datetime_utc, UTC_TIMESTAMP() "
            "FROM myems_system_db.tbl_jishiyun_runs r "
            "JOIN myems_system_db.tbl_jishiyun_tasks t ON t.id = r.task_id AND t.data_source_id = r.data_source_id "
            "WHERE r.id = %s AND r.data_source_id = %s FOR UPDATE", (run_id, self.queue.source_id),
        )
        row = cursor.fetchone()
        if lease is not None:
            if (
                lease.id != run_id or lease.source_id != self.queue.source_id
                or row is None or row[0] != "running" or row[1] != "meter_initialize"
                or row[2] != lease.token or _db_time(row[3]) <= _db_time(row[4])
            ):
                raise PersistenceError("Initialization lease is expired, cancelled or replaced")
            return
        if row is None or row[0] != "preview" or row[1] != "meter_initialize":
            raise PersistenceError("An active meter_initialize preview run is required")

    def progress(self, run_id: int, *, after_id: int = 0, limit: int = 100) -> dict[str, object]:
        _positive_id(run_id, "Initialization run ID")
        if type(after_id) is not int or after_id < 0 or type(limit) is not int or not 1 <= limit <= 200:
            raise ContractError("Item cursor must be nonnegative and page size between 1 and 200")
        with self.queue.transaction() as cursor:
            self._engines(cursor)
            cursor.execute(
                "SELECT r.status, r.parameters_json, r.error_code, r.expected_count, r.valid_count "
                "FROM myems_system_db.tbl_jishiyun_runs r "
                "JOIN myems_system_db.tbl_jishiyun_tasks t ON t.id = r.task_id AND t.data_source_id = r.data_source_id "
                "WHERE r.id = %s AND r.data_source_id = %s AND t.task_type = 'meter_initialize'",
                (run_id, self.queue.source_id),
            )
            row = cursor.fetchone()
            if row is None:
                raise PersistenceError("Initialization run does not exist for this source")
            cursor.execute(
                "SELECT status, COUNT(*) FROM myems_system_db.tbl_jishiyun_run_items WHERE run_id = %s GROUP BY status",
                (run_id,),
            )
            statuses = {str(item[0]): item[1] for item in cursor.fetchall()}
            cursor.execute(
                "SELECT id, external_address, external_id, status, meter_id, point_id, error_message "
                "FROM myems_system_db.tbl_jishiyun_run_items WHERE run_id = %s AND id > %s "
                "ORDER BY id LIMIT %s", (run_id, after_id, limit + 1),
            )
            page = cursor.fetchall()
            items: list[dict[str, object]] = [
                {
                    "item_id": item[0], "address": item[1], "external_id": item[2], "status": item[3],
                    "meter_id": item[4], "point_id": item[5], "error_message": item[6],
                }
                for item in page[:limit]
            ]
            return {
                "run_id": run_id, "status": row[0], "error_code": row[2],
                "selected_count": row[3], "successful_selected_count": row[4],
                "all_preview_item_status_counts": statuses, "items": items,
                "has_more": len(page) > limit, "next_after_id": items[-1]["item_id"] if items else None,
                "collection_activated_by_this_run": False,
            }

    def save_fetched(self, run: ClaimedRun, archives: tuple[MeterArchive, ...]) -> ArchiveOutcome:
        if run.phase != "archive_fetch":
            raise PersistenceError("An archive-fetch run is required")
        validated = self._validated(archives)
        with self.queue.transaction() as cursor:
            self._engines(cursor)
            self._run(cursor, run.id, run)
            count = self._save_preview(cursor, run.id, validated)
            cursor.execute(
                "UPDATE myems_system_db.tbl_jishiyun_runs SET status = 'preview', returned_count = %s, "
                "lease_token = NULL, lease_expires_datetime_utc = NULL, next_attempt_datetime_utc = NULL, "
                "error_code = NULL, error_message = NULL "
                "WHERE id = %s AND status = 'running' AND lease_token = %s "
                "AND lease_expires_datetime_utc > UTC_TIMESTAMP()", (count, run.id, run.token),
            )
            if cursor.rowcount != 1:
                raise PersistenceError("Archive-fetch lease expired before saving preview")
            return ArchiveOutcome("preview", run.id, archive_count=count)

    def submit_approved(self, run_id: int, addresses: tuple[str, ...], actor: int) -> None:
        actor = _positive_id(actor, "Approving user ID")
        if not addresses or len(addresses) != len(set(addresses)):
            raise ContractError("Select a nonempty distinct set of preview addresses")
        selected = sorted(_address(address) for address in addresses)
        with self.queue.transaction() as cursor:
            self._engines(cursor)
            cursor.execute(
                "SELECT status, parameters_json FROM myems_system_db.tbl_jishiyun_runs "
                "WHERE id = %s AND data_source_id = %s FOR UPDATE", (run_id, self.queue.source_id),
            )
            previous_run = cursor.fetchone()
            if previous_run is not None and previous_run[0] in ("queued", "running", "done", "partial", "error"):
                if isinstance(previous_run[1], str):
                    try:
                        previous_parameters = json.loads(previous_run[1])
                    except ValueError:
                        raise PersistenceError("Stored initialization submission is invalid") from None
                    if isinstance(previous_parameters, dict):
                        previous_parameters = cast(dict[str, object], previous_parameters)
                        if (
                            previous_parameters.get("phase") == "archive_initialize"
                            and previous_parameters.get("selected_addresses") == selected
                            and previous_parameters.get("approved_by_user_id") == actor
                        ):
                            return
                raise PersistenceError("Run is not a preview or an identical initialization submission")
            self._run(cursor, run_id)
            cursor.execute(
                "SELECT t.is_enabled, t.provider_utc_offset_minutes, t.parameters_json "
                "FROM myems_system_db.tbl_jishiyun_tasks t "
                "JOIN myems_system_db.tbl_jishiyun_runs r ON r.task_id = t.id AND r.data_source_id = t.data_source_id "
                "WHERE r.id = %s AND r.data_source_id = %s FOR UPDATE", (run_id, self.queue.source_id),
            )
            task = cursor.fetchone()
            if task is None or task[0] != 1:
                raise PersistenceError("Initialization task must remain enabled for submission")
            offset, encoding = _settings_from_task(task[1], task[2])
            cursor.execute(
                "SELECT external_address, status, approved_configuration_json "
                "FROM myems_system_db.tbl_jishiyun_run_items WHERE run_id = %s FOR UPDATE", (run_id,),
            )
            items = {str(row[0]): row for row in cursor.fetchall()}
            for address in selected:
                item = items.get(address)
                if item is None or item[1] not in ("pending", "created", "reused"):
                    raise PersistenceError("Every selected archive must be approved before submission")
                self._references(cursor, configuration_from_json(item[2]))
            cursor.execute(
                "UPDATE myems_system_db.tbl_jishiyun_runs SET status = 'queued', parameters_json = %s, "
                "attempt_count = 0, next_attempt_datetime_utc = NULL, finished_datetime_utc = NULL, "
                "expected_count = %s, valid_count = 0, inserted_count = 0, duplicate_count = 0, invalid_count = 0, "
                "error_code = NULL, error_message = NULL WHERE id = %s",
                (_json({
                    "phase": "archive_initialize", "provider_utc_offset_minutes": offset, "encoding": encoding,
                    "selected_addresses": selected, "approved_by_user_id": actor,
                }), len(selected), run_id),
            )
            self.queue.event(cursor, run_id, "approved_initialization_queued", {
                "selected_addresses": selected, "actor_user_id": actor,
            })

    def execute_approved(self, run: ClaimedRun) -> ArchiveOutcome:
        if run.phase != "archive_initialize":
            raise PersistenceError("An approved initialization run is required")
        with self.queue.transaction() as cursor:
            self._engines(cursor)
            self._run(cursor, run.id, run)
            cursor.execute(
                "SELECT parameters_json FROM myems_system_db.tbl_jishiyun_runs WHERE id = %s", (run.id,),
            )
            row = cursor.fetchone()
            if row is None or not isinstance(row[0], str):
                raise PersistenceError("Initialization run parameters are missing")
            try:
                value = json.loads(row[0])
            except ValueError:
                raise PersistenceError("Initialization run parameters are invalid") from None
            if not isinstance(value, dict):
                raise PersistenceError("Initialization parameters must be an object")
            parameters = cast(dict[str, object], value)
            actor = _positive_id(parameters.get("approved_by_user_id"), "Approving user ID")
            raw_addresses = parameters.get("selected_addresses")
            if not isinstance(raw_addresses, list):
                raise PersistenceError("Initialization selection must be an address list")
            addresses = tuple(_address(address) for address in cast(list[object], raw_addresses))
            if not addresses or len(set(addresses)) != len(addresses):
                raise PersistenceError("Initialization selection must be distinct and nonempty")
        counts = {"created": 0, "reused": 0, "conflict": 0, "error": 0}
        for address in addresses:
            self.queue.renew(run)
            with self.queue.transaction() as cursor:
                self._run(cursor, run.id, run)
                cursor.execute(
                    "SELECT status FROM myems_system_db.tbl_jishiyun_run_items "
                    "WHERE run_id = %s AND external_address = %s", (run.id, address),
                )
                previous = cursor.fetchone()
                if previous is not None and previous[0] in ("conflict", "error"):
                    counts[str(previous[0])] += 1
                    continue
            try:
                outcome = self.initialize(run.id, address, actor, lease=run)
            except (InitializationConflict, ContractError, PersistenceError) as exc:
                outcome = "conflict" if isinstance(exc, InitializationConflict) else "error"
                self._item_failed(run, address, outcome, actor)
            except Exception:
                self._item_failed(run, address, "error", actor)
                raise
            counts[outcome] += 1
        with self.queue.transaction() as cursor:
            self._engines(cursor)
            self._run(cursor, run.id, run)
            status = "partial" if counts["conflict"] or counts["error"] else "done"
            self.queue.event(cursor, run.id, "initialization_finished", {
                "selected_count": len(addresses), **counts, "actor_user_id": actor,
            })
            cursor.execute(
                "UPDATE myems_system_db.tbl_jishiyun_runs SET status = %s, expected_count = %s, "
                "returned_count = %s, valid_count = %s, inserted_count = %s, duplicate_count = %s, invalid_count = %s, "
                "lease_token = NULL, lease_expires_datetime_utc = NULL, finished_datetime_utc = UTC_TIMESTAMP() "
                "WHERE id = %s AND status = 'running' AND lease_token = %s "
                "AND lease_expires_datetime_utc > UTC_TIMESTAMP()",
                (status, len(addresses), len(addresses), counts["created"] + counts["reused"],
                 counts["created"], counts["reused"], counts["conflict"] + counts["error"], run.id, run.token),
            )
            if cursor.rowcount != 1:
                raise PersistenceError("Initialization lease expired before finalization")
        return ArchiveOutcome(status, run.id, created_count=counts["created"], reused_count=counts["reused"],
                              conflict_count=counts["conflict"], error_count=counts["error"])

    def _item_failed(
        self, run: ClaimedRun, address: str, status: Literal["conflict", "error"], actor: int,
    ) -> None:
        with self.queue.transaction() as cursor:
            self._engines(cursor)
            self._run(cursor, run.id, run)
            cursor.execute(
                "UPDATE myems_system_db.tbl_jishiyun_run_items SET status = %s, error_message = %s "
                "WHERE run_id = %s AND external_address = %s",
                (status, "Initialization requires manual reconciliation" if status == "conflict"
                 else "Initialization failed; review configuration and service health", run.id, address),
            )
            if cursor.rowcount != 1:
                raise PersistenceError("Initialization result item is missing or unchanged")
            self.queue.event(cursor, run.id, "initialization_item_failed", {
                "address": address, "status": status, "actor_user_id": actor,
            })
            self._run(cursor, run.id, run)

    @staticmethod
    def _validated(archives: tuple[MeterArchive, ...]) -> tuple[MeterArchive, ...]:
        # Revalidate the complete set before any inserts, including both identity keys.
        rows = [archive.raw for archive in archives]
        validated = parse_archives(_json({
            "IsSuccess": True, "Code": "0000", "Data": rows, "Count": len(rows),
        }), timezone.utc)
        for archive in validated:
            _address(archive.address)
        return validated

    def create_preview(
        self, task_id: int, request_key: str, archives: tuple[MeterArchive, ...], actor: int,
    ) -> int:
        task_id, actor = _positive_id(task_id, "Initialization task ID"), _positive_id(actor, "Requesting user ID")
        request_key = _uuid(request_key)
        validated = self._validated(archives)
        with self.queue.transaction() as cursor:
            self._engines(cursor)
            cursor.execute(
                "SELECT task_type, is_enabled FROM myems_system_db.tbl_jishiyun_tasks "
                "WHERE id = %s AND data_source_id = %s FOR UPDATE", (task_id, self.queue.source_id),
            )
            task = cursor.fetchone()
            if task is None or task[0] != "meter_initialize" or task[1] != 1:
                raise PersistenceError("An enabled initialization task for this source is required")
            cursor.execute(
                "SELECT id, data_source_id, task_id, requested_by_user_id "
                "FROM myems_system_db.tbl_jishiyun_runs WHERE request_key = %s FOR UPDATE", (request_key,),
            )
            existing = cursor.fetchone()
            if existing is not None:
                if existing[1:] != (self.queue.source_id, task_id, actor):
                    raise InitializationConflict("Preview request identifier belongs to another submission")
                run_id = _positive_id(existing[0], "Preview run ID")
                self._run(cursor, run_id)
                self._save_preview(cursor, run_id, validated, require_same=True)
                return run_id
            cursor.execute(
                "INSERT INTO myems_system_db.tbl_jishiyun_runs "
                "(task_id, data_source_id, request_key, status, scope_snapshot_json, parameters_json, requested_by_user_id) "
                "VALUES (%s, %s, %s, 'preview', %s, %s, %s)",
                (task_id, self.queue.source_id, request_key, '{"version":1,"meters":[]}', '{"kind":"archive_preview"}', actor),
            )
            run_id = _positive_id(cursor.lastrowid, "Preview run ID")
            self._save_preview(cursor, run_id, validated)
            return run_id

    def preview(self, run_id: int, archives: tuple[MeterArchive, ...]) -> int:
        validated = self._validated(archives)
        with self.queue.transaction() as cursor:
            self._engines(cursor)
            self._run(cursor, run_id)
            return self._save_preview(cursor, run_id, validated)

    def _save_preview(
        self, cursor: Cursor, run_id: int, validated: tuple[MeterArchive, ...], *, require_same: bool = False,
    ) -> int:
        cursor.execute(
            "SELECT external_address, external_id, archive_json FROM myems_system_db.tbl_jishiyun_run_items "
            "WHERE run_id = %s ORDER BY external_address FOR UPDATE", (run_id,),
        )
        existing = cursor.fetchall()
        cursor.execute(
            "SELECT id FROM myems_system_db.tbl_jishiyun_events "
            "WHERE run_id = %s AND data_source_id = %s AND event_type = 'archive_preview_saved' LIMIT 1",
            (run_id, self.queue.source_id),
        )
        already_saved = cursor.fetchone() is not None
        records = sorted((archive.address, archive.external_id, _json(archive.raw)) for archive in validated)
        if existing or require_same or already_saved:
            if existing != records:
                raise InitializationConflict("Preview is immutable; request a new preview for changed archives")
            return len(existing)
        for address, external_id, raw in records:
            cursor.execute(
                "INSERT INTO myems_system_db.tbl_jishiyun_run_items "
                "(run_id, external_address, external_id, archive_json, status) VALUES (%s, %s, %s, %s, 'preview')",
                (run_id, address, external_id, raw),
            )
        self.queue.event(cursor, run_id, "archive_preview_saved", {"archive_count": len(records)})
        return len(records)

    def approve(self, run_id: int, address: str, config: MeterConfiguration, actor: int) -> None:
        self.approve_many(run_id, ((address, config),), actor)

    def approve_many(
        self, run_id: int, configurations: tuple[tuple[str, MeterConfiguration], ...], actor: int,
    ) -> None:
        actor = _positive_id(actor, "Approving user ID")
        if not 1 <= len(configurations) <= 200:
            raise ContractError("Approve between 1 and 200 preview items per request")
        validated = [(_address(address), config, config.serialized()) for address, config in configurations]
        if len({item[0] for item in validated}) != len(validated):
            raise ContractError("Approval addresses must be distinct")
        with self.queue.transaction() as cursor:
            self._engines(cursor)
            self._run(cursor, run_id)
            for address, config, body in sorted(validated, key=lambda item: item[0]):
                self._references(cursor, config)
                cursor.execute(
                    "SELECT id, status, approved_configuration_json FROM myems_system_db.tbl_jishiyun_run_items "
                    "WHERE run_id = %s AND external_address = %s FOR UPDATE", (run_id, address),
                )
                row = cursor.fetchone()
                if row is None or row[1] not in ("preview", "pending"):
                    raise PersistenceError("Only preview or pending items can be approved")
                if row[1] == "pending" and row[2] == body:
                    continue
                cursor.execute(
                    "UPDATE myems_system_db.tbl_jishiyun_run_items SET approved_configuration_json = %s, "
                    "status = 'pending', error_message = NULL WHERE id = %s", (body, row[0]),
                )
                self.queue.event(cursor, run_id, "archive_item_approved", {
                    "address": address, "actor_user_id": actor, "configuration": json.loads(body),
                })

    def preview_page(self, run_id: int, *, before_id: int | None = None, limit: int = 50) -> dict[str, object]:
        _positive_id(run_id, "Initialization run ID")
        if type(limit) is not int or not 1 <= limit <= 200:
            raise ContractError("Page size must be between 1 and 200")
        if before_id is not None:
            _positive_id(before_id, "Preview cursor")
        with self.queue.transaction() as cursor:
            self._engines(cursor)
            cursor.execute(
                "SELECT r.status FROM myems_system_db.tbl_jishiyun_runs r "
                "JOIN myems_system_db.tbl_jishiyun_tasks t ON t.id = r.task_id AND t.data_source_id = r.data_source_id "
                "WHERE r.id = %s AND r.data_source_id = %s AND t.task_type = 'meter_initialize'",
                (run_id, self.queue.source_id),
            )
            run = cursor.fetchone()
            if run is None:
                raise PersistenceError("Initialization run does not exist for this source")
            query = (
                "SELECT i.id, i.external_address, i.external_id, i.status, i.archive_json, "
                "i.approved_configuration_json, m.meter_id, m.point_id "
                "FROM myems_system_db.tbl_jishiyun_run_items i "
                "LEFT JOIN myems_system_db.tbl_jishiyun_meter_mappings m "
                "ON m.data_source_id = %s AND m.external_address = i.external_address WHERE i.run_id = %s"
            )
            params: list[object] = [self.queue.source_id, run_id]
            if before_id is not None:
                query += " AND i.id < %s"
                params.append(before_id)
            cursor.execute(query + " ORDER BY i.id DESC LIMIT %s", tuple(params + [limit + 1]))
            rows = cursor.fetchall()
            items: list[dict[str, object]] = []
            for row in rows[:limit]:
                try:
                    archive = json.loads(str(row[4]))
                except ValueError:
                    raise PersistenceError("Stored archive preview is invalid") from None
                if not isinstance(archive, dict):
                    raise PersistenceError("Stored archive preview must be an object")
                archive = cast(dict[str, object], archive)
                items.append({
                    "id": row[0], "external_address": row[1], "external_id": row[2], "status": row[3],
                    "name": archive.get("AmmeterName"), "area": archive.get("AreaName"),
                    "configuration": json.loads(configuration_from_json(row[5]).serialized()) if row[5] else None,
                    "existing_meter_id": row[6], "existing_point_id": row[7],
                })
            return {
                "run_id": run_id, "status": run[0], "items": items, "has_more": len(rows) > limit,
                "next_before_id": items[-1]["id"] if items else None,
            }

    @staticmethod
    def _references(cursor: Cursor, config: MeterConfiguration) -> None:
        for table, id_ in (
            ("tbl_energy_categories", config.energy_category_id), ("tbl_cost_centers", config.cost_center_id),
        ):
            cursor.execute(f"SELECT id FROM myems_system_db.{table} WHERE id = %s FOR UPDATE", (id_,))
            if cursor.fetchone() is None:
                raise PersistenceError("Approved energy category or cost center does not exist")

    def initialize(
        self, run_id: int, address: str, actor: int, *, lease: ClaimedRun | None = None,
    ) -> Literal["created", "reused"]:
        if lease is not None and lease.phase != "archive_initialize":
            raise PersistenceError("Only an approved initialization lease can create business objects")
        address, actor = _address(address), _positive_id(actor, "Executing user ID")
        with self.queue.transaction() as cursor:
            self._engines(cursor)
            self._run(cursor, run_id, lease)
            cursor.execute(
                "SELECT id, external_id, archive_json, approved_configuration_json, status, meter_id, point_id "
                "FROM myems_system_db.tbl_jishiyun_run_items WHERE run_id = %s AND external_address = %s FOR UPDATE",
                (run_id, address),
            )
            item = cursor.fetchone()
            if item is None or item[4] not in ("pending", "created", "reused"):
                raise PersistenceError("Item must have approved configuration before initialization")
            config = configuration_from_json(item[3])
            self._references(cursor, config)
            cursor.execute(
                "SELECT id, external_address, external_id, meter_id, point_id "
                "FROM myems_system_db.tbl_jishiyun_meter_mappings "
                "WHERE data_source_id = %s AND (external_address = %s OR external_id = %s) FOR UPDATE",
                (self.queue.source_id, address, item[1]),
            )
            mappings = cursor.fetchall()
            outcome: Literal["created", "reused"]
            if mappings:
                if len(mappings) != 1 or mappings[0][1] != address or mappings[0][2] != item[1]:
                    raise InitializationConflict("External address or ID conflicts with an existing mapping")
                meter_id, point_id = mappings[0][3], mappings[0][4]
                cursor.execute(
                    "SELECT mp.id FROM myems_system_db.tbl_meters_points mp "
                    "JOIN myems_system_db.tbl_meters m ON m.id = mp.meter_id "
                    "JOIN myems_system_db.tbl_points p ON p.id = mp.point_id "
                    "WHERE mp.meter_id = %s AND mp.point_id = %s AND p.data_source_id = %s FOR UPDATE",
                    (meter_id, point_id, self.queue.source_id),
                )
                if len(cursor.fetchall()) != 1:
                    raise InitializationConflict("Existing mapping has broken or duplicate local associations")
                cursor.execute(
                    "SELECT object_type, address FROM myems_system_db.tbl_points WHERE id = %s FOR UPDATE", (point_id,),
                )
                local_point = cursor.fetchone()
                if local_point is None or local_point[0] not in (PENDING_OBJECT_TYPE, "ENERGY_VALUE"):
                    raise InitializationConflict("Mapped point has an incompatible object type")
                try:
                    point_address = json.loads(str(local_point[1]))
                except ValueError:
                    raise InitializationConflict("Mapped point address is not valid JSON") from None
                if point_address != {"provider": "jishiyun", "address": address, "field": "ZYGDNSZ"}:
                    raise InitializationConflict("Mapped point no longer belongs to this external register")
                if item[4] in ("created", "reused"):
                    if item[5:7] != (meter_id, point_id):
                        raise InitializationConflict("Completed item's local identities no longer match")
                    return cast(Literal["created", "reused"], item[4])
                outcome = "reused"
            else:
                if item[4] != "pending":
                    raise InitializationConflict("Completed item's mapping was removed; refusing automatic recreation")
                cursor.execute("SELECT id FROM myems_system_db.tbl_meters WHERE name = %s FOR UPDATE", (config.name,))
                if cursor.fetchone() is not None:
                    raise InitializationConflict("Meter name already exists; explicit reconciliation required")
                cursor.execute(
                    "SELECT id FROM myems_system_db.tbl_points WHERE data_source_id = %s AND name = %s FOR UPDATE",
                    (self.queue.source_id, config.name + " ZYGDNSZ"),
                )
                if cursor.fetchone() is not None:
                    raise InitializationConflict("Point name already exists; explicit reconciliation required")
                cursor.execute(
                    "INSERT INTO myems_system_db.tbl_meters "
                    "(name, uuid, energy_category_id, is_counted, hourly_low_limit, hourly_high_limit, cost_center_id) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                    (config.name, str(uuid4()), config.energy_category_id, config.is_counted,
                     config.hourly_low_limit, config.hourly_high_limit, config.cost_center_id),
                )
                meter_id = _positive_id(cursor.lastrowid, "New meter ID")
                cursor.execute(
                    "INSERT INTO myems_system_db.tbl_points "
                    "(name, data_source_id, object_type, units, high_limit, low_limit, ratio, offset_constant, "
                    "is_trend, is_virtual, address, faults, definitions) "
                    "VALUES (%s, %s, %s, %s, %s, 0, 1, 0, TRUE, FALSE, %s, %s, %s)",
                    (config.name + " ZYGDNSZ", self.queue.source_id, PENDING_OBJECT_TYPE, config.units,
                     config.point_high_limit, _json({"provider": "jishiyun", "address": address, "field": "ZYGDNSZ"}),
                     "[]", "{}"),
                )
                point_id = _positive_id(cursor.lastrowid, "New point ID")
                cursor.execute(
                    "INSERT INTO myems_system_db.tbl_meters_points (meter_id, point_id) VALUES (%s, %s)",
                    (meter_id, point_id),
                )
                cursor.execute(
                    "INSERT INTO myems_system_db.tbl_jishiyun_meter_mappings "
                    "(data_source_id, external_id, external_address, meter_id, point_id, initialization_run_id, "
                    "archive_json, configuration_status, is_enabled, last_archive_sync_datetime_utc) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, 'pending', FALSE, UTC_TIMESTAMP())",
                    (self.queue.source_id, item[1], address, meter_id, point_id, run_id, item[2]),
                )
                outcome = "created"
            cursor.execute(
                "UPDATE myems_system_db.tbl_jishiyun_run_items SET status = %s, meter_id = %s, point_id = %s, "
                "error_message = NULL WHERE id = %s", (outcome, meter_id, point_id, item[0]),
            )
            self.queue.event(cursor, run_id, "archive_item_initialized", {
                "address": address, "actor_user_id": actor, "outcome": outcome,
                "meter_id": meter_id, "point_id": point_id, "existing_configuration_overwritten": False,
                "new_mapping_defaults_disabled": outcome == "created",
            })
            self._run(cursor, run_id, lease)
            return outcome
