import unittest
from datetime import datetime, timedelta, timezone

from pydantic import ValidationError

from app.main import (
    PunchInIn,
    PunchOutIn,
    RegularizeIn,
    attendance_date,
    compute_late_minutes,
    compute_overtime,
    compute_work_hours,
    month_bounds,
    trend_pipeline,
    weekdays_between,
)


class AttendanceRuleTests(unittest.TestCase):
    def test_attendance_date_uses_previous_day_before_overnight_shift_end(self):
        punch_in = datetime(2026, 7, 7, 0, 0, tzinfo=timezone.utc)

        self.assertEqual(attendance_date(punch_in, "22:00", "06:00"), "2026-07-06")

    def test_late_grace_boundary_is_strict_and_minutes_are_floored(self):
        shift_start_utc = datetime(2026, 7, 6, 4, 0, tzinfo=timezone.utc)

        self.assertEqual(compute_late_minutes(shift_start_utc + timedelta(minutes=10), "2026-07-06", "09:30"), 0)
        self.assertEqual(compute_late_minutes(shift_start_utc + timedelta(minutes=10, seconds=1), "2026-07-06", "09:30"), 10)

    def test_work_hours_use_half_up_rounding(self):
        start = datetime(2026, 7, 6, 4, 0, tzinfo=timezone.utc)

        self.assertEqual(compute_work_hours(start, start + timedelta(seconds=18)), 0.01)

    def test_overnight_overtime_has_thirty_minute_minimum(self):
        shift_end = datetime(2026, 7, 7, 0, 30, tzinfo=timezone.utc)

        self.assertEqual(compute_overtime(shift_end + timedelta(minutes=29), "2026-07-06", "22:00", "06:00"), 0)
        self.assertEqual(compute_overtime(shift_end + timedelta(minutes=30), "2026-07-06", "22:00", "06:00"), 30)

    def test_last_supported_month_has_valid_bounds(self):
        start, end, first, last = month_bounds("9999-12")

        self.assertEqual((start, end), ("9999-12-01", "9999-12-31"))
        self.assertEqual((first.isoformat(), last.isoformat()), (start, end))
        self.assertEqual(weekdays_between(first, last), 23)
        trend_pipeline("Engineering", last, last)

    def test_non_nullable_optional_timestamps_reject_explicit_null(self):
        with self.assertRaises(ValidationError):
            PunchInIn.model_validate({"emp_code": "EMP0001", "punched_at": None})
        with self.assertRaises(ValidationError):
            PunchOutIn.model_validate({"emp_code": "EMP0001", "punched_at": None})
        with self.assertRaises(ValidationError):
            RegularizeIn.model_validate({
                "status": None,
                "reason": "manual correction",
                "regularized_by": "hr.admin",
            })


if __name__ == "__main__":
    unittest.main()
