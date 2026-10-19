import unittest
from datetime import datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

from configure_pilot import _new_period


LABEL = "PILOT TEST Jishiyun source 4"
START = datetime(2026, 10, 16, 16)
END = datetime(2026, 10, 18, 16)
OLD = (1, LABEL, 2, START - timedelta(days=1), START, "timeofuse", "元/kWh")
RATE = [(timedelta(0), timedelta(days=1), Decimal("2.3"))]


class Cursor:
    def __init__(self, periods, *, rate=None):
        self.periods = periods
        self.rate = RATE if rate is None else rate
        self.calls = []

    def execute(self, sql, params=()):
        self.calls.append((sql, params))
        if "JOIN myems_system_db.tbl_cost_centers_tariffs" in sql:
            self.rows = self.periods
        elif "SELECT start_time_of_day" in sql:
            self.rows = self.rate
        elif "SELECT id FROM" in sql:
            self.rows = []
        elif "LAST_INSERT_ID" in sql:
            self.rows = [(2,)]
        else:
            self.rows = []

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0]


class RenewalTests(unittest.TestCase):
    def run_period(self, cursor, *, start=START, end=END, apply=False):
        return _new_period(
            SimpleNamespace(source_id=4), cursor, 1, 2, 2, LABEL, start, end, apply=apply,
        )

    def test_preview_is_read_only(self):
        cursor = Cursor([OLD])
        result = self.run_period(cursor)
        self.assertEqual(result["status"], "preview")
        self.assertEqual(result["end_utc"], END.isoformat())
        self.assertTrue(all(sql.startswith("SELECT") for sql, _ in cursor.calls))

    def test_append_preserves_old_tariff_and_audits(self):
        cursor = Cursor([OLD])
        result = self.run_period(cursor, apply=True)
        self.assertEqual(result["status"], "configured")
        writes = [(sql, params) for sql, params in cursor.calls if not sql.startswith("SELECT")]
        self.assertEqual(len(writes), 4)
        self.assertTrue(all(sql.startswith("INSERT") for sql, _ in writes))
        self.assertIn("pilot_test_period_configured", writes[-1][0])
        self.assertEqual(writes[0][1][-2:], (START, END))

    def test_replay_does_not_write(self):
        period = (2, LABEL + " 20261016T1600Z-20261018T1600Z", 2, START, END, "timeofuse", "元/kWh")
        cursor = Cursor([OLD, period])
        self.assertEqual(self.run_period(cursor, apply=True)["status"], "already_configured")
        self.assertTrue(all(sql.startswith("SELECT") for sql, _ in cursor.calls))

    def test_overlap_gap_and_non_hour_rejected(self):
        for start in (START - timedelta(hours=1), START + timedelta(hours=1), START + timedelta(minutes=15)):
            cursor = Cursor([OLD])
            with self.subTest(start=start), self.assertRaises(ValueError):
                self.run_period(cursor, start=start, apply=True)
            self.assertTrue(all(sql.startswith("SELECT") for sql, _ in cursor.calls))

    def test_unexpected_existing_parameters_rejected(self):
        for index, replacement in ((1, "public"), (2, 1), (5, "other"), (6, "USD/kWh")):
            period = list(OLD)
            period[index] = replacement
            with self.subTest(index=index), self.assertRaises(ValueError):
                self.run_period(Cursor([tuple(period)]), apply=True)
        with self.assertRaises(ValueError):
            self.run_period(Cursor([OLD], rate=[(timedelta(0), timedelta(days=1), Decimal("9"))]), apply=True)


if __name__ == "__main__":
    unittest.main()
