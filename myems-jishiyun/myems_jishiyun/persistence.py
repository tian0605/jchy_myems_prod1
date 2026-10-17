"""Transactional raw-result persistence; no writes to legacy energy tables."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Literal, Protocol, cast
from uuid import UUID

from myems_jishiyun.contracts import DATE_TYPE, INTERVAL, VALUE_TYPE, ContractError, EnergyReading, ReadingKey


Quality = Literal["valid", "invalid", "conflict"]
AlertType = Literal["missing", "invalid", "conflict", "identity_mismatch"]
Parameters = tuple[object, ...]


class Cursor(Protocol):
    @property
    def rowcount(self) -> int: ...

    @property
    def lastrowid(self) -> int | None: ...

    def execute(self, operation: str, params: Parameters = ()) -> object: ...
    def fetchone(self) -> tuple[object, ...] | None: ...
    def fetchall(self) -> list[tuple[object, ...]]: ...
    def close(self) -> None: ...


class Connection(Protocol):
    """Caller supplies a dedicated, idle MySQL connection with both schema grants."""

    def start_transaction(self) -> None: ...
    def cursor(self) -> Cursor: ...
    def commit(self) -> None: ...
    def rollback(self) -> None: ...


class PersistenceError(RuntimeError):
    """Run state, lease or stored data prevents safe persistence."""


@dataclass(frozen=True)
class ScopeMeter:
    address: str
    external_id: int
    effective_start: datetime | None = None
    effective_end: datetime | None = None


@dataclass(frozen=True)
class Sample:
    address: str
    external_id: int
    timestamp_utc: datetime
    value: Decimal | None
    quality: Quality
    raw_json: str

    @property
    def key(self) -> ReadingKey:
        return self.address, self.timestamp_utc


@dataclass(frozen=True)
class Batch:
    expected: frozenset[ReadingKey]
    samples: tuple[Sample, ...]
    issues: tuple[tuple[ReadingKey, AlertType], ...]
    unexpected: tuple[EnergyReading, ...]
    duplicate_count: int

    @property
    def returned_count(self) -> int:
        return len(self.samples)

    @property
    def valid_count(self) -> int:
        return sum(sample.quality == "valid" for sample in self.samples)


@dataclass(frozen=True)
class Result:
    status: Literal["done", "partial"]
    expected_count: int
    returned_count: int
    valid_count: int
    inserted_count: int
    updated_count: int
    duplicate_count: int
    invalid_count: int
    missing_count: int
    unexpected_count: int


def _positive_id(value: object, name: str) -> int:
    if type(value) is not int or not 0 < value <= 9223372036854775807:
        raise ContractError(f"{name} must be a positive BIGINT")
    return value


positive_id = _positive_id


def _address(value: object) -> str:
    if (
        not isinstance(value, str) or not value or len(value) > 64
        or any(ord(character) < 33 or ord(character) > 126 for character in value)
    ):
        raise ContractError("Scope address must contain 1 to 64 visible ASCII characters")
    return value


def _quarter(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ContractError("Persistence timestamps must be timezone-aware")
    value = value.astimezone(timezone.utc)
    if value.minute % 15 or value.second or value.microsecond:
        raise ContractError("Persistence timestamps must align to 15 minutes")
    return value


def _json_default(value: object) -> str:
    if isinstance(value, Decimal):
        # Raw Decimal values remain exact strings; the register has a DECIMAL column.
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError("Unsupported audit value")


def _json(value: object) -> str:
    return json.dumps(_audit_value(value), default=_json_default, ensure_ascii=True, sort_keys=True, allow_nan=False)


def _audit_value(value: object) -> object:
    if isinstance(value, float) and not math.isfinite(value):
        return {"non_finite_number": str(value)}
    if isinstance(value, dict):
        values = cast(dict[str, object], value)
        return {key: _audit_value(item) for key, item in values.items()}
    if isinstance(value, (list, tuple)):
        return [_audit_value(item) for item in cast(list[object] | tuple[object, ...], value)]
    return value


def _scope_meters(scope: tuple[ScopeMeter, ...]) -> dict[str, ScopeMeter]:
    by_address: dict[str, ScopeMeter] = {}
    external_ids: set[int] = set()
    if not scope:
        raise ContractError("Configured scope must not be empty")
    for meter in scope:
        _address(meter.address)
        _positive_id(meter.external_id, "External ID")
        if meter.address in by_address or meter.external_id in external_ids:
            raise ContractError("Scope contains duplicate addresses or external IDs")
        start = _quarter(meter.effective_start) if meter.effective_start is not None else None
        end = _quarter(meter.effective_end) if meter.effective_end is not None else None
        if start is not None and end is not None and start >= end:
            raise ContractError("Scope effective range must be increasing")
        by_address[meter.address] = meter
        external_ids.add(meter.external_id)
    return by_address


def scope_snapshot(scope: tuple[ScopeMeter, ...]) -> str:
    """Versioned immutable run scope, captured from approved mappings before HTTP."""
    _scope_meters(scope)
    meters: list[dict[str, object]] = [
        {
            "address": meter.address,
            "external_id": meter.external_id,
            "effective_start_utc": _quarter(meter.effective_start) if meter.effective_start is not None else None,
            "effective_end_utc": _quarter(meter.effective_end) if meter.effective_end is not None else None,
        }
        for meter in scope
    ]
    return _json({"version": 1, "meters": meters})


def _snapshot_scope(body: object) -> tuple[ScopeMeter, ...]:
    if not isinstance(body, str):
        raise PersistenceError("Run scope snapshot must be JSON text")
    try:
        snapshot = json.loads(body)
    except (ValueError, TypeError):
        raise PersistenceError("Run scope snapshot is not valid JSON") from None
    if not isinstance(snapshot, dict):
        raise PersistenceError("Run scope snapshot must be an object")
    snapshot = cast(dict[str, object], snapshot)
    if type(snapshot.get("version")) is not int or snapshot["version"] != 1:
        raise PersistenceError("Unsupported run scope snapshot version")
    meters = snapshot.get("meters")
    if not isinstance(meters, list):
        raise PersistenceError("Run scope snapshot must contain a meter list")
    result: list[ScopeMeter] = []
    for meter in cast(list[object], meters):
        if not isinstance(meter, dict):
            raise PersistenceError("Run scope meters must be objects")
        meter = cast(dict[str, object], meter)
        if set(meter) != {
            "address", "external_id", "effective_start_utc", "effective_end_utc",
        }:
            raise PersistenceError("Invalid run scope meter shape")
        boundaries: list[datetime | None] = []
        for field in ("effective_start_utc", "effective_end_utc"):
            value = meter[field]
            if value is None:
                boundaries.append(None)
                continue
            if not isinstance(value, str):
                raise PersistenceError("Scope effective timestamps must be ISO strings")
            try:
                boundaries.append(_quarter(datetime.fromisoformat(value)))
            except ValueError:
                raise PersistenceError("Scope effective timestamp is invalid") from None
        result.append(ScopeMeter(
            _address(meter["address"]), _positive_id(meter["external_id"], "External ID"),
            boundaries[0], boundaries[1],
        ))
    _scope_meters(tuple(result))
    return tuple(result)


def _run_delay(body: object) -> timedelta:
    if not isinstance(body, str):
        raise PersistenceError("Run collection parameters must be JSON text")
    try:
        parameters = json.loads(body)
    except ValueError:
        raise PersistenceError("Run collection parameters are not valid JSON") from None
    if not isinstance(parameters, dict):
        raise PersistenceError("Run collection parameters must be an object")
    delay = cast(dict[str, object], parameters).get("delay_minutes")
    if type(delay) is not int or not 0 <= delay <= 65535:
        raise PersistenceError("Run must snapshot a non-negative delay_minutes setting")
    return timedelta(minutes=delay)


def prepare_batch(
    readings: tuple[EnergyReading, ...],
    scope: tuple[ScopeMeter, ...],
    start: datetime,
    end: datetime,
) -> Batch:
    """Resolve only expected slots; response content can never redefine the scope."""
    start, end = _quarter(start), _quarter(end)
    if start >= end or end - start > timedelta(days=1):
        raise ContractError("Persistence window must be increasing and at most one day")
    meters = _scope_meters(scope)
    expected: set[ReadingKey] = set()
    timestamp = start
    while timestamp < end:
        for meter in scope:
            if (
                (meter.effective_start is None or timestamp >= meter.effective_start)
                and (meter.effective_end is None or timestamp < meter.effective_end)
            ):
                expected.add((meter.address, timestamp))
        timestamp += INTERVAL
    if not expected:
        raise ContractError("No configured meter slots fall within the run window")
    groups: dict[ReadingKey, list[EnergyReading]] = {}
    unexpected: list[EnergyReading] = []
    duplicates = 0
    observed: set[ReadingKey] = set()
    for reading in readings:
        _address(reading.address)
        _positive_id(reading.external_id, "Reading external ID")
        _quarter(reading.timestamp_utc)
        if reading.value is not None and (
            not reading.value.is_finite() or reading.value < 0
            or reading.value > Decimal("999999999999999.999999")
            or reading.value != reading.value.quantize(Decimal("0.000001"))
        ):
            raise ContractError("Readings must be validated DECIMAL(21,6) values or invalid slots")
        if reading.key in observed:
            duplicates += 1
        observed.add(reading.key)
        if reading.key not in expected:
            unexpected.append(reading)
            continue
        groups.setdefault(reading.key, []).append(reading)
    samples: list[Sample] = []
    issues: list[tuple[ReadingKey, AlertType]] = [(key, "missing") for key in sorted(expected - groups.keys())]
    for key, records in sorted(groups.items()):
        identities = {reading.external_id for reading in records}
        values = {reading.value for reading in records}
        identity_mismatch = identities != {meters[key[0]].external_id}
        quality: Quality
        value: Decimal | None = None
        if identity_mismatch:
            quality = "conflict"
            issues.append((key, "identity_mismatch"))
        elif len(values) > 1:
            quality = "conflict"
            issues.append((key, "conflict"))
        elif any(reading.value is None or reading.error is not None for reading in records):
            quality = "invalid"
            issues.append((key, "invalid"))
        else:
            quality, value = "valid", records[0].value
        samples.append(Sample(
            key[0], meters[key[0]].external_id, key[1], value, quality,
            _json({"records": [reading.raw for reading in records]}),
        ))
    return Batch(frozenset(expected), tuple(samples), tuple(issues), tuple(unexpected), duplicates)


def _db_time(value: object) -> datetime:
    # mysql.connector returns naive DATETIME values, stored by contract as UTC.
    if not isinstance(value, datetime) or value.tzinfo is not None:
        raise PersistenceError("Database must return naive UTC DATETIME values")
    return value.replace(tzinfo=timezone.utc)


def _stored_decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    if not isinstance(value, (Decimal, int, str)):
        raise PersistenceError("Stored raw register must be an exact decimal")
    number = Decimal(value)
    if not number.is_finite():
        raise PersistenceError("Stored raw register must be finite")
    return number


db_time = _db_time
stored_decimal = _stored_decimal
snapshot_scope = _snapshot_scope


class ResultStore:
    """One MySQL transaction, one server, two InnoDB schemas, dedicated connection.

    The source row serializes cooperating writers. A locked run plus its current
    lease token fences abandoned workers. Never use this on separate DB servers.
    """

    def __init__(self, connection: Connection) -> None:
        self.connection = connection

    def save(self, run_id: int, lease_token: str, readings: tuple[EnergyReading, ...]) -> Result:
        _positive_id(run_id, "Run ID")
        try:
            token = str(UUID(lease_token))
        except (ValueError, AttributeError):
            raise PersistenceError("Lease token must be a UUID") from None
        connection = self.connection
        connection.start_transaction()
        cursor: Cursor | None = None
        try:
            cursor = connection.cursor()
            self._verify_engines(cursor)
            # Read source identity first; lock ordering is always source, then run.
            cursor.execute("SELECT data_source_id FROM myems_system_db.tbl_jishiyun_runs WHERE id = %s", (run_id,))
            run_source = cursor.fetchone()
            if run_source is None:
                raise PersistenceError("Run does not exist")
            source_id = _positive_id(run_source[0], "Data source ID")
            cursor.execute("SELECT id FROM myems_system_db.tbl_data_sources WHERE id = %s FOR UPDATE", (source_id,))
            if cursor.fetchone() is None:
                raise PersistenceError("Run data source does not exist")
            cursor.execute(
                "SELECT data_source_id, status, lease_token, lease_expires_datetime_utc, "
                "window_start_utc, window_end_utc, scope_snapshot_json, UTC_TIMESTAMP(), parameters_json "
                "FROM myems_system_db.tbl_jishiyun_runs WHERE id = %s FOR UPDATE",
                (run_id,),
            )
            run = cursor.fetchone()
            if run is None or run[0] != source_id:
                raise PersistenceError("Run data source changed")
            if run[1] != "running" or run[2] != token or _db_time(run[3]) <= _db_time(run[7]):
                raise PersistenceError("Run is not running under a current matching lease")
            start, end = _db_time(run[4]), _db_time(run[5])
            if end + _run_delay(run[8]) > _db_time(run[7]):
                raise PersistenceError("Collection window is not due after its configured provider delay")
            batch = prepare_batch(readings, _snapshot_scope(run[6]), start, end)
            inserted = updated = repeated = 0
            for sample in batch.samples:
                outcome = self._sample(cursor, source_id, run_id, sample)
                inserted += outcome == "inserted"
                updated += outcome == "updated"
                repeated += outcome == "duplicate"
            self._alerts(cursor, source_id, run_id, batch)
            if batch.unexpected:
                records: list[dict[str, object]] = [
                    {"address": reading.address, "external_id": reading.external_id,
                     "timestamp_utc": reading.timestamp_utc, "raw": reading.raw}
                    for reading in batch.unexpected
                ]
                self._event(cursor, source_id, run_id, "unexpected_readings", {"records": records})
            missing = sum(kind == "missing" for _, kind in batch.issues)
            result = Result(
                "partial" if batch.issues or batch.unexpected else "done",
                len(batch.expected), batch.returned_count, batch.valid_count,
                inserted, updated, batch.duplicate_count + repeated,
                batch.returned_count - batch.valid_count, missing, len(batch.unexpected),
            )
            self._event(cursor, source_id, run_id, "raw_result_saved", {
                "status": result.status, "expected_count": result.expected_count,
                "returned_count": result.returned_count, "valid_count": result.valid_count,
                "inserted_count": inserted, "updated_count": updated,
                "duplicate_count": result.duplicate_count, "missing_count": missing,
                "invalid_count": result.invalid_count, "unexpected_count": result.unexpected_count,
                "business_values_written": False,
            })
            cursor.execute(
                "UPDATE myems_system_db.tbl_jishiyun_runs SET status = %s, "
                "expected_count = %s, returned_count = %s, valid_count = %s, inserted_count = %s, "
                "updated_count = %s, duplicate_count = %s, invalid_count = %s, missing_count = %s, "
                "unexpected_count = %s, finished_datetime_utc = UTC_TIMESTAMP(), "
                "lease_token = NULL, lease_expires_datetime_utc = NULL, error_code = NULL, error_message = NULL "
                "WHERE id = %s AND status = 'running' AND lease_token = %s "
                "AND lease_expires_datetime_utc > UTC_TIMESTAMP()",
                (
                    result.status, result.expected_count, result.returned_count, result.valid_count,
                    inserted, updated, result.duplicate_count, result.invalid_count, missing,
                    result.unexpected_count, run_id, token,
                ),
            )
            if cursor.rowcount != 1:
                raise PersistenceError("Run lease expired before commit")
            connection.commit()
            return result
        except Exception:
            connection.rollback()
            raise
        finally:
            if cursor is not None:
                cursor.close()

    @staticmethod
    def _verify_engines(cursor: Cursor) -> None:
        tables = {
            ("myems_system_db", "tbl_data_sources"),
            ("myems_system_db", "tbl_jishiyun_runs"),
            ("myems_system_db", "tbl_jishiyun_alerts"),
            ("myems_system_db", "tbl_jishiyun_events"),
            ("myems_historical_db", "tbl_jishiyun_energy_readings"),
        }
        cursor.execute(
            "SELECT TABLE_SCHEMA, TABLE_NAME, ENGINE FROM information_schema.TABLES "
            "WHERE (TABLE_SCHEMA = 'myems_system_db' AND TABLE_NAME IN "
            "('tbl_data_sources', 'tbl_jishiyun_runs', 'tbl_jishiyun_alerts', 'tbl_jishiyun_events')) "
            "OR (TABLE_SCHEMA = 'myems_historical_db' AND TABLE_NAME = 'tbl_jishiyun_energy_readings')",
        )
        rows = cursor.fetchall()
        if len(rows) != len(tables) or any(
            (row[0], row[1]) not in tables or row[2] != "InnoDB" for row in rows
        ):
            raise PersistenceError("Raw persistence requires all participating tables on the same MySQL server using InnoDB")

    @staticmethod
    def _event(
        cursor: Cursor, source_id: int, run_id: int, kind: str, details: object,
        alert_id: int | None = None,
    ) -> None:
        cursor.execute(
            "INSERT INTO myems_system_db.tbl_jishiyun_events "
            "(data_source_id, run_id, alert_id, event_type, details_json) VALUES (%s, %s, %s, %s, %s)",
            (source_id, run_id, alert_id, kind, _json(details)),
        )

    def _sample(
        self, cursor: Cursor, source_id: int, run_id: int, sample: Sample,
    ) -> Literal["inserted", "updated", "duplicate"]:
        key = (source_id, sample.address, sample.timestamp_utc.replace(tzinfo=None), DATE_TYPE, VALUE_TYPE)
        cursor.execute(
            "SELECT id, external_id, forward_active_register, quality, revision, last_run_id "
            "FROM myems_historical_db.tbl_jishiyun_energy_readings "
            "WHERE data_source_id = %s AND external_address = %s AND reading_datetime_utc = %s "
            "AND date_type = %s AND value_type = %s FOR UPDATE",
            key,
        )
        previous = cursor.fetchone()
        if previous is None:
            cursor.execute(
                "INSERT INTO myems_historical_db.tbl_jishiyun_energy_readings "
                "(data_source_id, external_address, reading_datetime_utc, date_type, value_type, external_id, "
                "forward_active_register, quality, raw_record_json, first_run_id, last_run_id) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                key + (sample.external_id, sample.value, sample.quality, sample.raw_json, run_id, run_id),
            )
            return "inserted"
        if _positive_id(previous[5], "Previous run ID") > run_id:
            raise PersistenceError("An older run cannot overwrite a newer run's raw result")
        changed = (
            previous[1] != sample.external_id or _stored_decimal(previous[2]) != sample.value
            or previous[3] != sample.quality
        )
        revision = _positive_id(previous[4], "Sample revision") + int(changed)
        cursor.execute(
            "UPDATE myems_historical_db.tbl_jishiyun_energy_readings SET external_id = %s, "
            "forward_active_register = %s, quality = %s, raw_record_json = %s, last_run_id = %s, revision = %s "
            "WHERE id = %s",
            (sample.external_id, sample.value, sample.quality, sample.raw_json, run_id, revision, previous[0]),
        )
        if changed:
            self._event(cursor, source_id, run_id, "raw_sample_revised", {
                "address": sample.address, "timestamp_utc": sample.timestamp_utc,
                "previous": {"external_id": previous[1], "value": _stored_decimal(previous[2]), "quality": previous[3]},
                "current": {"external_id": sample.external_id, "value": sample.value, "quality": sample.quality},
                "previous_run_id": previous[5], "revision": revision,
                "requires_business_recalculation": True,
            })
        return "updated" if changed else "duplicate"

    def _alerts(self, cursor: Cursor, source_id: int, run_id: int, batch: Batch) -> None:
        issues = dict(batch.issues)
        returned = {sample.key for sample in batch.samples}
        valid = {sample.key for sample in batch.samples if sample.quality == "valid"}
        for address, timestamp in sorted(batch.expected):
            key = (address, timestamp)
            if key not in returned:
                cursor.execute(
                    "SELECT last_run_id FROM myems_historical_db.tbl_jishiyun_energy_readings "
                    "WHERE data_source_id = %s AND external_address = %s AND reading_datetime_utc = %s "
                    "AND date_type = %s AND value_type = %s FOR UPDATE",
                    (source_id, address, timestamp.replace(tzinfo=None), DATE_TYPE, VALUE_TYPE),
                )
                previous_sample = cursor.fetchone()
                if previous_sample is not None and _positive_id(previous_sample[0], "Previous run ID") > run_id:
                    raise PersistenceError("An older run cannot mark newer raw slots missing")
            cursor.execute(
                "SELECT id, alert_type, status, last_run_id, acknowledged_by_user_id, acknowledged_datetime_utc "
                "FROM myems_system_db.tbl_jishiyun_alerts "
                "WHERE data_source_id = %s AND external_address = %s AND reading_datetime_utc = %s "
                "AND alert_type IN ('missing', 'invalid', 'conflict', 'identity_mismatch') FOR UPDATE",
                (source_id, address, timestamp.replace(tzinfo=None)),
            )
            existing = cursor.fetchall()
            if any(_positive_id(row[3], "Alert run ID") > run_id for row in existing):
                raise PersistenceError("An older run cannot change newer slot alerts")
            issue = issues.get(key)
            for row in existing:
                if row[2] == "active" and key in valid:
                    cursor.execute(
                        "UPDATE myems_system_db.tbl_jishiyun_alerts SET status = 'recovered', "
                        "last_run_id = %s, last_seen_datetime_utc = UTC_TIMESTAMP(), "
                        "recovered_datetime_utc = UTC_TIMESTAMP() WHERE id = %s",
                        (run_id, row[0]),
                    )
                    self._event(cursor, source_id, run_id, "alert_recovered", {
                        "alert_id": row[0], "address": address, "timestamp_utc": timestamp, "type": row[1],
                    }, _positive_id(row[0], "Alert ID"))
            if issue is None:
                continue
            matching = next((row for row in existing if row[1] == issue), None)
            if matching is None:
                cursor.execute(
                    "INSERT INTO myems_system_db.tbl_jishiyun_alerts "
                    "(data_source_id, external_address, reading_datetime_utc, alert_type, "
                    "first_run_id, last_run_id, first_seen_datetime_utc, last_seen_datetime_utc) "
                    "VALUES (%s, %s, %s, %s, %s, %s, UTC_TIMESTAMP(), UTC_TIMESTAMP())",
                    (source_id, address, timestamp.replace(tzinfo=None), issue, run_id, run_id),
                )
                transition = "alert_created"
                alert_id = _positive_id(cursor.lastrowid, "New alert ID")
            else:
                cursor.execute(
                    "UPDATE myems_system_db.tbl_jishiyun_alerts SET last_run_id = %s, "
                    "last_seen_datetime_utc = UTC_TIMESTAMP(), recovered_datetime_utc = NULL, "
                    "acknowledged_by_user_id = CASE WHEN status = 'recovered' THEN NULL ELSE acknowledged_by_user_id END, "
                    "acknowledged_datetime_utc = CASE WHEN status = 'recovered' THEN NULL ELSE acknowledged_datetime_utc END, "
                    "status = 'active' "
                    "WHERE id = %s",
                    (run_id, matching[0]),
                )
                transition = "alert_reopened" if matching[2] == "recovered" else None
                alert_id = _positive_id(matching[0], "Alert ID")
            if transition is not None:
                self._event(cursor, source_id, run_id, transition, {
                    "address": address, "timestamp_utc": timestamp, "type": issue,
                    "previous_acknowledged_by_user_id": matching[4] if matching is not None else None,
                    "previous_acknowledged_datetime_utc": matching[5] if matching is not None else None,
                }, alert_id)
