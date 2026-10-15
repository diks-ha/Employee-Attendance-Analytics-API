"""Unit tests for the pure business-rule helpers in app/main.py (no database needed).

Run from the repository root (after `pip install -r requirements.txt`):
    python -m unittest tests.test_rules -v
"""
import unittest
from datetime import datetime, timedelta, timezone

from app import main as m

IST = m.IST
UTC = timezone.utc


def ist(y, mo, d, h, mi, s=0):
    """A moment given in IST wall-clock time, as a UTC datetime (what the API stores)."""
    return datetime(y, mo, d, h, mi, s, tzinfo=IST).astimezone(UTC)


class TimeConversion(unittest.TestCase):
    def test_ms_truncated_to_whole_seconds(self):
        # 09:40:00.900 counts as 09:40:00
        dt = m.ms_to_datetime(1783312500900)
        self.assertEqual(dt.microsecond, 0)
        self.assertEqual(m.datetime_to_ms(dt), 1783312500000)

    def test_round_trip_is_exact(self):
        self.assertEqual(m.datetime_to_ms(m.ms_to_datetime(1783312500000)), 1783312500000)

    def test_naive_datetime_treated_as_utc(self):
        self.assertEqual(m.datetime_to_ms(datetime(1970, 1, 1, 0, 0, 1)), 1000)

    def test_none_passes_through(self):
        self.assertIsNone(m.datetime_to_ms(None))


class AttendanceDate_R1(unittest.TestCase):
    def test_day_shift_uses_ist_date_not_utc_date(self):
        # 00:30 IST on Jul 7 is still Jul 6 in UTC. The attendance date must be the IST date.
        self.assertEqual(m.attendance_date_for(ist(2026, 7, 7, 0, 30), "09:30", "18:30"), "2026-07-07")

    def test_late_evening_ist_is_next_day_in_utc_but_same_ist_day(self):
        self.assertEqual(m.attendance_date_for(ist(2026, 7, 6, 23, 50), "09:30", "18:30"), "2026-07-06")

    def test_overnight_punch_in_before_shift_end_belongs_to_previous_day(self):
        # shift 22:00-06:00; punch at 02:00 IST on Jul 7 -> shift that started Jul 6
        self.assertEqual(m.attendance_date_for(ist(2026, 7, 7, 2, 0), "22:00", "06:00"), "2026-07-06")

    def test_overnight_punch_in_exactly_at_shift_end_is_not_earlier(self):
        self.assertEqual(m.attendance_date_for(ist(2026, 7, 7, 6, 0, 0), "22:00", "06:00"), "2026-07-07")
        self.assertEqual(m.attendance_date_for(ist(2026, 7, 7, 5, 59, 59), "22:00", "06:00"), "2026-07-06")

    def test_overnight_evening_punch_is_same_day(self):
        self.assertEqual(m.attendance_date_for(ist(2026, 7, 6, 21, 55), "22:00", "06:00"), "2026-07-06")

    def test_is_overnight(self):
        self.assertTrue(m.is_overnight("22:00", "06:00"))
        self.assertTrue(m.is_overnight("09:00", "09:00"))  # <= means overnight
        self.assertFalse(m.is_overnight("09:30", "18:30"))

    def test_overnight_shift_end_is_next_calendar_day(self):
        end = m.shift_end_at("2026-07-06", "22:00", "06:00")
        self.assertEqual((end.year, end.month, end.day, end.hour), (2026, 7, 7, 6))


class Late_R2(unittest.TestCase):
    def late(self, h, mi, s, shift="09:30"):
        t = ist(2026, 7, 6, h, mi, s)
        return m.compute_late_minutes(t, shift, "2026-07-06")

    def test_examples_from_the_contract(self):
        self.assertEqual(self.late(9, 40, 0), 0)  # exactly 10:00 after start -> not late
        self.assertEqual(self.late(9, 40, 1), 10)  # one second over -> late, 10 whole minutes
        self.assertEqual(self.late(10, 15, 59), 45)

    def test_on_time_and_early(self):
        self.assertEqual(self.late(9, 30, 0), 0)
        self.assertEqual(self.late(8, 0, 0), 0)

    def test_sample_document_2(self):
        self.assertEqual(self.late(10, 5, 0), 35)

    def test_overnight_late_uses_previous_day_shift_start(self):
        # date 2026-07-06, shift 22:00-06:00, punch at 22:20 same evening -> 20 min late
        t = ist(2026, 7, 6, 22, 20)
        self.assertEqual(m.compute_late_minutes(t, "22:00", "2026-07-06"), 20)
        # 21:55 -> early -> 0
        self.assertEqual(m.compute_late_minutes(ist(2026, 7, 6, 21, 55), "22:00", "2026-07-06"), 0)


class Overtime_R3(unittest.TestCase):
    def ot(self, out, shift_start="09:30", shift_end="18:30", date="2026-07-06"):
        return m.compute_overtime(out, shift_start, shift_end, date)

    def test_below_30_is_zero(self):
        self.assertEqual(self.ot(ist(2026, 7, 6, 18, 35)), 0)  # sample document 1
        self.assertEqual(self.ot(ist(2026, 7, 6, 18, 59, 59)), 0)  # 29m59s -> floor 29 -> 0

    def test_exactly_30_counts(self):
        self.assertEqual(self.ot(ist(2026, 7, 6, 19, 0, 0)), 30)

    def test_floor_of_minutes(self):
        self.assertEqual(self.ot(ist(2026, 7, 6, 19, 10, 59)), 40)  # sample document 2: 19:10 -> 40

    def test_before_shift_end_is_zero(self):
        self.assertEqual(self.ot(ist(2026, 7, 6, 17, 0)), 0)

    def test_overnight_sample_document_7(self):
        out = ist(2026, 7, 7, 6, 40)
        self.assertEqual(self.ot(out, "22:00", "06:00", "2026-07-06"), 40)


class WorkHours_R4_R5(unittest.TestCase):
    def hours(self, seconds):
        a = ist(2026, 7, 6, 9, 0)
        return m.compute_work_hours(a, a + timedelta(seconds=seconds))

    def test_half_up_not_bankers(self):
        self.assertEqual(self.hours(32850), 9.13)  # 9.125h exactly -> half-up 9.13 (round() gives 9.12)
        self.assertEqual(self.hours(1 * 3600 + 18), 1.01)  # 1.005h -> 1.01 (float round() gives 1.0)

    def test_samples(self):
        a = ist(2026, 7, 6, 9, 28)
        self.assertEqual(m.compute_work_hours(a, ist(2026, 7, 6, 18, 35)), 9.12)  # sample 1
        self.assertEqual(m.compute_work_hours(ist(2026, 7, 6, 21, 55), ist(2026, 7, 7, 6, 40)), 8.75)  # sample 7
        self.assertEqual(m.compute_work_hours(ist(2026, 7, 7, 9, 30), ist(2026, 7, 7, 13, 0)), 3.5)  # sample 4

    def test_plain_values(self):
        self.assertEqual(self.hours(3600), 1.0)
        self.assertEqual(self.hours(0), 0.0)

    def test_half_day_uses_rounded_value(self):
        # 4.495h = 16182s -> rounds to 4.50 (half-up) -> NOT a half day
        self.assertEqual(self.hours(16182), 4.5)
        self.assertFalse(m.is_half_day(self.hours(16182)))
        # 4.494h = 16178.4s -> truncated 16178s -> 4.49 -> half day
        self.assertEqual(self.hours(16178), 4.49)
        self.assertTrue(m.is_half_day(self.hours(16178)))
        self.assertFalse(m.is_half_day(4.5))
        self.assertTrue(m.is_half_day(3.5))


class DerivedFields(unittest.TestCase):
    emp = {"shift_start": "09:30", "shift_end": "18:30"}

    def test_open_record(self):
        d = m.derived_fields(self.emp, "PRESENT", "2026-07-06", ist(2026, 7, 6, 10, 5), None)
        self.assertEqual(d, {"work_hours": None, "late_minutes": 35, "overtime_minutes": 0, "half_day": False})

    def test_closed_record_sample_2(self):
        d = m.derived_fields(self.emp, "PRESENT", "2026-07-06", ist(2026, 7, 6, 10, 5), ist(2026, 7, 6, 19, 10))
        self.assertEqual(d, {"work_hours": 9.08, "late_minutes": 35, "overtime_minutes": 40, "half_day": False})

    def test_absent_and_leave_get_defaults(self):
        for status in ("ABSENT", "LEAVE"):
            d = m.derived_fields(self.emp, status, "2026-07-06", None, None)
            self.assertEqual(d, {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False})


class Rounding_R8(unittest.TestCase):
    def test_half_up(self):
        self.assertEqual(m.round_half_up(2.675, 2), 2.68)  # round() gives 2.67
        self.assertEqual(m.round_half_up(0.125, 2), 0.13)  # round() gives 0.12
        self.assertEqual(m.round_half_up(8.125, 2), 8.13)
        self.assertEqual(m.round_half_up(0.00005, 4), 0.0001)

    def test_float_noise_is_ignored(self):
        # 8.124999999999998 is really 8.125 plus float noise (~2e-15), so it must round UP like 8.125
        self.assertEqual(m.round_half_up(8.124999999999998, 2), 8.13)
        # a value that is genuinely below the half stays down
        self.assertEqual(m.round_half_up(8.1249, 2), 8.12)
        self.assertEqual(m.round_half_up(0.1 + 0.2 + 8.0, 2), 8.3)

    def test_decimal_input_is_exact(self):
        from decimal import Decimal
        self.assertEqual(m.round_half_up(Decimal(50 * 3) / Decimal(7), 2), 21.43)  # 21.428571...
        self.assertEqual(m.round_half_up(Decimal(1) / Decimal(8), 2), 0.13)


class WorkingDays_R7(unittest.TestCase):
    D = m.Date

    def test_full_july_2026(self):
        first, last = m.month_bounds("2026-07")
        self.assertEqual((first, last), (self.D(2026, 7, 1), self.D(2026, 7, 31)))
        self.assertEqual(m.count_working_days(first, last), 23)  # July 2026 has 23 weekdays

    def test_february_leap_and_non_leap(self):
        self.assertEqual(m.month_bounds("2024-02")[1], self.D(2024, 2, 29))
        self.assertEqual(m.month_bounds("2026-02")[1], self.D(2026, 2, 28))
        self.assertEqual(m.count_working_days(*m.month_bounds("2026-02")), 20)

    def test_mid_month_joiner_counts_from_join_date(self):
        # joined Mon 2026-07-13 -> Jul 13..31 = 15 weekdays
        self.assertEqual(m.count_working_days(self.D(2026, 7, 13), self.D(2026, 7, 31)), 15)

    def test_joined_on_a_weekend(self):
        self.assertEqual(m.count_working_days(self.D(2026, 7, 11), self.D(2026, 7, 12)), 0)  # Sat-Sun
        self.assertEqual(m.count_working_days(self.D(2026, 7, 11), self.D(2026, 7, 31)), 15)

    def test_joined_after_month_end(self):
        self.assertEqual(m.count_working_days(self.D(2026, 8, 3), self.D(2026, 7, 31)), 0)

    def test_single_day(self):
        self.assertEqual(m.count_working_days(self.D(2026, 7, 6), self.D(2026, 7, 6)), 1)  # Monday
        self.assertEqual(m.count_working_days(self.D(2026, 7, 5), self.D(2026, 7, 5)), 0)  # Sunday


class Validators(unittest.TestCase):
    def test_check_date(self):
        self.assertEqual(m.check_date("2026-07-06"), "2026-07-06")
        for bad in ("2026-02-30", "2026-7-6", "20260706", "2026-07-06T00:00", "", "abc", "2026-07-06\n"):
            with self.assertRaises(ValueError, msg=bad):
                m.check_date(bad)

    def test_check_month(self):
        self.assertEqual(m.check_month("2026-12"), "2026-12")
        for bad in ("2026-13", "2026-00", "2026-7", "0000-01", "2026-07-01"):
            with self.assertRaises(ValueError, msg=bad):
                m.check_month(bad)


if __name__ == "__main__":
    unittest.main()
