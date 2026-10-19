"""Explicit, read-only API verification; never initializes meters or writes DB data."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone

from myems_jishiyun.client import JishiyunClient, TransportError, http_test_setting
from myems_jishiyun.contracts import ContractError, check_coverage, electricity_request


def provider_timezone(value: str) -> timezone:
    if not re.fullmatch(r"[+-]\d{2}:\d{2}", value):
        raise ValueError("Provider UTC offset must use +HH:MM or -HH:MM")
    hours, minutes = int(value[1:3]), int(value[4:6])
    if minutes > 59 or hours > 14 or (hours == 14 and minutes):
        raise ValueError("Provider UTC offset is out of range")
    offset = timedelta(hours=hours, minutes=minutes)
    return timezone(offset if value[0] == "+" else -offset)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True, help="Provider base URL ending in /WebApi; HTTPS by default")
    parser.add_argument("--utc-offset", required=True, help="Provider timestamp UTC offset, e.g. +08:00")
    parser.add_argument("--encoding", choices=("json", "form"), default="json")
    parser.add_argument("--start", help="Local provider time, YYYY-MM-DD HH:MM:SS")
    parser.add_argument("--end", help="Exclusive local provider time, YYYY-MM-DD HH:MM:SS")
    parser.add_argument("--expected-addresses", help="Comma-separated configured collection scope; never derived from response")
    arguments = parser.parse_args(argv)
    if bool(arguments.start) != bool(arguments.end):
        parser.error("--start and --end must be supplied together")
    if arguments.expected_addresses and not arguments.start:
        parser.error("--expected-addresses requires --start and --end")
    try:
        api_key = os.environ.get("JISHIYUN_API_KEY", "")
        if not api_key:
            raise ValueError("JISHIYUN_API_KEY environment variable is required")
        local_timezone = provider_timezone(arguments.utc_offset)
        start = end = None
        if arguments.start:
            start = datetime.strptime(arguments.start, "%Y-%m-%d %H:%M:%S").replace(tzinfo=local_timezone)
            end = datetime.strptime(arguments.end, "%Y-%m-%d %H:%M:%S").replace(tzinfo=local_timezone)
            if start >= end or end - start > timedelta(days=1):
                raise ValueError("Verification window must be increasing and no longer than one day")
            for boundary in (start, end):
                if boundary.minute % 15 or boundary.second:
                    raise ValueError("Verification window must align to 15-minute boundaries")
            electricity_request(start, end, local_timezone)
        scope: set[str] | None = None
        if arguments.expected_addresses:
            addresses = arguments.expected_addresses.split(",")
            if any(not address.strip() for address in addresses):
                raise ValueError("Configured addresses must not be empty")
            scope = {address.strip() for address in addresses}
            if len(scope) != len(addresses):
                raise ValueError("Configured addresses must not contain duplicates")
            if start is not None and end is not None:
                check_coverage((), scope, start, end)
        allow_http_test = http_test_setting(os.environ.get("JISHIYUN_ALLOW_HTTP_TEST", "false"))
        client = JishiyunClient(
            arguments.base_url, api_key, local_timezone, encoding=arguments.encoding, allow_http_test=allow_http_test,
        )
        archives = client.archives()
        summary: dict[str, object] = {
            "archive_count": len(archives),
            "archive_offline_count": sum(not archive.online for archive in archives),
            "scope_source": "explicit_configuration" if scope else "not_supplied",
        }
        incomplete = False
        if start is not None and end is not None:
            readings = client.readings(start, end)
            summary["energy_row_count"] = len(readings)
            summary["energy_distinct_address_count"] = len({reading.address for reading in readings})
            summary["invalid_reading_count"] = sum(reading.value is None for reading in readings)
            incomplete = any(reading.value is None for reading in readings)
            if scope is not None:
                coverage = check_coverage(readings, scope, start, end)
                summary.update({
                    "expected_count": coverage.expected_count,
                    "returned_count": coverage.returned_count,
                    "valid_count": coverage.valid_count,
                    "missing_count": len(coverage.missing),
                    "invalid_slot_count": len(coverage.invalid),
                    "unexpected_count": len(coverage.unexpected),
                    "duplicate_count": coverage.duplicate_count,
                    "interval_coverage_complete": coverage.complete,
                    "hourly_boundary_verified": False,
                })
                incomplete = incomplete or not coverage.complete
            else:
                summary["interval_coverage_complete"] = None
                summary["coverage_note"] = "No configured scope: completeness is not evaluated"
        summary["request_count_including_retries"] = client.request_count
        print(json.dumps(summary, ensure_ascii=True))
        return 2 if incomplete else 0
    except (ContractError, TransportError) as exc:
        print(f"Verification failed: {exc}", file=sys.stderr)
        return 1
    except ValueError:
        # argparse/date parsing exceptions can contain user input; do not echo secrets.
        print("Verification configuration invalid: check key, provider URL, HTTP test permission, offset, window and scope", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
