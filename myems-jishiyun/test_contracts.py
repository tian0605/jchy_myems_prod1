import json
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from myems_jishiyun.contracts import (
    ContractError,
    check_coverage,
    electricity_request,
    parse_archives,
    parse_readings,
)


LOCAL_TIME = timezone(timedelta(hours=8))
START = datetime(2026, 10, 1, 12, tzinfo=LOCAL_TIME)
END = START + timedelta(hours=1)


def response(rows: list[dict[str, object]]) -> str:
    return json.dumps({"IsSuccess": True, "Code": "0000", "Msg": "OK", "Data": rows, "Count": len(rows)})


def sample(address: str = "0001", minute: int = 0, value: object = 328585.2) -> dict[str, object]:
    return {
        "Address": address, "AmmeterID": 1,
        "ReadingDate": f"2026-10-01 12:{minute:02}",
        "ZYGDNSZ": value, "ZYGDN": 999999,
    }


class ContractTests(unittest.TestCase):
    def test_request_uses_sjz_and_all_devices(self):
        request = electricity_request(START, END, LOCAL_TIME)
        self.assertEqual(request, {
            "valueType": "SJZ", "dateType": "mi15", "meterAddress": "",
            "startTime": "2026-10-01 12:00:00", "endTime": "2026-10-01 13:00:00",
        })

    def test_reading_uses_only_forward_active_register(self):
        reading = parse_readings(response([sample()]), LOCAL_TIME)[0]
        self.assertEqual(reading.address, "0001")
        self.assertEqual(reading.value, Decimal("328585.2"))
        self.assertEqual(reading.timestamp_utc, datetime(2026, 10, 1, 4, tzinfo=timezone.utc))

    def test_provider_midnight_label_maps_to_next_day_and_preserves_raw(self):
        for label in ("2026-10-16 23:59", "2026-10-16 23:59:00", "2026/10/16 23:59:00",
                      "2026-12-31 23:59", "2028-02-29 23:59"):
            row = sample()
            row["ReadingDate"] = label
            reading = parse_readings(response([row]), LOCAL_TIME)[0]
            day = datetime.strptime(label[:10].replace("/", "-"), "%Y-%m-%d")
            expected = (day + timedelta(days=1)).replace(tzinfo=LOCAL_TIME).astimezone(timezone.utc)
            with self.subTest(label=label):
                self.assertEqual(reading.timestamp_utc, expected)
                self.assertEqual(reading.raw["ReadingDate"], label)
                self.assertEqual(reading.value, Decimal("328585.2"))
                self.assertTrue(check_coverage(
                    (reading,), {"0001"}, expected, expected + timedelta(minutes=15),
                ).complete)

    def test_midnight_label_uses_provider_timezone_not_utc(self):
        row = sample()
        row["ReadingDate"] = "2026-10-16 23:59"
        reading = parse_readings(response([row]), timezone.utc)[0]
        self.assertEqual(reading.timestamp_utc, datetime(2026, 10, 17, tzinfo=timezone.utc))

    def test_midnight_compatibility_does_not_round_other_invalid_times(self):
        for label in ("2026-10-16 23:59:01", "2026-10-16 22:59", "2026-10-16 23:58",
                      "2026-10-17 00:01"):
            row = sample()
            row["ReadingDate"] = label
            with self.subTest(label=label), self.assertRaises(ContractError):
                parse_readings(response([row]), LOCAL_TIME)

    def test_normalized_midnight_conflict_still_invalidates_slot(self):
        first, second = sample(), sample(value=5)
        first["ReadingDate"] = "2026-10-16 23:59"
        second["ReadingDate"] = "2026-10-17 00:00"
        start = datetime(2026, 10, 17, tzinfo=LOCAL_TIME)
        coverage = check_coverage(
            parse_readings(response([first, second]), LOCAL_TIME),
            {"0001"}, start, start + timedelta(minutes=15),
        )
        self.assertFalse(coverage.complete)
        self.assertEqual(len(coverage.invalid), 1)

    def test_missing_or_invalid_readings_are_not_zero(self):
        for value in (None, True, "not-a-number", "NaN", "Infinity", -1, "0.0000001", "1000000000000000"):
            with self.subTest(value=value):
                reading = parse_readings(response([sample(value=value)]), LOCAL_TIME)[0]
                self.assertIsNone(reading.value)
                self.assertIsNotNone(reading.error)
                coverage = check_coverage((reading,), {"0001"}, START, END)
                self.assertEqual(coverage.returned_count, 1)
                self.assertEqual(coverage.valid_count, 0)
                self.assertEqual(len(coverage.invalid), 1)
                self.assertFalse(coverage.complete)
        row = sample()
        del row["ZYGDNSZ"]
        self.assertIsNone(parse_readings(response([row]), LOCAL_TIME)[0].value)

    def test_zero_and_unchanged_registers_are_valid(self):
        for value in (0, 328585.2):
            readings = parse_readings(response([sample(minute=m, value=value) for m in (0, 15, 30, 45)]), LOCAL_TIME)
            self.assertTrue(check_coverage(readings, {"0001"}, START, END).complete)

    def test_33_device_scope_has_16_missing_slots(self):
        scope = {f"{index:04}" for index in range(33)}
        rows = [sample(f"{index:04}", minute) for index in range(29) for minute in (0, 15, 30, 45)]
        coverage = check_coverage(parse_readings(response(rows), LOCAL_TIME), scope, START, END)
        self.assertEqual((coverage.expected_count, coverage.returned_count, coverage.valid_count), (132, 116, 116))
        self.assertEqual(len(coverage.missing), 16)
        self.assertEqual(len({address for address, _ in coverage.missing}), 4)
        self.assertFalse(coverage.complete)

    def test_duplicates_cannot_fill_missing_slots(self):
        rows = [sample(), sample(), sample(), sample()]
        coverage = check_coverage(parse_readings(response(rows), LOCAL_TIME), {"0001"}, START, END)
        self.assertEqual(coverage.duplicate_count, 3)
        self.assertEqual(coverage.returned_count, 1)
        self.assertEqual(len(coverage.missing), 3)

    def test_conflicting_duplicate_invalidates_slot(self):
        rows = [sample(), sample(value=5)]
        coverage = check_coverage(parse_readings(response(rows), LOCAL_TIME), {"0001"}, START, END)
        self.assertEqual(len(coverage.invalid), 1)
        self.assertEqual(coverage.valid_count, 0)

    def test_conflicting_identity_invalidates_slot(self):
        first = sample()
        second = sample()
        second["AmmeterID"] = 2
        readings = parse_readings(response([first, second]), LOCAL_TIME)
        self.assertEqual(len(check_coverage(readings, {"0001"}, START, END).invalid), 1)

    def test_six_fractional_digits_are_preserved_without_float_rounding(self):
        body = response([sample(value=0)]).replace('"ZYGDNSZ": 0', '"ZYGDNSZ": 999999999999999.123456')
        reading = parse_readings(body, LOCAL_TIME)[0]
        self.assertEqual(reading.value, Decimal("999999999999999.123456"))

    def test_window_can_cross_local_midnight(self):
        start = datetime(2026, 10, 1, 23, 45, tzinfo=LOCAL_TIME)
        end = start + timedelta(minutes=30)
        coverage = check_coverage((), {"0001"}, start, end)
        self.assertEqual(coverage.expected_count, 2)
        self.assertEqual(coverage.missing[1][1], datetime(2026, 10, 1, 16, tzinfo=timezone.utc))

    def test_offline_or_unreturned_devices_stay_in_expected_scope(self):
        coverage = check_coverage(parse_readings(response([]), LOCAL_TIME), {"0001"}, START, END)
        self.assertEqual(len(coverage.missing), 4)
        self.assertEqual(coverage.valid_count, 0)

    def test_end_boundary_is_separate_from_four_interval_slots(self):
        row = sample()
        row["ReadingDate"] = "2026-10-01 13:00"
        coverage = check_coverage(parse_readings(response([row]), LOCAL_TIME), {"0001"}, START, END)
        self.assertEqual(len(coverage.unexpected), 1)
        self.assertEqual(len(coverage.missing), 4)

    def test_unexpected_device_is_not_used_to_fill_scope(self):
        coverage = check_coverage(parse_readings(response([sample("different")]), LOCAL_TIME), {"0001"}, START, END)
        self.assertEqual(len(coverage.unexpected), 1)
        self.assertEqual(coverage.returned_count, 0)

    def test_invalid_response_envelopes_fail_explicitly(self):
        cases = (
            "invalid json", "[]",
            '{"IsSuccess":false,"Code":"0000","Data":[],"Count":0}',
            '{"IsSuccess":true,"Code":"0001","Data":[],"Count":0}',
            '{"IsSuccess":true,"Code":"0000","Data":null,"Count":0}',
            '{"IsSuccess":true,"Code":"0000","Data":[],"Count":1}',
            '{"IsSuccess":true,"Code":"0000","Data":[],"Count":false}',
            '{"IsSuccess":true,"Code":"0000","Data":[1],"Count":1}',
        )
        for body in cases:
            with self.subTest(body=body), self.assertRaises(ContractError):
                parse_readings(body, LOCAL_TIME)

    def test_invalid_identity_or_timestamp_rejects_batch(self):
        for field, value in (("Address", 123), ("Address", " 0001"), ("AmmeterID", True),
                             ("ReadingDate", "2026-10-01 12:07"), ("ReadingDate", "invalid")):
            row = sample()
            row[field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ContractError):
                parse_readings(response([row]), LOCAL_TIME)

    def test_invalid_windows_fail(self):
        for start, end in ((START, START), (END, START), (START.replace(tzinfo=None), END),
                           (START + timedelta(minutes=1), END)):
            with self.subTest(start=start), self.assertRaises(ContractError):
                electricity_request(start, end, LOCAL_TIME)
        with self.assertRaises(ContractError):
            check_coverage((), set(), START, END)

    def test_archive_preserves_identity_bool_ratios_and_slash_dates(self):
        row: dict[str, object] = {
            "ID": 42, "Address": "0001", "AmmeterName": "Synthetic meter",
            "AreaName": "Test area", "TimesV": 1.0, "TimesA": 120.0,
            "SingleRate": False, "OnlineState": False, "AddDate": "2024/5/28 8:28:15",
        }
        archive = parse_archives(response([row]), LOCAL_TIME)[0]
        self.assertEqual(archive.current_ratio, Decimal("120.0"))
        self.assertFalse(archive.online)
        self.assertEqual(archive.added_at_utc, datetime(2024, 5, 28, 0, 28, 15, tzinfo=timezone.utc))
        with self.assertRaises(ContractError):
            parse_archives(response([row, row]), LOCAL_TIME)


if __name__ == "__main__":
    unittest.main()
