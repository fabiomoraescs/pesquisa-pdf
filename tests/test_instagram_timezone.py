"""Apresentação local de timestamps UTC, independente do fuso da máquina."""

import unittest
from datetime import datetime, timezone

from platform_core.presentation import format_local_datetime, to_local_datetime


class InstagramTimezoneTests(unittest.TestCase):
    def test_utc_to_maceio_and_month_boundary(self):
        start = datetime(2026, 9, 1, 3, 0, tzinfo=timezone.utc)
        end = datetime(2026, 10, 1, 2, 59, 59, tzinfo=timezone.utc)
        self.assertEqual(format_local_datetime(start, "America/Maceio"), "01/09/2026 00:00")
        self.assertEqual(
            to_local_datetime(end, "America/Maceio").strftime("%d/%m/%Y %H:%M:%S"),
            "30/09/2026 23:59:59",
        )
        self.assertEqual(
            f"{format_local_datetime(start, 'America/Maceio')} a "
            f"{format_local_datetime(end, 'America/Maceio')}",
            "01/09/2026 00:00 a 30/09/2026 23:59",
        )

    def test_naive_sqlite_utc_and_missing_value(self):
        self.assertEqual(
            format_local_datetime(datetime(2026, 9, 1, 3, 0), "America/Maceio"),
            "01/09/2026 00:00",
        )
        self.assertIsNone(to_local_datetime(None, "America/Maceio"))
        self.assertEqual(format_local_datetime(None, "America/Maceio"), "—")

    def test_configured_timezone_not_host_timezone(self):
        utc_value = datetime(2026, 9, 1, 3, 0, tzinfo=timezone.utc)
        self.assertEqual(format_local_datetime(utc_value, "America/Maceio"), "01/09/2026 00:00")
        self.assertEqual(format_local_datetime(utc_value, "Europe/Lisbon"), "01/09/2026 04:00")


if __name__ == "__main__":
    unittest.main()
