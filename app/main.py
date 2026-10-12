"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience; real environment variables win)

Layout of this file: config -> time/rule helpers -> serialisation -> models -> indexes ->
employees -> attendance -> analytics pipelines -> analytics endpoints -> admin explain.
"""
import json
import os
import re
from calendar import monthrange
from contextlib import asynccontextmanager
from datetime import date as date_cls, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Any, Literal, Optional

from bson import json_util
from bson.decimal128 import Decimal128
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, Field, StrictInt, field_validator, model_validator
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError

load_dotenv()  # override=False: real environment variables take priority

# tz_aware=True: PyMongo hands back aware UTC datetimes instead of naive ones.
client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
    serverSelectionTimeoutMS=5000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

PRESENCE = ["PRESENT", "WFH", "ON_DUTY"]
NON_PRESENCE = ("ABSENT", "LEAVE")
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]

MIN_MS = 100_000_000_000
MAX_MS = 4_102_444_800_000
GRACE_SECONDS = 10 * 60
MIN_OVERTIME_MIN = 30
HALF_DAY_BELOW = Decimal("4.50")

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


# --------------------------------------------------------------------------- #
# Indexes (idempotent; run at startup)
# --------------------------------------------------------------------------- #
_indexes_ready = False


def ensure_indexes() -> None:
    global _indexes_ready
    emp, logs = db.employees, db.attendance_logs

    def create_if_missing(collection, keys: list[tuple[str, int]], name: str, unique: bool = False) -> None:
        key_spec = tuple(keys)
        for index in collection.list_indexes():
            if tuple(index["key"].items()) == key_spec:
                if unique and not index.get("unique", False):
                    raise RuntimeError(f"index {key_spec} exists without the required unique constraint")
                return
        collection.create_index(keys, unique=unique, name=name)

    # emp_code is the business key: uniqueness is enforced by the database.
    create_if_missing(emp, [("emp_code", 1)], "emp_code_unique", unique=True)
    create_if_missing(emp, [("department", 1), ("emp_code", 1)], "emp_dept_code")      # list + filter + sort
    create_if_missing(emp, [("department", 1), ("joined_on", 1)], "emp_dept_joined")  # headcount / trend
    create_if_missing(emp, [("joined_on", 1)], "emp_joined")                           # unfiltered headcount
    # one record per (emp_code, date): punch-in race guard, monthly lookups, $lookup join key
    create_if_missing(logs, [("emp_code", 1), ("date", 1)], "log_emp_date", unique=True)
    create_if_missing(logs, [("date", -1), ("emp_code", 1)], "log_date_emp")           # list sort, leaderboard
    create_if_missing(logs, [("status", 1), ("date", -1), ("emp_code", 1)], "log_status_date_emp")
    create_if_missing(logs, [("emp_code", 1), ("punch_in", -1)], "log_emp_punchin")    # punch-out lookup
    _indexes_ready = True


@asynccontextmanager
async def lifespan(_: FastAPI):
    try:
        ensure_indexes()
    except Exception as exc:  # DB not reachable yet: /health reports 503, indexes retried there
        print(f"index creation deferred: {exc}")
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Validation helpers
# --------------------------------------------------------------------------- #
def bad(where: str, field: str, msg: str):
    raise RequestValidationError([{"type": "value_error", "loc": (where, field), "msg": msg, "input": None}])


def parse_date(value: str, field: str, where: str = "query") -> date_cls:
    if not DATE_RE.match(value or ""):
        bad(where, field, "must be YYYY-MM-DD")
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        bad(where, field, "not a valid calendar date")


def month_bounds(month: str) -> tuple[str, str, date_cls, date_cls]:
    """('2026-07-01', '2026-07-31', date, date) for 'YYYY-MM'."""
    if not MONTH_RE.match(month or ""):
        bad("query", "month", "must be YYYY-MM")
    year, mon = int(month[:4]), int(month[5:])
    try:
        first = date_cls(year, mon, 1)
    except ValueError:
        bad("query", "month", "invalid month")
    last = date_cls(year, mon, monthrange(year, mon)[1])
    return first.isoformat(), last.isoformat(), first, last


def require(value: Any, field: str):
    if value is None:
        bad("query", field, "field required")
    return value


# --------------------------------------------------------------------------- #
# Time + business-rule helpers (R1-R5)
# --------------------------------------------------------------------------- #
def ms_to_dt(ms: int) -> datetime:
    """Epoch ms -> aware UTC datetime, truncated to whole seconds (R1)."""
    return (EPOCH + timedelta(milliseconds=ms)).replace(microsecond=0)


def dt_to_ms(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return (dt - EPOCH) // timedelta(milliseconds=1)


def now_utc() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def hhmm(value: str) -> tuple[int, int]:
    h, m = value.split(":")
    return int(h), int(m)


def is_overnight(shift_start: str, shift_end: str) -> bool:
    return hhmm(shift_end) <= hhmm(shift_start)


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    """R1: IST calendar date of the punch-in; overnight shifts started the day before if earlier than shift_end."""
    ist = punch_in.astimezone(IST)
    day = ist.date()
    if is_overnight(shift_start, shift_end):
        eh, em = hhmm(shift_end)
        if (ist.hour, ist.minute, ist.second) < (eh, em, 0):
            day -= timedelta(days=1)
    return day.isoformat()


def shift_start_dt(date_str: str, shift_start: str) -> datetime:
    h, m = hhmm(shift_start)
    d = date_cls.fromisoformat(date_str)
    return datetime(d.year, d.month, d.day, h, m, tzinfo=IST)


def shift_end_dt(date_str: str, shift_start: str, shift_end: str) -> datetime:
    h, m = hhmm(shift_end)
    d = date_cls.fromisoformat(date_str)
    if is_overnight(shift_start, shift_end):
        d += timedelta(days=1)
    return datetime(d.year, d.month, d.day, h, m, tzinfo=IST)


def compute_late_minutes(punch_in: datetime, date_str: str, shift_start: str) -> int:
    """R2: late only if strictly more than 10:00 after shift start; then floor minutes since shift start."""
    secs = int((punch_in - shift_start_dt(date_str, shift_start)).total_seconds())
    return secs // 60 if secs > GRACE_SECONDS else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: seconds / 3600 rounded half-up to 2 decimals (Decimal, so no float/banker's rounding)."""
    secs = int((punch_out - punch_in).total_seconds())
    return float((Decimal(secs) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_overtime(punch_out: datetime, date_str: str, shift_start: str, shift_end: str) -> int:
    """R3: floor minutes after shift end, only when >= 30."""
    secs = int((punch_out - shift_end_dt(date_str, shift_start, shift_end)).total_seconds())
    minutes = secs // 60
    return minutes if minutes >= MIN_OVERTIME_MIN else 0


def derive(emp: dict, date_str: str, status: str, punch_in: Optional[datetime], punch_out: Optional[datetime]) -> dict:
    """All stored derived fields for a record, per the table in DATA_MODEL.md."""
    out = {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    if status in NON_PRESENCE or punch_in is None:
        return out
    out["late_minutes"] = compute_late_minutes(punch_in, date_str, emp["shift_start"])
    if punch_out is not None:
        wh = compute_work_hours(punch_in, punch_out)
        out["work_hours"] = wh
        out["overtime_minutes"] = compute_overtime(punch_out, date_str, emp["shift_start"], emp["shift_end"])
        out["half_day"] = Decimal(str(wh)) < HALF_DAY_BELOW
    return out


def half_up(value: Any, places: int) -> float:
    q = Decimal(1).scaleb(-places)
    return float(Decimal(repr(float(value))).quantize(q, rounding=ROUND_HALF_UP))


def to_decimal(value: Any) -> Decimal:
    if isinstance(value, Decimal128):
        return value.to_decimal()
    return Decimal(str(value))


def num(value: Any):
    """int when whole, else float (JSON-friendly counts such as 3.0 -> 3)."""
    value = float(value)
    return int(value) if value.is_integer() else value


# --------------------------------------------------------------------------- #
# Serialisation (BSON <-> API)
# --------------------------------------------------------------------------- #
def conv(value: Any):
    return dt_to_ms(value) if isinstance(value, datetime) else value


def employee_out(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": dt_to_ms(doc.get("created_at")),
    }


def history_out(entry: dict) -> dict:
    return {
        "at": dt_to_ms(entry.get("at")),
        "by": entry.get("by"),
        "reason": entry.get("reason"),
        "changes": {k: {"from": conv(v.get("from")), "to": conv(v.get("to"))} for k, v in (entry.get("changes") or {}).items()},
    }


def record_out(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": dt_to_ms(doc.get("punch_in")),
        "punch_out": dt_to_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes") or 0,
        "overtime_minutes": doc.get("overtime_minutes") or 0,
        "half_day": bool(doc.get("half_day") or False),
        "history": [history_out(h) for h in (doc.get("history") or [])],
    }


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
EpochMs = Annotated[StrictInt, Field(ge=MIN_MS, le=MAX_MS)]
TIME_PATTERN = r"^([01]\d|2[0-3]):[0-5]\d$"


class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=TIME_PATTERN)
    shift_end: str = Field(default="18:30", pattern=TIME_PATTERN)
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def _valid_date(cls, v: str) -> str:
        if not DATE_RE.match(v):
            raise ValueError("must be YYYY-MM-DD")
        datetime.strptime(v, "%Y-%m-%d")
        return v

    @model_validator(mode="after")
    def _shift_differs(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None
    status: PresenceStatus = "PRESENT"

    @field_validator("punched_at", mode="before")
    @classmethod
    def _punched_at_not_null(cls, value: Any) -> Any:
        if value is None:
            raise ValueError("must not be null")
        return value


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None

    @field_validator("punched_at", mode="before")
    @classmethod
    def _punched_at_not_null(cls, value: Any) -> Any:
        if value is None:
            raise ValueError("must not be null")
        return value


class RegularizeIn(BaseModel):
    status: Optional[Status] = None
    punch_in: Optional[EpochMs] = None
    punch_out: Optional[EpochMs] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @field_validator("status", "punch_in", "punch_out", mode="before")
    @classmethod
    def _optional_fields_not_null(cls, value: Any) -> Any:
        if value is None:
            raise ValueError("must not be null")
        return value


# --------------------------------------------------------------------------- #
# System + employees
# --------------------------------------------------------------------------- #
@app.get("/")
def root():
    return {
        "name": app.title,
        "docs": app.docs_url,
        "health": "/health",
    }


@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        if not _indexes_ready:
            ensure_indexes()
    except Exception:
        raise HTTPException(503, "database unavailable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_utc()
    try:
        db.employees.insert_one(dict(doc))  # unique index decides; concurrent duplicates -> exactly one wins
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return employee_out(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {"department": department} if department else {}
    total = db.employees.count_documents(q)
    cur = db.employees.find(q, {"_id": 0}).sort("emp_code", 1).skip((page - 1) * page_size).limit(page_size)
    return {"items": [employee_out(d) for d in cur], "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# Attendance
# --------------------------------------------------------------------------- #
def get_employee_or_404(emp_code: str) -> dict:
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    return emp


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = get_employee_or_404(body.emp_code)
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()
    d = attendance_date(ts, emp["shift_start"], emp["shift_end"])
    doc = {
        "emp_code": body.emp_code,
        "date": d,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, d, emp["shift_start"]),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)  # unique (emp_code, date): the loser of a race gets 409
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return record_out(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = get_employee_or_404(body.emp_code)
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts}}, sort=[("punch_in", -1)]
    )
    if not rec:
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    pin = rec["punch_in"]
    if ts <= pin:
        bad("body", "punched_at", "must be after punch_in")
    if ts - pin > timedelta(hours=24):
        bad("body", "punched_at", "more than 24 hours after punch_in")
    d = derive(emp, rec["date"], rec["status"], pin, ts)
    updated = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None},  # only the first closer matches
        {"$set": {"punch_out": ts, "work_hours": d["work_hours"], "overtime_minutes": d["overtime_minutes"], "half_day": d["half_day"]}},
        return_document=True,
    )
    if updated is None:
        raise HTTPException(409, "already punched out")
    return record_out(updated)


def attendance_filter(emp_code, date_from, date_to, status) -> dict:
    q: dict = {}
    if emp_code:
        q["emp_code"] = emp_code
    lo = parse_date(date_from, "date_from") if date_from else None
    hi = parse_date(date_to, "date_to") if date_to else None
    if lo and hi and lo > hi:
        bad("query", "date_from", "date_from must not be after date_to")
    if lo or hi:
        q["date"] = {}
        if lo:
            q["date"]["$gte"] = lo.isoformat()
        if hi:
            q["date"]["$lte"] = hi.isoformat()
    if status:
        q["status"] = status
    return q


ATTENDANCE_SORT = {"date": -1, "emp_code": 1}


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = attendance_filter(emp_code, date_from, date_to, status)
    total = db.attendance_logs.count_documents(q)
    cur = db.attendance_logs.find(q, {"_id": 0}).sort(list(ATTENDANCE_SORT.items())).skip((page - 1) * page_size).limit(page_size)
    return {"items": [record_out(d) for d in cur], "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: RegularizeIn):
    parse_date(date, "date", "path")
    emp = get_employee_or_404(emp_code)
    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not rec:
        raise HTTPException(404, "no attendance record for that date")

    old_status, old_in, old_out = rec["status"], rec.get("punch_in"), rec.get("punch_out")
    new_status = body.status or old_status
    if new_status in NON_PRESENCE:
        if body.punch_in is not None or body.punch_out is not None:
            bad("body", "punch_in", "punch times cannot be supplied with ABSENT or LEAVE")
        new_in = new_out = None
    else:
        new_in = ms_to_dt(body.punch_in) if body.punch_in is not None else old_in
        if new_in is None:
            bad("body", "punch_in", "a presence status requires a punch_in")
        if body.punch_in is not None and attendance_date(new_in, emp["shift_start"], emp["shift_end"]) != date:
            bad("body", "punch_in", "punch_in must stay on the record's attendance date")
        new_out = ms_to_dt(body.punch_out) if body.punch_out is not None else old_out
        if new_out is not None and (new_out <= new_in or new_out - new_in > timedelta(hours=24)):
            bad("body", "punch_out", "must be after punch_in and within 24 hours")

    new_vals = {"status": new_status, "punch_in": new_in, "punch_out": new_out, **derive(emp, date, new_status, new_in, new_out)}
    old_vals = {
        "status": old_status, "punch_in": old_in, "punch_out": old_out,
        "work_hours": rec.get("work_hours"), "late_minutes": rec.get("late_minutes") or 0,
        "overtime_minutes": rec.get("overtime_minutes") or 0, "half_day": bool(rec.get("half_day") or False),
    }
    changes = {k: {"from": old_vals[k], "to": v} for k, v in new_vals.items() if old_vals[k] != v}
    if not changes:
        bad("body", "status", "request changes nothing")

    history = rec.get("history")
    guard = {
        "_id": rec["_id"], "status": old_status, "punch_in": old_in, "punch_out": old_out,
        "history": {"$size": len(history)} if history is not None else {"$exists": False},
    }
    entry = {"at": now_utc(), "by": body.regularized_by, "reason": body.reason, "changes": changes}
    updated = db.attendance_logs.find_one_and_update(
        guard, {"$set": new_vals, "$push": {"history": entry}}, return_document=True
    )
    if updated is None:  # somebody edited the record between our read and write: never overwrite their history
        raise HTTPException(409, "record was modified concurrently, retry")
    return record_out(updated)


# --------------------------------------------------------------------------- #
# Analytics: pipeline builders (shared by the endpoints and /admin/explain)
# --------------------------------------------------------------------------- #
IS_PRESENT = {"$in": ["$status", PRESENCE]}
HALF = {"$eq": [{"$ifNull": ["$half_day", False]}, True]}
DAY_WEIGHT = {"$cond": [IS_PRESENT, {"$cond": [HALF, 0.5, 1]}, 0]}  # presence weight, any day
IS_WEEKDAY = {"$lte": [{"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}}}, 5]}
WEEKDAY_WEIGHT = {"$cond": [IS_WEEKDAY, DAY_WEIGHT, 0]}                                          # R7: Mon-Fri only
HAS_HOURS = {"$and": [IS_PRESENT, {"$gt": [{"$ifNull": ["$work_hours", None]}, None]}]}
LATE = {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}


def half_up_expr(x: Any, places: int) -> dict:
    """Half-up rounding inside the pipeline ($round would be banker's)."""
    m = 10 ** places
    return {"$divide": [{"$floor": {"$add": [{"$multiply": [x, m]}, 0.5000000001]}}, m]}


def month_stats_group() -> dict:
    return {
        "$group": {
            "_id": None,
            "present_days": {"$sum": WEEKDAY_WEIGHT},
            "wh_sum": {"$sum": {"$cond": [HAS_HOURS, {"$toDecimal": "$work_hours"}, 0]}},
            "wh_n": {"$sum": {"$cond": [HAS_HOURS, 1, 0]}},
            "late_count": {"$sum": {"$cond": [LATE, 1, 0]}},
            "total_late": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "total_ot": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
            "leave": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "on_duty": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
        }
    }


def monthly_pipeline(emp_code: str, month: str) -> tuple[list, dict]:
    start, end, *_ = month_bounds(month)
    return [{"$match": {"emp_code": emp_code, "date": {"$gte": start, "$lte": end}}}, month_stats_group()], {"hint": {"emp_code": 1, "date": 1}}


def summary_pipeline(month: str, department: Optional[str]) -> tuple[list, dict]:
    start, end, *_ = month_bounds(month)
    match: dict = {"joined_on": {"$lte": end}}  # R9: headcount counts everyone who has joined, logs or not
    if department:
        match["department"] = department
    first = lambda f: {"$ifNull": [{"$first": f"$s.{f}"}, 0]}  # noqa: E731
    return [
        {"$match": match},
        {"$lookup": {
            "from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code",
            "pipeline": [{"$match": {"date": {"$gte": start, "$lte": end}}}, month_stats_group()],
            "as": "s",
        }},  # employees with no logs get s = [] and still reach the $group below
        {"$group": {
            "_id": "$department",
            "headcount": {"$sum": 1},
            "present_days": {"$sum": first("present_days")},
            "wh_sum": {"$sum": first("wh_sum")},
            "wh_n": {"$sum": first("wh_n")},
            "late_count": {"$sum": first("late_count")},
            "total_late": {"$sum": first("total_late")},
            "leave": {"$sum": first("leave")},
            "on_duty": {"$sum": first("on_duty")},
        }},
        {"$sort": {"_id": 1}},
    ], {"hint": {"department": 1, "joined_on": 1} if department else {"joined_on": 1}}


def leaderboard_pipeline(month: str, limit: int, department: Optional[str]) -> tuple[list, dict]:
    start, end, *_ = month_bounds(month)
    pipe: list = [
        {"$match": {"date": {"$gte": start, "$lte": end}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "e"}},
        {"$unwind": "$e"},  # logs of unknown employees disappear here
    ]
    if department:
        pipe.append({"$match": {"e.department": department}})  # rank inside the department only
    pipe += [
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},  # 1,2,2,4
        {"$match": {"rank": {"$lte": limit}}},  # limit applies after ranking
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$e.name", "department": "$e.department",
                      "total_late_minutes": 1, "late_count": 1}},
    ]
    return pipe, {"hint": {"date": -1, "emp_code": 1}}


def trend_pipeline(department: str, d_from: date_cls, d_to: date_cls) -> tuple[list, dict]:
    lo, hi = d_from.isoformat(), d_to.isoformat()
    lo_dt = datetime(d_from.year, d_from.month, d_from.day, tzinfo=UTC)
    hi_dt = (
        datetime.max.replace(tzinfo=UTC)
        if d_to == date_cls.max
        else datetime(d_to.year, d_to.month, d_to.day, tzinfo=UTC) + timedelta(days=1)
    )  # bounds are [lo, hi)
    dept = {"$literal": department}
    rate = {"$cond": [{"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                      half_up_expr({"$divide": ["$present_count", "$headcount"]}, 4), None]}
    return [
        {"$match": {"department": department}},
        {"$lookup": {
            "from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code",
            "pipeline": [
                {"$match": {"date": {"$gte": lo, "$lte": hi}}},
                {"$project": {"_id": 0, "date": 1, "w": DAY_WEIGHT, "late": {"$cond": [LATE, 1, 0]}}},
            ],
            "as": "logs",
        }},
        {"$unwind": "$logs"},
        {"$group": {"_id": "$logs.date", "present_count": {"$sum": "$logs.w"}, "late_count": {"$sum": "$logs.late"}}},
        # A literal sentinel guarantees $densify has an anchor even when the range has no logs.
        {"$unionWith": {"pipeline": [
            {"$documents": [{"_id": lo, "present_count": 0, "late_count": 0}]}
        ]}},
        {"$group": {"_id": "$_id", "present_count": {"$sum": "$present_count"}, "late_count": {"$sum": "$late_count"}}},
        {"$addFields": {"dt": {"$dateFromString": {"dateString": "$_id", "format": "%Y-%m-%d"}}}},
        {"$densify": {"field": "dt", "range": {"step": 1, "unit": "day", "bounds": [lo_dt, hi_dt]}}},
        {"$addFields": {
            "date": {"$dateToString": {"date": "$dt", "format": "%Y-%m-%d"}},
            "is_working_day": {"$lte": [{"$isoDayOfWeek": "$dt"}, 5]},
            "present_count": {"$ifNull": ["$present_count", 0]},
            "late_count": {"$ifNull": ["$late_count", 0]},
        }},
        {"$lookup": {
            "from": "employees", "let": {"d": "$date"},
            "pipeline": [{"$match": {"$expr": {"$and": [{"$eq": ["$department", dept]}, {"$lte": ["$joined_on", "$$d"]}]}}},
                         {"$count": "n"}],
            "as": "hc",
        }},
        {"$addFields": {"headcount": {"$ifNull": [{"$first": "$hc.n"}, 0]}}},
        {"$addFields": {"rate": rate}},
        {"$sort": {"dt": 1}},
        {"$setWindowFields": {"sortBy": {"dt": 1}, "output": {"ma": {"$avg": "$rate", "window": {"documents": [-6, 0]}}}}},
        {"$addFields": {"ma": {"$cond": [{"$eq": ["$ma", None]}, None, half_up_expr("$ma", 4)]}}},
        {"$sort": {"dt": 1}},
        {"$project": {"_id": 0, "date": 1, "is_working_day": 1, "headcount": 1, "present_count": 1, "late_count": 1,
                      "attendance_rate": "$rate", "moving_avg_7d": "$ma"}},
    ], {"hint": {"department": 1, "joined_on": 1}}


def run_agg(collection: str, built: tuple[list, dict]) -> list:
    pipeline, opts = built
    return list(db[collection].aggregate(pipeline, **opts))


# --------------------------------------------------------------------------- #
# Analytics endpoints
# --------------------------------------------------------------------------- #
def weekdays_between(a: date_cls, b: date_cls) -> int:
    days = (b - a).days + 1
    if days <= 0:
        return 0
    full_weeks, remainder = divmod(days, 7)
    return full_weeks * 5 + sum((a.weekday() + offset) % 7 < 5 for offset in range(remainder))


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str):
    _, _, first, last = month_bounds(month)
    emp = get_employee_or_404(emp_code)
    rows = run_agg("attendance_logs", monthly_pipeline(emp_code, month))
    s = rows[0] if rows else {}
    joined = date_cls.fromisoformat(emp["joined_on"])
    working_days = weekdays_between(max(first, joined), last) if joined <= last else 0  # R7
    present = Decimal(str(s.get("present_days", 0)))
    pct = float((present / working_days * 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)) if working_days else None
    return {
        "emp_code": emp_code, "month": month, "working_days": working_days,
        "present_days": half_up(present, 2), "leave_days": int(s.get("leave", 0)),
        "late_count": int(s.get("late_count", 0)), "total_late_minutes": int(s.get("total_late", 0)),
        "total_overtime_minutes": int(s.get("total_ot", 0)), "attendance_pct": pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(month: str, department: Optional[str] = None):
    rows = run_agg("employees", summary_pipeline(month, department))
    items = []
    for r in rows:
        n = int(r["wh_n"])
        avg = (to_decimal(r["wh_sum"]) / n).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) if n else None
        items.append({
            "department": r["_id"], "headcount": int(r["headcount"]), "present_days": half_up(r["present_days"], 2),
            "avg_work_hours": float(avg) if avg is not None else None, "late_count": int(r["late_count"]),
            "total_late_minutes": int(r["total_late"]), "leave_count": int(r["leave"]), "on_duty_count": int(r["on_duty"]),
        })
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str, limit: int = Query(10, ge=1, le=50), department: Optional[str] = None):
    rows = run_agg("attendance_logs", leaderboard_pipeline(month, limit, department))
    return {"month": month, "items": [
        {"rank": int(r["rank"]), "emp_code": r["emp_code"], "name": r["name"], "department": r["department"],
         "total_late_minutes": int(r["total_late_minutes"]), "late_count": int(r["late_count"])} for r in rows]}


def trend_params(department: str, from_: Optional[str], to: Optional[str]) -> tuple[date_cls, date_cls]:
    d_from = parse_date(require(from_, "from"), "from")
    d_to = parse_date(require(to, "to"), "to")
    if d_to < d_from:
        bad("query", "to", "to must not be before from")
    if (d_to - d_from).days + 1 > 92:
        bad("query", "to", "range must not exceed 92 days")
    return d_from, d_to


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_: Optional[str] = Query(None, alias="from"), to: Optional[str] = None):
    d_from, d_to = trend_params(department, from_, to)
    if db.employees.count_documents({"department": department}, limit=1) == 0:
        raise HTTPException(404, "unknown department")
    rows = run_agg("employees", trend_pipeline(department, d_from, d_to))
    return {"department": department, "items": [{
        "date": r["date"], "is_working_day": bool(r["is_working_day"]), "headcount": int(r["headcount"]),
        "present_count": num(r["present_count"]), "late_count": int(r["late_count"]),
        "attendance_rate": half_up(r["attendance_rate"], 4) if r.get("attendance_rate") is not None else None,
        "moving_avg_7d": half_up(r["moving_avg_7d"], 4) if r.get("moving_avg_7d") is not None else None,
    } for r in rows]}


# --------------------------------------------------------------------------- #
# Admin: explain
# --------------------------------------------------------------------------- #
@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"],
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    from_: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if endpoint == "attendance_list":
        q = attendance_filter(emp_code, date_from, date_to, status)
        coll = "attendance_logs"
        cmd: dict = {"find": coll, "filter": q, "sort": dict(ATTENDANCE_SORT), "skip": (page - 1) * page_size, "limit": page_size}
    else:
        if endpoint == "employee_monthly":
            built, coll = monthly_pipeline(require(emp_code, "emp_code"), require(month, "month")), "attendance_logs"
        elif endpoint == "department_summary":
            built, coll = summary_pipeline(require(month, "month"), department), "employees"
        elif endpoint == "late_leaderboard":
            built, coll = leaderboard_pipeline(require(month, "month"), limit, department), "attendance_logs"
        else:
            d_from, d_to = trend_params(require(department, "department"), from_, to)
            built, coll = trend_pipeline(department, d_from, d_to), "employees"
        pipeline, opts = built
        cmd = {"aggregate": coll, "pipeline": pipeline, "cursor": {}, **opts}
    result = db.command({"explain": cmd, "verbosity": "executionStats"})
    return {"endpoint": endpoint, "collection": coll, "explain": json.loads(json_util.dumps(result))}
