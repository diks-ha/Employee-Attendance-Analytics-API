"""Integration tests: they talk to a RUNNING API and a REAL MongoDB.

!! These tests DELETE all documents in the employees and attendance_logs collections of MONGO_DB. !!
For safety they refuse to run unless the database name contains "test".

How to run (two terminals, same environment variables in both):
    export MONGO_URI="mongodb://localhost:27017"
    export MONGO_DB="attendance_test"
    uvicorn app.main:app --port 8000                       # terminal 1
    python -m unittest tests.test_api -v                   # terminal 2
Optional: BASE_URL=http://127.0.0.1:8000 (default), SDT_SCALE=1 also loads 100,000 records and checks speed + plans.

The expected analytics values come from a plain-Python "oracle" (simple loops, Decimal maths) that reads the
database contents directly. It is independent of the MongoDB pipelines, so a mismatch means one of the two is wrong.
"""
import json
import os
import threading
import time
import unittest
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal

from pymongo import MongoClient

BASE_URL = os.getenv("BASE_URL", "http://127.0.0.1:8000")  # 127.0.0.1, not "localhost": on Windows "localhost" can add ~2s per request
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")
IST = timezone(timedelta(hours=5, minutes=30))
PRESENT = ("PRESENT", "WFH", "ON_DUTY")

db = MongoClient(MONGO_URI, tz_aware=True)[MONGO_DB]


# ----------------------------------------------------------------------------- helpers
def call(method, path, body=None, raw=None):
    """Returns (status_code, parsed_json). `raw` lets a test send an exact JSON string."""
    data = raw.encode() if raw is not None else (json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(BASE_URL + path, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read() or "null")


def ist_ms(y, mo, d, h, mi, s=0, ms=0):
    return int(datetime(y, mo, d, h, mi, s, tzinfo=IST).timestamp()) * 1000 + ms


def ist_dt(y, mo, d, h, mi, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=IST).astimezone(timezone.utc)


def parallel(n, fn):
    """Run fn(i) in n threads that all start at the same moment; return the list of results."""
    results, barrier = [None] * n, threading.Barrier(n)

    def worker(i):
        barrier.wait()
        results[i] = fn(i)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    return results


def employee(code, dept, joined, start="09:30", end="18:30"):
    return {"emp_code": code, "name": f"Name {code}", "email": f"{code.lower()}@example.com", "department": dept,
            "shift_start": start, "shift_end": end, "joined_on": joined, "created_at": ist_dt(2026, 1, 1, 10, 0)}


def log(code, day, status="PRESENT", wh=None, late=0, ot=0, half=False, punch=True, **extra):
    d = {"emp_code": code, "date": day, "status": status, "punch_in": ist_dt(*map(int, day.split("-")), 9, 30) if punch else None,
         "punch_out": ist_dt(*map(int, day.split("-")), 18, 30) if (punch and wh is not None) else None,
         "work_hours": wh, "late_minutes": late, "overtime_minutes": ot, "half_day": half, "history": []}
    d.update(extra)
    return d


# July 2026: Jul 1 = Wednesday, Jul 6 = Monday, Jul 11/12 = Sat/Sun, Jul 13 = Monday.
EMPLOYEES = [
    employee("EMP0001", "Engineering", "2026-01-05"),
    employee("EMP0002", "Engineering", "2026-07-13"),  # mid-month joiner
    employee("EMP0003", "Sales", "2026-02-01"),
    employee("EMP0004", "Sales", "2026-02-01"),
    employee("EMP0005", "Support", "2026-03-10", "22:00", "06:00"),  # overnight shift
    employee("EMP0006", "Support", "2026-03-10"),  # no logs at all
    employee("EMP0007", "Operations", "2026-08-03"),  # joins AFTER July
    employee("EMP0008", "Engineering", "2026-01-05"),
]
LOGS = [
    log("EMP0001", "2026-07-06", wh=9.12),
    log("EMP0001", "2026-07-07", wh=9.03, late=30, ot=45),
    log("EMP0001", "2026-07-08", "WFH", wh=3.5, late=30, half=True),
    log("EMP0001", "2026-07-09", "LEAVE", punch=False),
    log("EMP0001", "2026-07-10", "ABSENT", punch=False),
    log("EMP0001", "2026-07-11", wh=5.0),  # Saturday record
    log("EMP0002", "2026-07-13", wh=8.0),
    {"emp_code": "EMP0002", "date": "2026-07-14", "status": "PRESENT", "punch_in": ist_dt(2026, 7, 14, 9, 32),  # legacy: no half_day/history
     "punch_out": ist_dt(2026, 7, 14, 18, 31), "work_hours": 8.98, "late_minutes": 0, "overtime_minutes": 0},
    log("EMP0002", "2026-07-15", late=10),  # open record, no punch_out
    log("EMP0003", "2026-07-06", wh=9.08, late=35, ot=40),
    log("EMP0003", "2026-07-07", "ON_DUTY", wh=7.5, late=65),
    log("EMP0003", "2026-07-08", "LEAVE", punch=False),
    log("EMP0004", "2026-07-07", "WFH", wh=3.5, half=True),
    log("EMP0004", "2026-07-08", "ABSENT", punch=False),
    log("EMP0004", "2026-07-11", wh=6.0, late=60),  # Saturday: late minutes still count
    log("EMP0005", "2026-07-06", wh=8.75, late=30, ot=40),
    log("EMP0008", "2026-07-06", wh=9.0, late=60),
    log("EMP9999", "2026-07-06", wh=9.0, late=500),  # employee does not exist -> ignored by leaderboard
]


def setUpModule():
    if "test" not in MONGO_DB.lower():
        raise RuntimeError(f"Refusing to wipe database '{MONGO_DB}': its name must contain 'test'.")
    db.employees.delete_many({})
    db.attendance_logs.delete_many({})
    db.employees.insert_many([dict(e) for e in EMPLOYEES])
    db.attendance_logs.insert_many([dict(l) for l in LOGS])


# ----------------------------------------------------------------------------- oracle (plain Python)
def half_up(value, places):
    return float(Decimal(value).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


def weekday(day_str):
    return date.fromisoformat(day_str).weekday() < 5


def present_value(rec, weekdays_only):
    if rec["status"] not in PRESENT or (weekdays_only and not weekday(rec["date"])):
        return Decimal(0)
    return Decimal("0.5") if rec.get("half_day") else Decimal(1)


def month_days(month):
    y, m = int(month[:4]), int(month[5:])
    first = date(y, m, 1)
    last = (date(y + (m == 12), m % 12 + 1, 1)) - timedelta(days=1)
    return first, last


def logs_in_month(month, code=None):
    first, last = month_days(month)
    return [l for l in db.attendance_logs.find() if first.isoformat() <= l["date"] <= last.isoformat() and (code is None or l["emp_code"] == code)]


def oracle_monthly(code, month):
    emp = db.employees.find_one({"emp_code": code})
    first, last = month_days(month)
    start = max(first, date.fromisoformat(emp["joined_on"]))
    wd = sum(1 for i in range((last - start).days + 1) if (start + timedelta(days=i)).weekday() < 5) if start <= last else 0
    recs = logs_in_month(month, code)
    present = sum((present_value(r, True) for r in recs), Decimal(0))
    return {
        "emp_code": code, "month": month, "working_days": wd, "present_days": float(present),
        "leave_days": sum(r["status"] == "LEAVE" for r in recs),
        "late_count": sum((r.get("late_minutes") or 0) > 0 for r in recs),
        "total_late_minutes": sum(r.get("late_minutes") or 0 for r in recs),
        "total_overtime_minutes": sum(r.get("overtime_minutes") or 0 for r in recs),
        "attendance_pct": half_up(present / wd * 100, 2) if wd else None,
    }


def oracle_summary(month, department=None):
    _, last = month_days(month)
    emps = [e for e in db.employees.find() if e["joined_on"] <= last.isoformat() and (department is None or e["department"] == department)]
    out = {}
    for e in emps:
        row = out.setdefault(e["department"], {"department": e["department"], "headcount": 0, "present": Decimal(0), "hours": [], "late_count": 0,
                                               "total_late_minutes": 0, "leave_count": 0, "on_duty_count": 0})
        row["headcount"] += 1
        for r in logs_in_month(month, e["emp_code"]):
            row["present"] += present_value(r, True)
            if r["status"] in PRESENT and r.get("work_hours") is not None:
                row["hours"].append(Decimal(str(r["work_hours"])))
            row["late_count"] += (r.get("late_minutes") or 0) > 0
            row["total_late_minutes"] += r.get("late_minutes") or 0
            row["leave_count"] += r["status"] == "LEAVE"
            row["on_duty_count"] += r["status"] == "ON_DUTY"
    items = []
    for name in sorted(out):
        r = out[name]
        items.append({"department": name, "headcount": r["headcount"], "present_days": float(r["present"]),
                      "avg_work_hours": half_up(sum(r["hours"]) / len(r["hours"]), 2) if r["hours"] else None,
                      "late_count": r["late_count"], "total_late_minutes": r["total_late_minutes"],
                      "leave_count": r["leave_count"], "on_duty_count": r["on_duty_count"]})
    return {"month": month, "items": items}


def oracle_leaderboard(month, limit, department=None):
    emps = {e["emp_code"]: e for e in db.employees.find()}
    totals = {}
    for l in logs_in_month(month):
        if l["emp_code"] in emps and (l.get("late_minutes") or 0) > 0 and (department is None or emps[l["emp_code"]]["department"] == department):
            t = totals.setdefault(l["emp_code"], [0, 0])
            t[0] += l["late_minutes"]
            t[1] += 1
    ordered = sorted(totals.items(), key=lambda kv: (-kv[1][0], kv[0]))
    rows = []
    for code, (minutes, count) in ordered:
        rank = 1 + sum(1 for _, (m2, _) in ordered if m2 > minutes)  # standard competition rank
        rows.append({"rank": rank, "emp_code": code, "name": emps[code]["name"], "department": emps[code]["department"],
                     "total_late_minutes": minutes, "late_count": count})
    return {"month": month, "items": [r for r in rows if r["rank"] <= limit]}


def oracle_trend(department, start, end):
    emps = [e for e in db.employees.find() if e["department"] == department]
    codes = {e["emp_code"] for e in emps}
    rows, rates = [], []
    day, last = date.fromisoformat(start), date.fromisoformat(end)
    while day <= last:
        ds = day.isoformat()
        recs = [l for l in db.attendance_logs.find({"date": ds}) if l["emp_code"] in codes]
        hc = sum(e["joined_on"] <= ds for e in emps)
        present = sum((present_value(r, False) for r in recs), Decimal(0))
        working = day.weekday() < 5
        rate = half_up(present / hc, 4) if (working and hc > 0) else None
        rates.append(rate)
        window = [r for r in rates[-7:] if r is not None]  # this row + up to 6 earlier rows inside the range
        moving = half_up(sum(Decimal(str(r)) for r in window) / len(window), 4) if window else None
        rows.append({"date": ds, "is_working_day": working, "headcount": hc, "present_count": float(present),
                     "late_count": sum((r.get("late_minutes") or 0) > 0 for r in recs), "attendance_rate": rate, "moving_avg_7d": moving})
        day += timedelta(days=1)
    return {"department": department, "items": rows}


# ----------------------------------------------------------------------------- tests
class TestHealth(unittest.TestCase):
    def test_health_ok(self):
        self.assertEqual(call("GET", "/health"), (200, {"status": "ok"}))


class TestEmployees(unittest.TestCase):
    base = {"emp_code": "EMP5001", "name": "Test Person", "email": "t@example.com", "department": "QA", "joined_on": "2026-07-01"}

    def test_create_defaults_and_shape(self):
        status, body = call("POST", "/employees", {**self.base, "emp_code": "EMP5001", "created_at": 5, "_id": "x"})
        self.assertEqual(status, 201)
        self.assertEqual((body["shift_start"], body["shift_end"]), ("09:30", "18:30"))
        self.assertIsInstance(body["created_at"], int)
        self.assertGreater(body["created_at"], 100_000_000_000)  # server clock, not the client's 5
        self.assertNotIn("_id", body)
        self.assertEqual(call("POST", "/employees", {**self.base, "emp_code": "EMP5001"})[0], 409)

    def test_validation_422(self):
        bad = [{"emp_code": "EMP12"}, {"emp_code": "abc"}, {"emp_code": "EMP1234567"}, {"email": "nope"}, {"name": ""},
               {"department": ""}, {"shift_start": "25:00"}, {"shift_end": "9:5"}, {"shift_start": "09:30", "shift_end": "09:30"},
               {"joined_on": "2026-02-30"}, {"joined_on": "07/01/2026"}]
        for i, change in enumerate(bad):
            payload = {**self.base, "emp_code": f"EMP6{i:03d}", **change}
            self.assertEqual(call("POST", "/employees", payload)[0], 422, msg=str(change))
        self.assertEqual(call("POST", "/employees", {"emp_code": "EMP6999"})[0], 422)

    def test_concurrent_duplicate_creates(self):
        results = parallel(8, lambda i: call("POST", "/employees", {**self.base, "emp_code": "EMP5100"})[0])
        self.assertEqual(sorted(results), [201] + [409] * 7)

    def test_list_pagination_filter_total_sort(self):
        total_sales = db.employees.count_documents({"department": "Sales"})
        status, body = call("GET", "/employees?department=Sales&page=1&page_size=1")
        self.assertEqual((status, body["total"], len(body["items"]), body["page"], body["page_size"]), (200, total_sales, 1, 1, 1))
        self.assertEqual(call("GET", "/employees?department=sales")[1]["total"], 0)  # case-sensitive
        _, page1 = call("GET", "/employees?page=1&page_size=3")
        _, page2 = call("GET", "/employees?page=2&page_size=3")
        codes = [e["emp_code"] for e in page1["items"] + page2["items"]]
        self.assertEqual(codes, sorted(codes))
        self.assertEqual(len(set(codes)), 6)  # no overlap between pages (starter skipped a whole page)
        self.assertEqual(page1["total"], db.employees.count_documents({}))
        for q in ("page=0", "page=-1", "page_size=0", "page_size=101", "page=abc"):
            self.assertEqual(call("GET", "/employees?" + q)[0], 422, msg=q)


class TestPunching(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        for code, start, end in (("EMP7001", "09:30", "18:30"), ("EMP7002", "22:00", "06:00"), ("EMP7003", "09:30", "18:30")):
            call("POST", "/employees", {"emp_code": code, "name": "P", "email": "p@example.com", "department": "PunchDept",
                                        "shift_start": start, "shift_end": end, "joined_on": "2026-01-01"})

    def test_bad_timestamps_422(self):
        for raw in ('1783312500', '1783312500000.5', '"1783312500000"', 'true', 'null', '99999999999', '4102444800001', '-5'):
            status, _ = call("POST", "/attendance/punch-in", raw=f'{{"emp_code": "EMP7001", "punched_at": {raw}}}')
            self.assertEqual(status, 422, msg=raw)
        self.assertEqual(call("POST", "/attendance/punch-in", {"emp_code": "EMP7001", "status": "ABSENT"})[0], 422)
        self.assertEqual(call("POST", "/attendance/punch-in", {"emp_code": "EMP7001", "status": "bogus"})[0], 422)

    def test_unknown_employee_404(self):
        self.assertEqual(call("POST", "/attendance/punch-in", {"emp_code": "EMP0000"})[0], 404)
        self.assertEqual(call("POST", "/attendance/punch-out", {"emp_code": "EMP0000"})[0], 404)

    def test_day_shift_flow_late_overtime_halfup(self):
        # 09:40:01 -> late 10 ; milliseconds are truncated and the truncated value is returned
        status, rec = call("POST", "/attendance/punch-in", {"emp_code": "EMP7001", "punched_at": ist_ms(2026, 9, 7, 9, 40, 1, 900), "status": "WFH"})
        self.assertEqual(status, 201)
        self.assertEqual((rec["date"], rec["late_minutes"], rec["status"], rec["punch_out"], rec["work_hours"], rec["history"]),
                         ("2026-09-07", 10, "WFH", None, None, []))
        self.assertEqual(rec["punch_in"], ist_ms(2026, 9, 7, 9, 40, 1))
        self.assertNotIn("id", rec)
        self.assertEqual(call("POST", "/attendance/punch-in", {"emp_code": "EMP7001", "punched_at": ist_ms(2026, 9, 7, 12, 0)})[0], 409)
        # out 19:10:30 -> 9h30m29s -> 9.51 ; overtime 40 ; not a half day
        status, rec = call("POST", "/attendance/punch-out", {"emp_code": "EMP7001", "punched_at": ist_ms(2026, 9, 7, 19, 10, 30)})
        self.assertEqual(status, 200)
        self.assertEqual((rec["work_hours"], rec["overtime_minutes"], rec["half_day"], rec["late_minutes"]), (9.51, 40, False, 10))
        self.assertEqual(call("POST", "/attendance/punch-out", {"emp_code": "EMP7001", "punched_at": ist_ms(2026, 9, 7, 20, 0)})[0], 409)

    def test_exactly_ten_minutes_is_not_late(self):
        _, rec = call("POST", "/attendance/punch-in", {"emp_code": "EMP7001", "punched_at": ist_ms(2026, 9, 8, 9, 40, 0)})
        self.assertEqual(rec["late_minutes"], 0)

    def test_overnight_shift_across_midnight(self):
        _, rec = call("POST", "/attendance/punch-in", {"emp_code": "EMP7002", "punched_at": ist_ms(2026, 9, 14, 21, 55)})
        self.assertEqual((rec["date"], rec["late_minutes"]), ("2026-09-14", 0))
        status, rec = call("POST", "/attendance/punch-out", {"emp_code": "EMP7002", "punched_at": ist_ms(2026, 9, 15, 6, 40)})
        self.assertEqual((status, rec["date"], rec["work_hours"], rec["overtime_minutes"]), (200, "2026-09-14", 8.75, 40))
        # an early-morning punch-in belongs to the shift that started the previous day
        _, rec = call("POST", "/attendance/punch-in", {"emp_code": "EMP7002", "punched_at": ist_ms(2026, 9, 16, 2, 0)})
        self.assertEqual(rec["date"], "2026-09-15")

    def test_punch_out_errors(self):
        call("POST", "/attendance/punch-in", {"emp_code": "EMP7003", "punched_at": ist_ms(2026, 9, 21, 9, 30)})
        # before the punch-in -> nothing to close -> 404 ; equal -> 422 ; > 24h -> 422
        self.assertEqual(call("POST", "/attendance/punch-out", {"emp_code": "EMP7003", "punched_at": ist_ms(2026, 9, 21, 9, 29)})[0], 404)
        self.assertEqual(call("POST", "/attendance/punch-out", {"emp_code": "EMP7003", "punched_at": ist_ms(2026, 9, 21, 9, 30)})[0], 422)
        self.assertEqual(call("POST", "/attendance/punch-out", {"emp_code": "EMP7003", "punched_at": ist_ms(2026, 9, 22, 9, 30, 1)})[0], 422)
        self.assertEqual(call("POST", "/attendance/punch-out", {"emp_code": "EMP7003", "punched_at": ist_ms(2026, 9, 21, 13, 0)})[0], 200)  # 3.5h -> half day

    def test_half_day_flag(self):
        call("POST", "/attendance/punch-in", {"emp_code": "EMP7001", "punched_at": ist_ms(2026, 9, 28, 9, 30)})
        _, rec = call("POST", "/attendance/punch-out", {"emp_code": "EMP7001", "punched_at": ist_ms(2026, 9, 28, 13, 59, 59)})
        self.assertEqual((rec["work_hours"], rec["half_day"]), (4.5, False))  # 4.4997h rounds to 4.50 -> not < 4.50

    def test_concurrent_punch_in_and_punch_out(self):
        code = "EMP7001"
        body = {"emp_code": code, "punched_at": ist_ms(2026, 10, 5, 9, 30)}
        self.assertEqual(sorted(parallel(8, lambda i: call("POST", "/attendance/punch-in", body)[0])), [201] + [409] * 7)
        out = {"emp_code": code, "punched_at": ist_ms(2026, 10, 5, 18, 30)}
        self.assertEqual(sorted(parallel(8, lambda i: call("POST", "/attendance/punch-out", out)[0])), [200] + [409] * 7)


class TestAttendanceList(unittest.TestCase):
    def test_sorted_paginated_filtered(self):
        status, body = call("GET", "/attendance?date_from=2026-07-06&date_to=2026-07-08&page_size=100")
        self.assertEqual(status, 200)
        keys = [(r["date"], r["emp_code"]) for r in body["items"]]
        self.assertEqual(keys, sorted(keys, key=lambda k: (-int(k[0].replace("-", "")), k[1])))  # date desc, emp_code asc
        self.assertEqual(body["total"], db.attendance_logs.count_documents({"date": {"$gte": "2026-07-06", "$lte": "2026-07-08"}}))
        _, p1 = call("GET", "/attendance?date_from=2026-07-06&date_to=2026-07-08&page=1&page_size=4")
        _, p2 = call("GET", "/attendance?date_from=2026-07-06&date_to=2026-07-08&page=2&page_size=4")
        self.assertEqual([(r["date"], r["emp_code"]) for r in p1["items"] + p2["items"]], keys[:8])
        self.assertEqual(p1["total"], body["total"])

    def test_filters_and_defaults_for_legacy_documents(self):
        _, body = call("GET", "/attendance?emp_code=EMP0002&date_from=2026-07-14&date_to=2026-07-14")
        rec = body["items"][0]
        self.assertEqual((rec["half_day"], rec["history"], rec["overtime_minutes"]), (False, [], 0))
        self.assertEqual(set(rec), {"emp_code", "date", "status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day", "history"})
        self.assertIsInstance(rec["punch_in"], int)
        _, leave = call("GET", "/attendance?status=LEAVE")
        self.assertTrue(leave["items"] and all(r["status"] == "LEAVE" for r in leave["items"]))
        self.assertEqual(leave["total"], db.attendance_logs.count_documents({"status": "LEAVE"}))

    def test_422s(self):
        for q in ("date_from=2026-07-09&date_to=2026-07-08", "status=BOGUS", "date_from=2026-13-01", "page=0", "page_size=101"):
            self.assertEqual(call("GET", "/attendance?" + q)[0], 422, msg=q)


class TestRegularize(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        call("POST", "/employees", {"emp_code": "EMP7101", "name": "R", "email": "r@example.com", "department": "RegDept", "joined_on": "2026-01-01"})
        call("POST", "/attendance/punch-in", {"emp_code": "EMP7101", "punched_at": ist_ms(2026, 8, 3, 10, 5)})
        call("POST", "/attendance/punch-out", {"emp_code": "EMP7101", "punched_at": ist_ms(2026, 8, 3, 19, 10)})

    def patch(self, body, code="EMP7101", day="2026-08-03"):
        return call("PATCH", f"/attendance/{code}/{day}", {"reason": "biometric glitch", "regularized_by": "hr.admin", **body})

    def test_a_correction_updates_derived_fields_and_appends_one_history_entry(self):
        status, rec = self.patch({"punch_in": ist_ms(2026, 8, 3, 9, 28)})
        self.assertEqual(status, 200, rec)
        self.assertEqual((rec["late_minutes"], rec["work_hours"], rec["overtime_minutes"]), (0, 9.7, 40))
        self.assertEqual(len(rec["history"]), 1)
        entry = rec["history"][0]
        self.assertEqual(set(entry["changes"]), {"punch_in", "late_minutes", "work_hours"})  # overtime unchanged -> absent
        self.assertEqual(entry["changes"]["punch_in"]["to"], ist_ms(2026, 8, 3, 9, 28))
        self.assertEqual(entry["changes"]["late_minutes"], {"from": 35, "to": 0})
        self.assertEqual((entry["by"], entry["reason"]), ("hr.admin", "biometric glitch"))
        self.assertEqual(self.patch({"punch_in": ist_ms(2026, 8, 3, 9, 28)})[0], 422)  # nothing changes now

    def test_validation_and_404(self):
        self.assertEqual(self.patch({"status": "LEAVE", "punch_in": ist_ms(2026, 8, 3, 9, 0)})[0], 422)
        self.assertEqual(self.patch({"punch_out": ist_ms(2026, 8, 3, 8, 0)})[0], 422)  # before punch_in
        self.assertEqual(self.patch({"punch_in": ist_ms(2026, 8, 4, 9, 0)})[0], 422)  # not on the attendance date
        self.assertEqual(self.patch({})[0], 422)
        self.assertEqual(call("PATCH", "/attendance/EMP7101/2026-08-03", {"reason": "abc", "regularized_by": "x", "status": "LEAVE"})[0], 422)  # reason too short
        self.assertEqual(self.patch({"status": "LEAVE"}, code="EMP0000")[0], 404)
        self.assertEqual(self.patch({"status": "LEAVE"}, day="2026-08-09")[0], 404)
        self.assertEqual(self.patch({"status": "LEAVE"}, day="2026-02-30")[0], 422)

    def test_leave_clears_times_then_presence_needs_punch_in(self):
        call("POST", "/attendance/punch-in", {"emp_code": "EMP7101", "punched_at": ist_ms(2026, 8, 5, 9, 30)})
        status, rec = self.patch({"status": "ABSENT"}, day="2026-08-05")
        self.assertEqual((status, rec["punch_in"], rec["work_hours"], rec["half_day"]), (200, None, None, False))
        self.assertIn("punch_in", rec["history"][0]["changes"])
        self.assertEqual(self.patch({"status": "PRESENT"}, day="2026-08-05")[0], 422)  # no punch_in anywhere
        status, rec = self.patch({"status": "PRESENT", "punch_in": ist_ms(2026, 8, 5, 9, 31)}, day="2026-08-05")
        self.assertEqual((status, len(rec["history"])), (200, 2))

    def test_concurrent_corrections_do_not_lose_history(self):
        call("POST", "/attendance/punch-in", {"emp_code": "EMP7101", "punched_at": ist_ms(2026, 8, 6, 9, 30)})
        results = parallel(8, lambda i: self.patch({"punch_out": ist_ms(2026, 8, 6, 12 + i, 0)}, day="2026-08-06")[0])
        wins = results.count(200)
        self.assertGreaterEqual(wins, 1)
        self.assertTrue(all(r in (200, 409) for r in results), results)
        _, listing = call("GET", "/attendance?emp_code=EMP7101&date_from=2026-08-06&date_to=2026-08-06")
        self.assertEqual(len(listing["items"][0]["history"]), wins)  # exactly one entry per successful correction


class TestAnalytics(unittest.TestCase):
    def test_monthly_all_employees(self):
        for code in (e["emp_code"] for e in EMPLOYEES):
            status, body = call("GET", f"/analytics/employees/{code}/monthly?month=2026-07")
            self.assertEqual(status, 200)
            self.assertEqual(body, oracle_monthly(code, "2026-07"), msg=code)

    def test_monthly_known_values(self):
        _, b = call("GET", "/analytics/employees/EMP0001/monthly?month=2026-07")
        # 23 weekdays; present Jul 6 + 7 = 2, Jul 8 half = 0.5, Saturday ignored -> 2.5 ; 2.5/23*100 = 10.87
        self.assertEqual((b["working_days"], b["present_days"], b["leave_days"], b["late_count"], b["total_late_minutes"],
                          b["total_overtime_minutes"], b["attendance_pct"]), (23, 2.5, 1, 2, 60, 45, 10.87))
        _, b = call("GET", "/analytics/employees/EMP0002/monthly?month=2026-07")
        self.assertEqual((b["working_days"], b["present_days"]), (15, 3.0))  # joined Jul 13 -> 15 weekdays
        _, b = call("GET", "/analytics/employees/EMP0006/monthly?month=2026-07")  # no logs at all
        self.assertEqual((b["present_days"], b["late_count"], b["attendance_pct"]), (0.0, 0, 0.0))
        _, b = call("GET", "/analytics/employees/EMP0007/monthly?month=2026-07")  # joins in August
        self.assertEqual((b["working_days"], b["attendance_pct"]), (0, None))

    def test_monthly_errors(self):
        self.assertEqual(call("GET", "/analytics/employees/EMP0000/monthly?month=2026-07")[0], 404)
        for q in ("", "?month=2026-13", "?month=2026-7", "?month=abc"):
            self.assertEqual(call("GET", "/analytics/employees/EMP0001/monthly" + q)[0], 422, msg=q)

    def test_department_summary(self):
        for q in ("", "&department=Sales", "&department=Nope"):
            status, body = call("GET", "/analytics/departments/summary?month=2026-07" + q)
            self.assertEqual(status, 200)
            self.assertEqual(body, oracle_summary("2026-07", q.split("=")[1] if q else None), msg=q)
        _, body = call("GET", "/analytics/departments/summary?month=2026-07")
        names = [i["department"] for i in body["items"]]
        self.assertNotIn("Operations", names)  # nobody has joined yet
        support = next(i for i in body["items"] if i["department"] == "Support")
        self.assertEqual(support["headcount"], 2)  # EMP0006 has no logs but still counts
        self.assertEqual(call("GET", "/analytics/departments/summary?month=2026-08")[1], oracle_summary("2026-08"))

    def test_leaderboard_ties_at_the_cutoff(self):
        # totals: EMP0003=100, EMP0001=EMP0004=EMP0008=60, EMP0005=30, EMP0002=10 -> ranks 1,2,2,2,5,6
        _, body = call("GET", "/analytics/leaderboard/late?month=2026-07&limit=2")
        self.assertEqual([(r["rank"], r["emp_code"]) for r in body["items"]], [(1, "EMP0003"), (2, "EMP0001"), (2, "EMP0004"), (2, "EMP0008")])
        for limit in (1, 2, 3, 5, 10, 50):
            for dept in (None, "Engineering", "Sales"):
                path = f"/analytics/leaderboard/late?month=2026-07&limit={limit}" + (f"&department={dept}" if dept else "")
                self.assertEqual(call("GET", path)[1], oracle_leaderboard("2026-07", limit, dept), msg=path)
        codes = [r["emp_code"] for r in call("GET", "/analytics/leaderboard/late?month=2026-07&limit=50")[1]["items"]]
        self.assertNotIn("EMP9999", codes)
        for q in ("limit=0", "limit=51", "limit=x"):
            self.assertEqual(call("GET", "/analytics/leaderboard/late?month=2026-07&" + q)[0], 422, msg=q)

    def test_trend(self):
        for dept, a, b in (("Engineering", "2026-07-01", "2026-07-20"), ("Sales", "2026-07-06", "2026-07-12"), ("Support", "2026-06-29", "2026-07-05"),
                           ("Engineering", "2026-07-13", "2026-07-13"), ("Engineering", "2026-01-01", "2026-04-02")):
            status, body = call("GET", f"/analytics/departments/{dept}/trend?from={a}&to={b}")
            self.assertEqual(status, 200)
            expected = oracle_trend(dept, a, b)
            self.assertEqual(len(body["items"]), len(expected["items"]))
            self.assertEqual(body, expected, msg=(dept, a, b))
        _, body = call("GET", "/analytics/departments/Engineering/trend?from=2026-07-11&to=2026-07-13")
        sat, sun, mon = body["items"]
        self.assertEqual((sat["is_working_day"], sat["attendance_rate"], sat["present_count"]), (False, None, 1.0))  # weekend record counts as present_count
        self.assertEqual((sun["present_count"], sun["moving_avg_7d"]), (0.0, None))  # first rows: no non-null rate yet
        self.assertEqual((mon["headcount"], mon["attendance_rate"]), (3, 0.3333))  # EMP0002 joined Jul 13

    def test_trend_errors(self):
        self.assertEqual(call("GET", "/analytics/departments/Nowhere/trend?from=2026-07-01&to=2026-07-05")[0], 404)
        self.assertEqual(call("GET", "/analytics/departments/Sales/trend?from=2026-07-05&to=2026-07-01")[0], 422)
        self.assertEqual(call("GET", "/analytics/departments/Sales/trend?from=2026-07-01&to=2026-09-30")[0], 200)  # exactly 92 days
        self.assertEqual(call("GET", "/analytics/departments/Sales/trend?from=2026-07-01&to=2026-10-01")[0], 422)  # 93 days
        self.assertEqual(call("GET", "/analytics/departments/Sales/trend?from=2026-07-01")[0], 422)
        self.assertEqual(call("GET", "/analytics/departments/Sales/trend?from=bad&to=2026-07-01")[0], 422)


def winning_stages(explain):
    """All stage names inside the WINNING plan(s) only (rejected plans are ignored)."""
    plans = []
    if "queryPlanner" in explain:
        plans.append(explain["queryPlanner"]["winningPlan"])
    for stage in explain.get("stages", []):
        if "$cursor" in stage:
            plans.append(stage["$cursor"]["queryPlanner"]["winningPlan"])
    found = set()

    def walk(node):
        if isinstance(node, dict):
            if "stage" in node:
                found.add(node["stage"])
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    for plan in plans:
        walk(plan)
    return found, len(plans)


class TestExplain(unittest.TestCase):
    CASES = {
        "attendance_list": "",
        "attendance_list_filtered": "&emp_code=EMP0001&date_from=2026-07-01&date_to=2026-07-31&status=PRESENT&page=2&page_size=5",
        "employee_monthly": "&emp_code=EMP0001&month=2026-07",
        "department_summary": "&month=2026-07",
        "late_leaderboard": "&month=2026-07&limit=5&department=Sales",
        "department_trend": "&department=Engineering&from=2026-07-01&to=2026-07-31",
    }

    def test_winning_plans_use_an_index_and_no_collscan(self):
        for name, query in self.CASES.items():
            endpoint = name.replace("_filtered", "")
            status, body = call("GET", f"/admin/explain/{endpoint}?x=1{query}")
            self.assertEqual(status, 200, msg=name)
            self.assertEqual(body["endpoint"], endpoint)
            self.assertIn(body["collection"], ("attendance_logs", "employees"))
            stages, n_plans = winning_stages(body["explain"])
            print(f"\n  {name}: collection={body['collection']} winning stages={sorted(stages)}")
            self.assertGreaterEqual(n_plans, 1, msg=f"{name}: no winning plan found in the explain output")
            self.assertIn("IXSCAN", stages, msg=name)
            self.assertNotIn("COLLSCAN", stages, msg=name)
            self.assertIn("executionStats", json.dumps(body["explain"]))

    def test_explain_errors(self):
        self.assertEqual(call("GET", "/admin/explain/bogus")[0], 422)
        self.assertEqual(call("GET", "/admin/explain/employee_monthly?month=2026-07")[0], 422)
        self.assertEqual(call("GET", "/admin/explain/department_trend?department=Sales&from=2026-07-01")[0], 422)
        self.assertEqual(call("GET", "/admin/explain/late_leaderboard")[0], 422)


@unittest.skipUnless(os.getenv("SDT_SCALE"), "set SDT_SCALE=1 to load 100,000 records and check speed and plans")
class TestScale(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db.employees.delete_many({})
        db.attendance_logs.delete_many({})
        depts = ["Engineering", "Sales", "Support", "Finance", "HR"]
        emps = [employee(f"EMP{i:04d}", depts[i % 5], "2025-01-06") for i in range(1, 501)]
        db.employees.insert_many(emps)
        start, docs = date(2026, 1, 5), []
        for i in range(500):
            for d in range(200):  # 500 employees x 200 days = 100,000 records
                day = (start + timedelta(days=d)).isoformat()
                docs.append(log(emps[i]["emp_code"], day, wh=8.5, late=(d * (i + 7)) % 40))
        db.attendance_logs.insert_many(docs)
        cls.count = len(docs)

    def timed(self, path, limit=1.5):
        t0 = time.time()
        status, body = call("GET", path)
        elapsed = time.time() - t0
        self.assertEqual(status, 200, msg=path)
        print(f"\n  {elapsed * 1000:7.0f} ms  {path}")
        self.assertLess(elapsed, limit, msg=path)
        return body

    def test_speed_and_plans(self):
        self.assertEqual(self.count, 100_000)
        self.timed("/attendance?page=1&page_size=100")
        self.timed("/attendance?page=500&page_size=100&status=PRESENT")
        self.timed("/attendance?emp_code=EMP0042&date_from=2026-02-01&date_to=2026-03-31")
        self.timed("/analytics/employees/EMP0042/monthly?month=2026-03")
        self.timed("/analytics/departments/summary?month=2026-03")
        self.timed("/analytics/leaderboard/late?month=2026-03&limit=10")
        self.timed("/analytics/departments/Sales/trend?from=2026-01-05&to=2026-04-06")
        for name, query in TestExplain.CASES.items():
            endpoint = name.replace("_filtered", "")
            _, body = call("GET", f"/admin/explain/{endpoint}?x=1{query.replace('2026-07', '2026-03').replace('2026-07-01', '2026-03-01')}")
            stages, _ = winning_stages(body["explain"])
            self.assertIn("IXSCAN", stages, msg=name)
            self.assertNotIn("COLLSCAN", stages, msg=name)


if __name__ == "__main__":
    unittest.main()
