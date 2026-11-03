import datetime as dt
import unittest

from futures_app.calendar import ExchangeCalendar
from futures_app.common import CHINA


class CalendarTests(unittest.TestCase):
    def setUp(self):
        self.calendar = ExchangeCalendar()

    def test_friday_closure_includes_weekend_without_forcing_exit(self):
        result = self.calendar.context(dt.datetime(2026, 10, 30, 14, 50, tzinfo=CHINA))
        self.assertTrue(result["session_open"])
        self.assertTrue(result["preclose_review_due"])
        self.assertEqual(result["closure_hours"], 66.5)
        self.assertTrue(result["weekend_holiday_hold_allowed"])

    def test_holiday_and_makeup_working_saturday_are_both_closed(self):
        for day in (dt.date(2026, 10, 1), dt.date(2026, 2, 14)):
            self.assertFalse(self.calendar.is_trading_day(day))

    def test_lunch_and_no_night_session(self):
        for hour in (12, 21):
            result = self.calendar.context(dt.datetime(2026, 10, 30, hour, 0, tzinfo=CHINA))
            self.assertFalse(result["session_open"])

    def test_uncovered_calendar_never_promises_a_session(self):
        result = self.calendar.context(dt.datetime(2027, 1, 4, 10, 0, tzinfo=CHINA))
        self.assertFalse(result["session_open"])
        self.assertFalse(result["calendar_verified"])
        self.assertEqual(result["known_events_status"], "unknown")


if __name__ == "__main__":
    unittest.main()
