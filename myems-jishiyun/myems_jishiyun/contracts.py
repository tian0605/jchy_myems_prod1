"""Jishiyun response contracts; no network access or persistence."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo
from decimal import Decimal, InvalidOperation
from typing import Any, cast


INTERVAL = timedelta(minutes=15)
READING_FIELD = "ZYGDNSZ"
VALUE_TYPE = "SJZ"
DATE_TYPE = "mi15"
ReadingKey = tuple[str, datetime]


class ContractError(ValueError):
    """The provider response cannot be safely processed."""


@dataclass(frozen=True)
class MeterArchive:
    external_id: int
    address: str
    name: str
    area_name: str
    voltage_ratio: Decimal
    current_ratio: Decimal
    single_rate: bool
    online: bool
    added_at_utc: datetime
    raw: dict[str, Any]


@dataclass(frozen=True)
class EnergyReading:
    address: str
    external_id: int
    timestamp_utc: datetime
    value: Decimal | None
    error: str | None
    raw: dict[str, Any]

    @property
    def key(self) -> ReadingKey:
        return self.address, self.timestamp_utc


@dataclass(frozen=True)
class Coverage:
    expected_count: int
    returned_count: int
    valid_count: int
    missing: tuple[ReadingKey, ...]
    invalid: tuple[ReadingKey, ...]
    unexpected: tuple[ReadingKey, ...]
    duplicate_count: int

    @property
    def complete(self) -> bool:
        return not (self.missing or self.invalid or self.unexpected)


def _rows(body: str) -> list[dict[str, Any]]:
    try:
        envelope = json.loads(body, parse_float=Decimal)
    except (json.JSONDecodeError, InvalidOperation) as exc:
        raise ContractError("Response is not valid JSON") from exc
    if not isinstance(envelope, dict):
        raise ContractError("Response must be an object envelope")
    envelope = cast(dict[str, Any], envelope)
    if envelope.get("IsSuccess") is not True or envelope.get("Code") != "0000":
        # Do not echo provider messages, which may contain request credentials.
        raise ContractError("Provider reported an unsuccessful response")
    rows = envelope.get("Data")
    count = envelope.get("Count")
    if not isinstance(rows, list):
        raise ContractError("Data must be an array of objects")
    rows = cast(list[Any], rows)
    if any(not isinstance(row, dict) for row in rows):
        raise ContractError("Data must be an array of objects")
    if type(count) is not int or count != len(rows):
        raise ContractError("Count must equal the number of returned records")
    return rows


def _text(row: dict[str, Any], field: str, max_length: int) -> str:
    value = row.get(field)
    if not isinstance(value, str) or not value.strip() or len(value) > max_length:
        raise ContractError(f"{field} must be a non-empty string up to {max_length} characters")
    if value != value.strip():
        raise ContractError(f"{field} must not contain surrounding whitespace")
    return value


def _external_id(row: dict[str, Any], field: str) -> int:
    value = row.get(field)
    if type(value) is not int or value <= 0 or value > 9223372036854775807:
        raise ContractError(f"{field} must be a positive BIGINT")
    return value


def _boolean(row: dict[str, Any], field: str) -> bool:
    value = row.get(field)
    if type(value) is not bool:
        raise ContractError(f"{field} must be boolean")
    return value


def _decimal(value: Any, field: str) -> Decimal:
    if value is None or isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ContractError(f"{field} must be a non-negative decimal")
    try:
        number = Decimal(str(value))
    except InvalidOperation as exc:
        raise ContractError(f"{field} must be a non-negative decimal") from exc
    if not number.is_finite() or number < 0 or number > Decimal("999999999999999.999999"):
        raise ContractError(f"{field} is outside DECIMAL(21,6)")
    if number != number.quantize(Decimal("0.000001")):
        raise ContractError(f"{field} has more than six fractional digits")
    return number


def _utc(value: Any, provider_timezone: tzinfo) -> datetime:
    if not isinstance(value, str):
        raise ContractError("Provider timestamp must be a string")
    for format_string in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S"):
        try:
            parsed = datetime.strptime(value, format_string)
        except ValueError:
            continue
        return parsed.replace(tzinfo=provider_timezone).astimezone(timezone.utc)
    raise ContractError("Provider timestamp has an unsupported format")


def _quarter_hour(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ContractError("Time window must use timezone-aware datetimes")
    result = value.astimezone(timezone.utc)
    if result.minute % 15 or result.second or result.microsecond:
        raise ContractError("Timestamp must align to a 15-minute boundary")
    return result


def electricity_request(start: datetime, end: datetime, provider_timezone: tzinfo) -> dict[str, str]:
    start, end = _quarter_hour(start), _quarter_hour(end)
    if start >= end:
        raise ContractError("Time window must be non-empty and increasing")
    return {
        "dateType": DATE_TYPE,
        "valueType": VALUE_TYPE,
        "meterAddress": "",
        "startTime": start.astimezone(provider_timezone).strftime("%Y-%m-%d %H:%M:%S"),
        "endTime": end.astimezone(provider_timezone).strftime("%Y-%m-%d %H:%M:%S"),
    }


def parse_archives(body: str, provider_timezone: tzinfo) -> tuple[MeterArchive, ...]:
    result: list[MeterArchive] = []
    addresses: set[str] = set()
    ids: set[int] = set()
    for row in _rows(body):
        address = _text(row, "Address", 64)
        external_id = _external_id(row, "ID")
        if address in addresses or external_id in ids:
            raise ContractError("Archive contains duplicate addresses or external IDs")
        addresses.add(address)
        ids.add(external_id)
        result.append(MeterArchive(
            external_id=external_id,
            address=address,
            name=_text(row, "AmmeterName", 255),
            area_name=_text(row, "AreaName", 512),
            voltage_ratio=_decimal(row.get("TimesV"), "TimesV"),
            current_ratio=_decimal(row.get("TimesA"), "TimesA"),
            single_rate=_boolean(row, "SingleRate"),
            online=_boolean(row, "OnlineState"),
            added_at_utc=_utc(row.get("AddDate"), provider_timezone),
            raw=row,
        ))
    return tuple(result)


def parse_readings(body: str, provider_timezone: tzinfo) -> tuple[EnergyReading, ...]:
    result: list[EnergyReading] = []
    for row in _rows(body):
        address = _text(row, "Address", 64)
        external_id = _external_id(row, "AmmeterID")
        timestamp = _utc(row.get("ReadingDate"), provider_timezone)
        local_timestamp = timestamp.astimezone(provider_timezone)
        # Provider-confirmed label: 23:59 means the next day's 00:00 register.
        if (local_timestamp.hour, local_timestamp.minute, local_timestamp.second) == (23, 59, 0):
            timestamp = (local_timestamp + timedelta(minutes=1)).astimezone(timezone.utc)
        timestamp = _quarter_hour(timestamp)
        try:
            value = _decimal(row.get(READING_FIELD), READING_FIELD)
            error = None
        except ContractError as exc:
            # Keep the identifiable slot for completeness alerts, without substituting zero.
            value = None
            error = str(exc)
        result.append(EnergyReading(address, external_id, timestamp, value, error, row))
    return tuple(result)


def check_coverage(
    readings: tuple[EnergyReading, ...],
    expected_addresses: set[str],
    start: datetime,
    end: datetime,
) -> Coverage:
    """Check [start, end); boundary readings for hourly differencing are checked separately."""
    start, end = _quarter_hour(start), _quarter_hour(end)
    if start >= end:
        raise ContractError("Time window must be non-empty and increasing")
    if not expected_addresses:
        raise ContractError("Expected device scope must not be empty")
    for address in expected_addresses:
        _text({"Address": address}, "Address", 64)
    expected: set[ReadingKey] = set()
    timestamp = start
    while timestamp < end:
        expected.update((address, timestamp) for address in expected_addresses)
        timestamp += INTERVAL
    observed: dict[ReadingKey, EnergyReading] = {}
    invalid: set[ReadingKey] = set()
    duplicate_count = 0
    for reading in readings:
        if reading.key in observed:
            duplicate_count += 1
            previous = observed[reading.key]
            if previous.value != reading.value or previous.external_id != reading.external_id:
                invalid.add(reading.key)
        else:
            observed[reading.key] = reading
        if reading.value is None or reading.error is not None:
            invalid.add(reading.key)
    received = set(observed)
    invalid &= expected
    return Coverage(
        expected_count=len(expected),
        returned_count=len(received & expected),
        valid_count=len((received & expected) - invalid),
        missing=tuple(sorted(expected - received)),
        invalid=tuple(sorted(invalid)),
        unexpected=tuple(sorted(received - expected)),
        duplicate_count=duplicate_count,
    )
