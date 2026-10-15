"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience; real environment variables win)

The file is organised top to bottom as:
  1. Setup (database connection, constants)
  2. Small helpers: time conversion and the business rules R1-R5, R7, R8
  3. Request validation types and models
  4. Output formatting (what we send back to the client)
  5. Index creation (runs at startup)
  6. Endpoints: system, employees, attendance
  7. Analytics: MongoDB aggregation pipelines + endpoints
  8. Admin: explain endpoint
"""
import calendar
import json
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import date as Date
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.exceptions import RequestValidationError
from pydantic import AfterValidator, BaseModel, Field, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

# --------------------------------------------------------------------------- #
# 1. Setup
# --------------------------------------------------------------------------- #
# load_dotenv() does NOT override variables that already exist in the environment,
# so real environment variables win over a local .env file.
load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

# tz_aware=True makes PyMongo return datetimes WITH a UTC time zone (not naive ones).
# serverSelectionTimeoutMS keeps /health from hanging for 30s when MongoDB is down.
client = MongoClient(MONGO_URI, tz_aware=True, serverSelectionTimeoutMS=5000)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

PRESENCE_STATUSES = ["PRESENT", "WFH", "ON_DUTY"]  # R6: these count as present
GRACE_SECONDS = 10 * 60  # R2
MIN_OVERTIME_MINUTES = 30  # R3
HALF_DAY_LIMIT = 4.50  # R5
MAX_TREND_DAYS = 92

log = logging.getLogger("uvicorn.error")


# --------------------------------------------------------------------------- #
# 2. Helpers: time conversion and business rules
# --------------------------------------------------------------------------- #
def ms_to_datetime(ms: Optional[int]) -> datetime:
    """Epoch milliseconds -> UTC datetime, cut down to WHOLE SECONDS (R1).
    If ms is None we use the current time."""
    if ms is None:
        return datetime.now(timezone.utc).replace(microsecond=0)
    return datetime.fromtimestamp(ms // 1000, tz=timezone.utc)


def datetime_to_ms(value: Optional[datetime]) -> Optional[int]:
    """Datetime -> integer epoch milliseconds. Uses integer maths (no float rounding errors)."""
    if value is None:
        return None
    if value.tzinfo is None:  # safety net: treat a naive datetime as UTC
        value = value.replace(tzinfo=timezone.utc)
    return (value - EPOCH) // timedelta(milliseconds=1)


def utc_now_ms_precision() -> datetime:
    """Current UTC time with millisecond precision (what MongoDB can store)."""
    now = datetime.now(timezone.utc)
    return now.replace(microsecond=(now.microsecond // 1000) * 1000)


def to_minutes(hhmm: str) -> int:
    """'09:30' -> 570 (minutes since midnight)."""
    hours, minutes = hhmm.split(":")
    return int(hours) * 60 + int(minutes)


def is_overnight(shift_start: str, shift_end: str) -> bool:
    """R1: a shift whose end is <= its start crosses midnight."""
    return to_minutes(shift_end) <= to_minutes(shift_start)


def attendance_date_for(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    """R1: the attendance date is the IST calendar date of the punch-in.
    Exception: on an overnight shift, a punch-in earlier than shift_end belongs to the
    shift that started the previous day."""
    ist = punch_in.astimezone(IST)
    day = ist.date()
    if is_overnight(shift_start, shift_end):
        seconds_into_day = ist.hour * 3600 + ist.minute * 60 + ist.second
        if seconds_into_day < to_minutes(shift_end) * 60:
            day = day - timedelta(days=1)
    return day.isoformat()


def shift_start_at(date_str: str, shift_start: str) -> datetime:
    """The moment (IST) the shift starts on the attendance date."""
    day = Date.fromisoformat(date_str)
    minutes = to_minutes(shift_start)
    return datetime(day.year, day.month, day.day, minutes // 60, minutes % 60, tzinfo=IST)


def shift_end_at(date_str: str, shift_start: str, shift_end: str) -> datetime:
    """The moment (IST) the shift ends. For an overnight shift that is the NEXT calendar day."""
    day = Date.fromisoformat(date_str)
    minutes = to_minutes(shift_end)
    end = datetime(day.year, day.month, day.day, minutes // 60, minutes % 60, tzinfo=IST)
    if is_overnight(shift_start, shift_end):
        end = end + timedelta(days=1)
    return end


def compute_late_minutes(punch_in: datetime, shift_start: str, date_str: str) -> int:
    """R2: late only if STRICTLY more than 10:00 after shift start.
    When late, the answer is whole minutes since shift start (not since the grace ended)."""
    start = shift_start_at(date_str, shift_start)
    seconds = int((punch_in.replace(microsecond=0) - start).total_seconds())
    if seconds > GRACE_SECONDS:
        return seconds // 60
    return 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: (out - in) in hours, rounded to 2 decimals HALF-UP.
    We stay in whole numbers: hundredths = floor(seconds * 100 / 3600 + 0.5)."""
    seconds = int((punch_out.replace(microsecond=0) - punch_in.replace(microsecond=0)).total_seconds())
    hundredths = (seconds * 100 * 2 + 3600) // 7200
    return hundredths / 100


def compute_overtime(punch_out: datetime, shift_start: str, shift_end: str, date_str: str) -> int:
    """R3: whole minutes after shift end, counted only when that is at least 30."""
    end = shift_end_at(date_str, shift_start, shift_end)
    seconds = int((punch_out.replace(microsecond=0) - end).total_seconds())
    if seconds < 0:
        return 0
    minutes = seconds // 60
    return minutes if minutes >= MIN_OVERTIME_MINUTES else 0


def is_half_day(work_hours: float) -> bool:
    """R5: the ROUNDED work hours are below 4.50."""
    return work_hours < HALF_DAY_LIMIT


def derived_fields(emp: dict, status: str, date_str: str, punch_in: Optional[datetime], punch_out: Optional[datetime]) -> dict:
    """The four stored derived fields for a record (DATA_MODEL.md table)."""
    if status not in PRESENCE_STATUSES or punch_in is None:
        return {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    late = compute_late_minutes(punch_in, emp["shift_start"], date_str)
    if punch_out is None:
        return {"work_hours": None, "late_minutes": late, "overtime_minutes": 0, "half_day": False}
    hours = compute_work_hours(punch_in, punch_out)
    overtime = compute_overtime(punch_out, emp["shift_start"], emp["shift_end"], date_str)
    return {"work_hours": hours, "late_minutes": late, "overtime_minutes": overtime, "half_day": is_half_day(hours)}


def round_half_up(value, places: int) -> float:
    """R8: half-up rounding (Python's round() and MongoDB's $round both round half to EVEN)."""
    if isinstance(value, float):
        value = Decimal(repr(round(value, 9)))  # round(.., 9) removes float noise like 8.124999999999
    exponent = Decimal(1).scaleb(-places)
    return float(Decimal(value).quantize(exponent, rounding=ROUND_HALF_UP))


def month_bounds(month: str) -> tuple:
    """'2026-07' -> (date(2026,7,1), date(2026,7,31))."""
    year, mon = int(month[:4]), int(month[5:7])
    return Date(year, mon, 1), Date(year, mon, calendar.monthrange(year, mon)[1])


def count_working_days(start: Date, end: Date) -> int:
    """R7: number of Mon-Fri days from start to end inclusive."""
    if start > end:
        return 0
    total_days = (end - start).days + 1
    full_weeks, extra_days = divmod(total_days, 7)
    count = full_weeks * 5
    for i in range(extra_days):
        if (start + timedelta(days=i)).weekday() < 5:
            count += 1
    return count


# --------------------------------------------------------------------------- #
# 3. Validation types and models
# --------------------------------------------------------------------------- #
def unprocessable(field: str, message: str, where: str = "query"):
    """Raise a normal FastAPI 422 response ({"detail": [...]}) from inside an endpoint."""
    raise RequestValidationError([{"type": "value_error", "loc": (where, field), "msg": message, "input": None}])


def require_params(**params):
    """422 if any of the given query parameters is missing (used by the explain endpoint)."""
    missing = [name for name, value in params.items() if value is None]
    if missing:
        raise RequestValidationError(
            [{"type": "missing", "loc": ("query", name), "msg": "Field required", "input": None} for name in missing]
        )


def check_date(value: str) -> str:
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        raise ValueError("must be a date in YYYY-MM-DD format")
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        raise ValueError("not a real calendar date")
    return value


def check_month(value: str) -> str:
    if not re.fullmatch(r"[0-9]{4}-(0[1-9]|1[0-2])", value) or int(value[:4]) < 1:
        raise ValueError("must be a month in YYYY-MM format")
    return value


DateStr = Annotated[str, AfterValidator(check_date)]
MonthStr = Annotated[str, AfterValidator(check_month)]
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]
# strict=True: only real JSON integers pass. Floats (1.5, 1.0), strings and booleans are rejected.
# The range rejects "seconds-looking" values (< 100000000000) and absurdly large ones.
EpochMs = Annotated[int, Field(strict=True, ge=100_000_000_000, le=4_102_444_800_000)]
Page = Annotated[int, Query(ge=1, le=1_000_000_000)]
PageSize = Annotated[int, Query(ge=1, le=100)]
HHMM = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"


class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP[0-9]{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=HHMM)
    shift_end: str = Field(default="18:30", pattern=HHMM)
    joined_on: DateStr

    @model_validator(mode="after")
    def shifts_must_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    # Default None is not validated, but an explicit null IS rejected (the contract says integer).
    punched_at: EpochMs = None
    status: PresenceStatus = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: EpochMs = None


class RegularizeIn(BaseModel):
    status: Status = None
    punch_in: EpochMs = None
    punch_out: EpochMs = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def no_punch_times_with_absent_or_leave(self):
        if self.status in ("ABSENT", "LEAVE") and (self.punch_in is not None or self.punch_out is not None):
            raise ValueError("punch_in / punch_out cannot be supplied together with status ABSENT or LEAVE")
        return self


# --------------------------------------------------------------------------- #
# 4. Output formatting (DB document -> API JSON)
# --------------------------------------------------------------------------- #
def employee_out(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": datetime_to_ms(doc.get("created_at")),
    }


def history_out(entry: dict) -> dict:
    changes = {}
    for field, change in (entry.get("changes") or {}).items():
        before, after = change.get("from"), change.get("to")
        if field in ("punch_in", "punch_out"):  # these are BSON dates inside the nested object
            before, after = datetime_to_ms(before), datetime_to_ms(after)
        changes[field] = {"from": before, "to": after}
    return {"at": datetime_to_ms(entry.get("at")), "by": entry.get("by"), "reason": entry.get("reason"), "changes": changes}


def attendance_out(doc: dict) -> dict:
    """Never returns _id. Missing stored fields get the defaults from DATA_MODEL.md."""
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": datetime_to_ms(doc.get("punch_in")),
        "punch_out": datetime_to_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes") or 0,
        "overtime_minutes": doc.get("overtime_minutes") or 0,
        "half_day": bool(doc.get("half_day")),
        "history": [history_out(e) for e in (doc.get("history") or [])],
    }


# --------------------------------------------------------------------------- #
# 5. Indexes (created at startup; create_index does nothing if the index already exists)
# --------------------------------------------------------------------------- #
def ensure_indexes():
    employees = db.employees
    employees.create_index([("emp_code", ASCENDING)], unique=True, name="uniq_emp_code")
    employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="dept_joined_on")
    employees.create_index([("joined_on", ASCENDING)], name="joined_on")

    logs = db.attendance_logs
    # One record per employee per day. Also serves: per-employee queries, monthly analytics,
    # the trend's emp_code $in match, and the department-summary $lookup.
    logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uniq_emp_date")
    # GET /attendance default order (date desc, emp_code asc) and date-range filters.
    logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="date_desc_emp")
    # GET /attendance?status=... with the same sort order.
    logs.create_index([("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)], name="status_date_emp")
    # Late leaderboard: month range + late_minutes > 0, grouped by emp_code, answered from the index alone.
    logs.create_index([("date", ASCENDING), ("late_minutes", ASCENDING), ("emp_code", ASCENDING)], name="date_late_emp")


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        ensure_indexes()
    except PyMongoError as exc:  # keep the app up so /health can report 503
        log.warning("Could not create indexes at startup: %s", exc)
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# 6. Endpoints: system, employees, attendance
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except PyMongoError:
        raise HTTPException(503, "database unavailable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = utc_now_ms_precision()
    try:
        # The UNIQUE index on emp_code makes this safe: of two simultaneous requests,
        # MongoDB accepts one and raises DuplicateKeyError for the other.
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return employee_out(doc)


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: Page = 1, page_size: PageSize = 20):
    flt = {}
    if department is not None:
        flt["department"] = department
    total = db.employees.count_documents(flt)  # total of the FILTERED set
    cursor = db.employees.find(flt).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size)
    return {"items": [employee_out(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    punched_at = ms_to_datetime(body.punched_at)
    day = attendance_date_for(punched_at, emp["shift_start"], emp["shift_end"])
    doc = {
        "emp_code": body.emp_code,
        "date": day,
        "status": body.status,
        "punch_in": punched_at,
        "punch_out": None,
        "history": [],
    }
    doc.update(derived_fields(emp, body.status, day, punched_at, None))
    try:
        # No "check first, insert later": the UNIQUE (emp_code, date) index decides who wins.
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return attendance_out(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    punched_at = ms_to_datetime(body.punched_at)

    # The employee's most recent record that started at or before this punch-out.
    # This works across midnight because we look at punch_in, not at the calendar date.
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": punched_at}}, sort=[("punch_in", DESCENDING)]
    )
    if rec is None:
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")

    seconds = int((punched_at - rec["punch_in"]).total_seconds())
    if seconds <= 0:
        unprocessable("punched_at", "punch-out must be after punch-in", "body")
    if seconds > 24 * 3600:
        unprocessable("punched_at", "punch-out is more than 24 hours after punch-in", "body")

    new_values = derived_fields(emp, rec["status"], rec["date"], rec["punch_in"], punched_at)
    new_values["punch_out"] = punched_at
    new_values.pop("late_minutes")  # late_minutes was fixed at punch-in; punch-out does not change it

    # Atomic compare-and-set: the filter says "only if still not punched out". If two requests
    # race, MongoDB applies one update; the other finds no matching document -> 409.
    updated = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None, "punch_in": rec["punch_in"]},
        {"$set": new_values},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "already punched out (or record changed at the same time)")
    return attendance_out(updated)


def attendance_list_spec(emp_code, date_from, date_to, status, page, page_size):
    """The filter / sort / skip / limit of GET /attendance. Shared with the explain endpoint
    so that explain looks at exactly the query the real endpoint runs."""
    flt = {}
    if emp_code is not None:
        flt["emp_code"] = emp_code
    if date_from is not None or date_to is not None:
        flt["date"] = {}
        if date_from is not None:
            flt["date"]["$gte"] = date_from
        if date_to is not None:
            flt["date"]["$lte"] = date_to
    if status is not None:
        flt["status"] = status
    sort = {"date": -1, "emp_code": 1}
    return flt, sort, (page - 1) * page_size, page_size


def check_date_order(date_from, date_to):
    if date_from is not None and date_to is not None and date_from > date_to:
        unprocessable("date_from", "date_from must not be after date_to")


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[DateStr] = None,
    date_to: Optional[DateStr] = None,
    status: Optional[Status] = None,
    page: Page = 1,
    page_size: PageSize = 20,
):
    check_date_order(date_from, date_to)
    flt, sort, skip, limit = attendance_list_spec(emp_code, date_from, date_to, status, page, page_size)
    total = db.attendance_logs.count_documents(flt)
    # Sorting, skipping and limiting all happen inside MongoDB (using an index); we never load everything.
    cursor = db.attendance_logs.find(flt).sort(list(sort.items())).skip(skip).limit(limit)
    return {"items": [attendance_out(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: DateStr, body: RegularizeIn):
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if rec is None:
        raise HTTPException(404, "no attendance record for that date")

    # --- what the record looks like now (missing stored fields = defaults) ---
    old_status = rec["status"]
    old_in, old_out = rec.get("punch_in"), rec.get("punch_out")
    old = {
        "status": old_status,
        "punch_in": old_in,
        "punch_out": old_out,
        "work_hours": rec.get("work_hours"),
        "late_minutes": rec.get("late_minutes") or 0,
        "overtime_minutes": rec.get("overtime_minutes") or 0,
        "half_day": bool(rec.get("half_day")),
    }

    # --- what it should look like after the correction ---
    new_status = body.status if body.status is not None else old_status
    if new_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            unprocessable("punch_in", "cannot set punch times on an ABSENT or LEAVE record", "body")
        new_in, new_out = None, None  # ABSENT / LEAVE clear both punch times
    else:
        new_in = ms_to_datetime(body.punch_in) if body.punch_in is not None else old_in
        new_out = ms_to_datetime(body.punch_out) if body.punch_out is not None else old_out
        if new_in is None:
            unprocessable("punch_in", "a presence status requires a punch_in", "body")
        if body.punch_in is not None and attendance_date_for(new_in, emp["shift_start"], emp["shift_end"]) != date:
            unprocessable("punch_in", "punch_in must fall on this record's attendance date", "body")
        if new_out is not None:
            seconds = int((new_out - new_in).total_seconds())
            if seconds <= 0 or seconds > 24 * 3600:
                unprocessable("punch_out", "punch_out must be after punch_in and within 24 hours", "body")

    # "A request that changes nothing" = none of the three editable fields changes.
    if new_status == old_status and new_in == old_in and new_out == old_out:
        unprocessable("body", "the request does not change anything", "body")

    new = {"status": new_status, "punch_in": new_in, "punch_out": new_out}
    new.update(derived_fields(emp, new_status, date, new_in, new_out))  # recompute R2-R5

    # history.changes: only fields whose value actually changed
    changes = {f: {"from": old[f], "to": new[f]} for f in old if old[f] != new[f]}
    entry = {"at": utc_now_ms_precision(), "by": body.regularized_by, "reason": body.reason, "changes": changes}

    # Optimistic concurrency: write only if the record is still exactly what we read, i.e. the three
    # editable fields are unchanged AND nobody has appended a history entry (history.N does not exist yet).
    history_length = len(rec.get("history") or [])
    still_unchanged = {
        "_id": rec["_id"],
        "status": old_status,
        "punch_in": old_in,
        "punch_out": old_out,
        f"history.{history_length}": {"$exists": False},
    }
    updated = db.attendance_logs.find_one_and_update(
        still_unchanged, {"$set": new, "$push": {"history": entry}}, return_document=ReturnDocument.AFTER
    )
    if updated is None:
        raise HTTPException(409, "the record was changed by someone else; please retry")
    return attendance_out(updated)


# --------------------------------------------------------------------------- #
# 7. Analytics (MongoDB aggregation pipelines)
# --------------------------------------------------------------------------- #
# Small reusable pieces of aggregation expressions. They read the STORED fields (never recompute).
IS_PRESENT = {"$in": ["$status", PRESENCE_STATUSES]}
IS_HALF_DAY = {"$eq": [{"$ifNull": ["$half_day", False]}, True]}
# "$date" is a 'YYYY-MM-DD' string; convert it to a date only to ask which weekday it is (1=Mon ... 7=Sun).
IS_WEEKDAY = {"$lte": [{"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}}}, 5]}
LATE_MINUTES = {"$ifNull": ["$late_minutes", 0]}
LATE_FLAG = {"$cond": [{"$gt": [LATE_MINUTES, 0]}, 1, 0]}
# We count presence in HALVES so everything stays an exact integer: full day = 2, half day = 1.
HALVES_IF_PRESENT = {"$cond": [IS_PRESENT, {"$cond": [IS_HALF_DAY, 1, 2]}, 0]}
HALVES_WEEKDAY_ONLY = {"$cond": [IS_WEEKDAY, HALVES_IF_PRESENT, 0]}  # R7: present_days only counts Mon-Fri
HOURS_COUNT = {"$and": [IS_PRESENT, {"$isNumber": "$work_hours"}]}  # present AND has a work_hours value


def month_range_strings(month: str) -> tuple:
    first, last = month_bounds(month)
    return first.isoformat(), last.isoformat()


def employee_monthly_pipeline(emp_code: str, first: str, last: str) -> list:
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first, "$lte": last}}},
        {
            "$group": {
                "_id": None,
                "present_halves": {"$sum": HALVES_WEEKDAY_ONLY},
                "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "late_count": {"$sum": LATE_FLAG},
                "total_late_minutes": {"$sum": LATE_MINUTES},
                "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
            }
        },
    ]


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: MonthStr):
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    first, last = month_bounds(month)
    result = list(db.attendance_logs.aggregate(employee_monthly_pipeline(emp_code, first.isoformat(), last.isoformat())))
    stats = result[0] if result else {}

    # R7: working days count from joined_on for people who joined during the month.
    start = max(first, Date.fromisoformat(emp["joined_on"]))
    working_days = count_working_days(start, last)
    halves = stats.get("present_halves", 0)
    attendance_pct = None
    if working_days > 0:  # present_days / working_days * 100  ==  halves * 50 / working_days
        attendance_pct = round_half_up(Decimal(50 * halves) / Decimal(working_days), 2)
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": halves / 2,
        "leave_days": stats.get("leave_days", 0),
        "late_count": stats.get("late_count", 0),
        "total_late_minutes": stats.get("total_late_minutes", 0),
        "total_overtime_minutes": stats.get("total_overtime_minutes", 0),
        "attendance_pct": attendance_pct,
    }


def department_summary_pipeline(first: str, last: str, department: Optional[str]) -> list:
    """Starts from EMPLOYEES (so people with no logs still count) and joins each one's logs."""
    employee_filter = {"joined_on": {"$lte": last}}  # R9: joined on or before the end of the month
    if department is not None:
        employee_filter["department"] = department

    def stat(name):  # the value from the per-employee stats document, or 0 if the employee has no logs
        return {"$ifNull": [{"$arrayElemAt": [f"$stats.{name}", 0]}, 0]}

    return [
        {"$match": employee_filter},
        {
            "$lookup": {
                "from": "attendance_logs",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "pipeline": [
                    {"$match": {"date": {"$gte": first, "$lte": last}}},
                    {
                        "$group": {
                            "_id": None,
                            "present_halves": {"$sum": HALVES_WEEKDAY_ONLY},
                            "hours_sum": {"$sum": {"$cond": [HOURS_COUNT, "$work_hours", 0]}},
                            "hours_count": {"$sum": {"$cond": [HOURS_COUNT, 1, 0]}},
                            "late_count": {"$sum": LATE_FLAG},
                            "late_minutes": {"$sum": LATE_MINUTES},
                            "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                            "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
                        }
                    },
                ],
                "as": "stats",
            }
        },
        {
            # One input row per employee, so $sum: 1 is the headcount. Sums are added up across the
            # department, which makes avg_work_hours an average over RECORDS, not over employees.
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "present_halves": {"$sum": stat("present_halves")},
                "hours_sum": {"$sum": stat("hours_sum")},
                "hours_count": {"$sum": stat("hours_count")},
                "late_count": {"$sum": stat("late_count")},
                "total_late_minutes": {"$sum": stat("late_minutes")},
                "leave_count": {"$sum": stat("leave_count")},
                "on_duty_count": {"$sum": stat("on_duty_count")},
            }
        },
        {"$sort": {"_id": 1}},
    ]


@app.get("/analytics/departments/summary")
def department_summary(month: MonthStr, department: Optional[str] = None):
    first, last = month_range_strings(month)
    items = []
    for row in db.employees.aggregate(department_summary_pipeline(first, last, department)):
        avg_hours = None
        if row["hours_count"] > 0:
            avg_hours = round_half_up(Decimal(repr(round(row["hours_sum"], 6))) / Decimal(row["hours_count"]), 2)
        items.append(
            {
                "department": row["_id"],
                "headcount": row["headcount"],
                "present_days": row["present_halves"] / 2,
                "avg_work_hours": avg_hours,
                "late_count": row["late_count"],
                "total_late_minutes": row["total_late_minutes"],
                "leave_count": row["leave_count"],
                "on_duty_count": row["on_duty_count"],
            }
        )
    return {"month": month, "items": items}


def late_leaderboard_pipeline(first: str, last: str, limit: int, department: Optional[str]) -> list:
    pipeline = [
        {"$match": {"date": {"$gte": first, "$lte": last}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},  # logs whose employee does not exist have an empty "emp" and are dropped here
    ]
    if department is not None:
        pipeline.append({"$match": {"emp.department": department}})  # filter BEFORE ranking
    pipeline += [
        # $rank = standard competition ranking (1, 2, 2, 4): ties share a rank, the next rank is skipped.
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},  # limit applied AFTER ranking, so tied rows are all kept
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
    ]
    return pipeline


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: MonthStr, limit: Annotated[int, Query(ge=1, le=50)] = 10, department: Optional[str] = None):
    first, last = month_range_strings(month)
    rows = list(db.attendance_logs.aggregate(late_leaderboard_pipeline(first, last, limit, department)))
    return {"month": month, "items": rows}


def check_trend_range(start: str, end: str):
    if end < start:
        unprocessable("to", "'to' must not be before 'from'")
    days = (Date.fromisoformat(end) - Date.fromisoformat(start)).days + 1
    if days > MAX_TREND_DAYS:
        unprocessable("to", f"the date range may not exceed {MAX_TREND_DAYS} days")


def department_codes_or_404(department: str) -> list:
    codes = db.employees.distinct("emp_code", {"department": department})
    if not codes:
        raise HTTPException(404, "department not found")
    return codes


def department_trend_pipeline(department: str, codes: list, start: str, end: str) -> list:
    first = Date.fromisoformat(start)
    n_days = (Date.fromisoformat(end) - first).days + 1
    first_dt = datetime(first.year, first.month, first.day, tzinfo=timezone.utc)
    day_string = {"$dateToString": {"format": "%Y-%m-%d", "date": {"$add": [first_dt, {"$multiply": ["$$i", 86400000]}]}}}
    return [
        # 1. Real data: one row per date that has records (index on emp_code + date).
        {"$match": {"emp_code": {"$in": codes}, "date": {"$gte": start, "$lte": end}}},
        {"$group": {"_id": "$date", "present_halves": {"$sum": HALVES_IF_PRESENT}, "late_count": {"$sum": LATE_FLAG}}},
        # 2. Gap filling: the database generates one zero-row for EVERY calendar day in the range...
        {
            "$unionWith": {
                "pipeline": [
                    {
                        "$documents": {
                            "$map": {
                                "input": {"$range": [0, n_days]},
                                "as": "i",
                                "in": {"_id": day_string, "present_halves": 0, "late_count": 0},
                            }
                        }
                    }
                ]
            }
        },
        # 3. ...and adding real + zero rows per date gives exactly one row per day, in date order.
        {
            "$group": {
                "_id": "$_id",
                "present_halves": {"$sum": "$present_halves"},
                "late_count": {"$sum": "$late_count"},
            }
        },
        {"$sort": {"_id": 1}},
        # 4. Headcount for each day (R9): department employees with joined_on <= that day.
        {
            "$lookup": {
                "from": "employees",
                "let": {"day": "$_id"},
                "pipeline": [
                    {"$match": {"$expr": {"$and": [
                        {"$eq": ["$department", {"$literal": department}]},
                        {"$lte": ["$joined_on", "$$day"]},
                    ]}}},
                    {"$count": "n"},
                ],
                "as": "hc",
            }
        },
        {
            "$set": {
                "headcount": {"$ifNull": [{"$arrayElemAt": ["$hc.n", 0]}, 0]},
                "is_working_day": {"$lte": [{"$isoDayOfWeek": {"$dateFromString": {"dateString": "$_id", "format": "%Y-%m-%d"}}}, 5]},
            }
        },
        {
            # rate in "ten-thousandths", rounded HALF-UP with integer maths (MongoDB's $round is half-to-even):
            #   rate = (halves / 2) / headcount   ->   floor((10000 * halves + headcount) / (2 * headcount))
            "$set": {
                "rate_e4": {
                    "$cond": [
                        {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                        {"$floor": {"$divide": [
                            {"$add": [{"$multiply": ["$present_halves", 10000]}, "$headcount"]},
                            {"$multiply": [2, "$headcount"]},
                        ]}},
                        None,
                    ]
                }
            }
        },
        # 5. Moving average over this row and up to 6 rows before it. Rows before 'from' do not exist in
        #    the pipeline, so the first rows automatically use a shorter window. $avg ignores null rates.
        {"$setWindowFields": {"sortBy": {"_id": 1}, "output": {"avg_e4": {"$avg": "$rate_e4", "window": {"documents": [-6, 0]}}}}},
        {
            "$project": {
                "_id": 0,
                "date": "$_id",
                "is_working_day": 1,
                "headcount": 1,
                "present_count": {"$divide": ["$present_halves", 2]},
                "late_count": 1,
                "attendance_rate": {"$cond": [{"$eq": ["$rate_e4", None]}, None, {"$divide": ["$rate_e4", 10000]}]},
                "moving_avg_7d": {"$cond": [
                    {"$eq": ["$avg_e4", None]},
                    None,
                    {"$divide": [{"$floor": {"$add": ["$avg_e4", 0.5]}}, 10000]},  # half-up to 4 decimals
                ]},
            }
        },
    ]


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_: Annotated[DateStr, Query(alias="from")], to: DateStr):
    check_trend_range(from_, to)  # 422 first ...
    codes = department_codes_or_404(department)  # ... then 404 for an unknown department
    rows = list(db.attendance_logs.aggregate(department_trend_pipeline(department, codes, from_, to)))
    return {"department": department, "items": rows}


# --------------------------------------------------------------------------- #
# 8. Admin: explain
# --------------------------------------------------------------------------- #
ExplainName = Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"]


def run_explain(command_body: dict) -> dict:
    """Run MongoDB's explain with executionStats and convert the BSON result to plain JSON."""
    result = db.command({"explain": command_body, "verbosity": "executionStats"})
    return json.loads(json_util.dumps(result))


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: ExplainName,
    emp_code: Optional[str] = None,
    month: Optional[MonthStr] = None,
    department: Optional[str] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    date_from: Optional[DateStr] = None,
    date_to: Optional[DateStr] = None,
    status: Optional[Status] = None,
    from_: Annotated[Optional[DateStr], Query(alias="from")] = None,
    to: Optional[DateStr] = None,
    page: Page = 1,
    page_size: PageSize = 20,
):
    if endpoint == "attendance_list":
        check_date_order(date_from, date_to)
        flt, sort, skip, lim = attendance_list_spec(emp_code, date_from, date_to, status, page, page_size)
        collection = "attendance_logs"
        body = {"find": collection, "filter": flt, "sort": sort, "skip": skip, "limit": lim}
    else:
        if endpoint == "employee_monthly":
            require_params(emp_code=emp_code, month=month)
            first, last = month_range_strings(month)
            collection, pipeline = "attendance_logs", employee_monthly_pipeline(emp_code, first, last)
        elif endpoint == "department_summary":
            require_params(month=month)
            first, last = month_range_strings(month)
            collection, pipeline = "employees", department_summary_pipeline(first, last, department)
        elif endpoint == "late_leaderboard":
            require_params(month=month)
            first, last = month_range_strings(month)
            collection, pipeline = "attendance_logs", late_leaderboard_pipeline(first, last, limit, department)
        else:  # department_trend
            require_params(department=department, **{"from": from_, "to": to})
            check_trend_range(from_, to)
            codes = department_codes_or_404(department)
            collection, pipeline = "attendance_logs", department_trend_pipeline(department, codes, from_, to)
        body = {"aggregate": collection, "pipeline": pipeline, "cursor": {}}
    return {"endpoint": endpoint, "collection": collection, "explain": run_explain(body)}
