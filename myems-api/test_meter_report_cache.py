import unittest
from unittest.mock import MagicMock, patch

from reports.metercache import meter_report_cache_allowed, space_report_cache_allowed


class MeterReportCacheTests(unittest.TestCase):
    def test_space_staged_id_and_uuid_bypass_cache(self):
        for identity, uuid, column in (("2", None, "s.id"), (None, "space-uuid", "s.uuid")):
            connection = self.connection((71,))
            with self.subTest(column=column), patch(
                "reports.metercache.mysql.connector.connect", return_value=connection,
            ):
                self.assertFalse(space_report_cache_allowed(identity, uuid))
            sql, args = connection.cursor.return_value.execute.call_args.args
            self.assertIn(column + "=%s", sql)
            self.assertIn("tbl_spaces_meters", sql)
            self.assertEqual(args, (identity or uuid,))
            connection.close.assert_called_once()

    def test_ordinary_space_caches_but_database_error_is_explicit(self):
        connection = self.connection(None)
        with patch("reports.metercache.mysql.connector.connect", return_value=connection):
            self.assertTrue(space_report_cache_allowed("2", None))
            connection.cursor.return_value.execute.side_effect = RuntimeError("database unavailable")
            with self.assertRaises(RuntimeError):
                space_report_cache_allowed("2", None)

    def connection(self, row):
        connection = MagicMock()
        connection.cursor.return_value.fetchone.return_value = row
        return connection

    def test_staged_id_bypasses_cache_and_preserves_id_precedence(self):
        connection = self.connection((71,))
        with patch("reports.metercache.mysql.connector.connect", return_value=connection):
            self.assertFalse(meter_report_cache_allowed("73", "ignored-uuid"))
        sql, args = connection.cursor.return_value.execute.call_args.args
        self.assertIn("m.id=%s", sql)
        self.assertEqual(args, ("73",))
        connection.close.assert_called_once()
        connection.cursor.return_value.close.assert_called_once()

    def test_uuid_and_legacy_meters_keep_caching(self):
        connection = self.connection(None)
        with patch("reports.metercache.mysql.connector.connect", return_value=connection):
            self.assertTrue(meter_report_cache_allowed(None, "meter-uuid"))
        sql, args = connection.cursor.return_value.execute.call_args.args
        self.assertIn("m.uuid=%s", sql)
        self.assertEqual(args, ("meter-uuid",))

    def test_database_failure_is_not_silently_treated_as_cacheable(self):
        connection = self.connection(None)
        connection.cursor.return_value.execute.side_effect = RuntimeError("database unavailable")
        with patch("reports.metercache.mysql.connector.connect", return_value=connection):
            with self.assertRaises(RuntimeError):
                meter_report_cache_allowed("73", None)
        connection.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
