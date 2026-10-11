"""
Employee Attendance & Analytics API
Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded; real environment variables win)
"""
import calendar
import json
import os
import random
import re
from contextlib import asynccontextmanager
from datetime import date as Date, datetime, timedelta, timezone, time
from decimal import Decimal, ROUND_HALF_UP
from time import sleep
from typing import Annotated, Literal, Optional

from bson import json_util
from bson.decimal128 import Decimal128
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, StrictInt, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()  # real environment variables are NOT overridden, so they win

client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,                    # datetimes come back with a time zone (UTC)
    serverSelectionTimeoutMS=5000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

GRACE_SECONDS = 10 * 60          # R2: grace period
MIN_OVERTIME_MINUTES = 30        # R3: overtime only counts from 30 min
HALF_DAY_HOURS = 4.50            # R5: below this is a half day


# --------------------------------------------------------------------------- #
# Startup: indexes
# --------------------------------------------------------------------------- #
def create_indexes():
    """Safe to run on every start: creating an index that already exists does nothing."""
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="emp_code_unique")
    db.employees.create_index(
        [("department", ASCENDING), ("emp_code", ASCENDING)], name="dept_emp_code"
    )
    db.employees.create_index([("joined_on", ASCENDING)], name="joined_on_idx")
    db.employees.create_index(
        [("department", ASCENDING), ("joined_on", ASCENDING)], name="dept_joined_on"
    )
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="emp_date_unique"
    )
    db.attendance_logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)], name="date_desc_emp"
    )
    db.attendance_logs.create_index(
        [("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)],
        name="status_date_emp",
    )
    db.attendance_logs.create_index(                      # finds the open record at punch-out
        [("emp_code", ASCENDING), ("punch_in", DESCENDING)], name="emp_punch_in"
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    create_indexes()
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Helpers: time and the business rules R1 - R5
# --------------------------------------------------------------------------- #
def to_ist(dt: datetime) -> datetime:
    """Any datetime -> IST, with no fractions of a second (R1)."""
    if dt.tzinfo is None:                      # treat a naive time as UTC
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(IST).replace(microsecond=0)


def to_millis(dt: Optional[datetime]) -> Optional[int]:
    """datetime -> epoch milliseconds (exact integer maths, no float errors)."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (dt - EPOCH) // timedelta(milliseconds=1)


def from_millis(ms: int) -> datetime:
    return EPOCH + timedelta(milliseconds=ms)


def shift_window(punch_in, shift_start: str, shift_end: str):
    """Returns (attendance_date, shift_start_dt, shift_end_dt) per R1."""
    p = to_ist(punch_in)
    sh, sm = map(int, shift_start.split(":"))
    eh, em = map(int, shift_end.split(":"))
    overnight = (eh, em) <= (sh, sm)
    day = p.date()
    if overnight and p.time() < time(eh, em):  # early-morning punch-in
        day -= timedelta(days=1)               # belongs to yesterday's shift
    start = datetime(day.year, day.month, day.day, sh, sm, tzinfo=IST)
    end = datetime(day.year, day.month, day.day, eh, em, tzinfo=IST)
    if overnight:
        end += timedelta(days=1)               # shift ends next morning
    return day.isoformat(), start, end


def compute_late_minutes(punch_in, shift_start_dt) -> int:
    seconds = int((to_ist(punch_in) - shift_start_dt).total_seconds())
    return seconds // 60 if seconds > GRACE_SECONDS else 0


def compute_overtime(punch_out, shift_end_dt) -> int:
    minutes = int((to_ist(punch_out) - shift_end_dt).total_seconds()) // 60
    return minutes if minutes >= MIN_OVERTIME_MINUTES else 0


def compute_work_hours(punch_in, punch_out) -> float:
    seconds = int((to_ist(punch_out) - to_ist(punch_in)).total_seconds())
    hours = Decimal(seconds) / Decimal(3600)
    return float(hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def derive(status: str, punch_in, punch_out, emp: dict) -> dict:
    """The four stored derived values for a record (R2 - R5, see DATA_MODEL.md)."""
    if status in ("ABSENT", "LEAVE") or punch_in is None:
        return {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    _, shift_start_dt, shift_end_dt = shift_window(punch_in, emp["shift_start"], emp["shift_end"])
    late = compute_late_minutes(punch_in, shift_start_dt)
    if punch_out is None:
        return {"work_hours": None, "late_minutes": late, "overtime_minutes": 0, "half_day": False}
    hours = compute_work_hours(punch_in, punch_out)
    return {
        "work_hours": hours,
        "late_minutes": late,
        "overtime_minutes": compute_overtime(punch_out, shift_end_dt),
        "half_day": hours < HALF_DAY_HOURS,
    }


# --------------------------------------------------------------------------- #
# Helpers: turning database documents into API responses
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
        "created_at": to_millis(doc.get("created_at")),
    }


def _history_value(key: str, value):
    return to_millis(value) if key in ("punch_in", "punch_out") else value


def history_out(entry: dict) -> dict:
    return {
        "at": to_millis(entry["at"]),
        "by": entry["by"],
        "reason": entry["reason"],
        "changes": {
            key: {
                "from": _history_value(key, change.get("from")),
                "to": _history_value(key, change.get("to")),
            }
            for key, change in entry.get("changes", {}).items()
        },
    }


def record_out(doc: dict) -> dict:
    """Never exposes _id. Older records may lack some fields, so use defaults."""
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": to_millis(doc.get("punch_in")),
        "punch_out": to_millis(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": [history_out(h) for h in doc.get("history", [])],
    }


def unprocessable(field: str, message: str, where: str = "query") -> HTTPException:
    return HTTPException(
        422, [{"loc": [where, field], "msg": message, "type": "value_error"}]
    )


DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def is_valid_date(value: str) -> bool:
    if not DATE_RE.match(value):
        return False
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        return False
    return True


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
EpochMillis = Annotated[StrictInt, Field(ge=100_000_000_000, le=4_102_444_800_000)]
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
HHMM = r"^([01]\d|2[0-3]):[0-5]\d$"


class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=HHMM)
    shift_end: str = Field("18:30", pattern=HHMM)
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def joined_on_is_a_date(cls, v: str) -> str:
        if not is_valid_date(v):
            raise ValueError("must be a real date in YYYY-MM-DD format")
        return v

    @model_validator(mode="after")
    def shifts_must_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMillis] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMillis] = None


class RegularizeIn(BaseModel):
    status: Optional[Status] = None
    punch_in: Optional[EpochMillis] = None
    punch_out: Optional[EpochMillis] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def no_explicit_nulls(self):
        for name in ("status", "punch_in", "punch_out"):
            if name in self.model_fields_set and getattr(self, name) is None:
                raise ValueError(f"{name} must not be null")
        return self


# --------------------------------------------------------------------------- #
# Endpoints
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
    doc["created_at"] = datetime.now(timezone.utc)
    try:
        db.employees.insert_one(doc)          # the unique index decides who wins a race
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return employee_out(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {}
    if department:
        q["department"] = department
    total = db.employees.count_documents(q)   # counts the FILTERED set
    cursor = (
        db.employees.find(q, {"_id": 0})
        .sort("emp_code", ASCENDING)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {
        "items": [employee_out(d) for d in cursor],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")

    if body.punched_at is not None:
        ts = from_millis(body.punched_at)
    else:
        ts = datetime.now(timezone.utc)
    ts = ts.replace(microsecond=0)            # R1: whole seconds

    day, shift_start_dt, _ = shift_window(ts, emp["shift_start"], emp["shift_end"])
    doc = {
        "emp_code": body.emp_code,
        "date": day,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, shift_start_dt),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)    # unique (emp_code, date) index decides the race
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return record_out(doc)


def build_attendance_query(emp_code, date_from, date_to, status) -> dict:
    for name, value in (("date_from", date_from), ("date_to", date_to)):
        if value is not None and not is_valid_date(value):
            raise unprocessable(name, "must be a real date in YYYY-MM-DD format")
    if date_from and date_to and date_from > date_to:
        raise unprocessable("date_from", "date_from must not be after date_to")
    q = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status
    return q


def attendance_list_plan(emp_code, date_from, date_to, status, page, page_size) -> dict:
    """Everything the list query needs. Shared by the endpoint and the explain endpoint."""
    if emp_code:
        hint = "emp_date_unique"
    elif status:
        hint = "status_date_emp"
    else:
        hint = "date_desc_emp"
    return {
        "filter": build_attendance_query(emp_code, date_from, date_to, status),
        "sort": [("date", DESCENDING), ("emp_code", ASCENDING)],
        "skip": (page - 1) * page_size,
        "limit": page_size,
        "hint": hint,
    }


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    plan = attendance_list_plan(emp_code, date_from, date_to, status, page, page_size)
    total = db.attendance_logs.count_documents(plan["filter"])
    cursor = (
        db.attendance_logs.find(plan["filter"], {"_id": 0})
        .sort(plan["sort"])                                   # the database sorts
        .hint(plan["hint"])
        .skip(plan["skip"])
        .limit(plan["limit"])
    )
    return {
        "items": [record_out(d) for d in cursor],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")

    if body.punched_at is not None:
        ts = from_millis(body.punched_at)
    else:
        ts = datetime.now(timezone.utc)
    ts = ts.replace(microsecond=0)

    # The employee's most recent record that started at or before this moment.
    # Searching by punch_in (not by date) is what makes overnight shifts work.
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts}},
        sort=[("punch_in", DESCENDING)],
    )
    if rec is None:
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")

    if ts <= rec["punch_in"]:
        raise unprocessable("punched_at", "punched_at must be after the punch-in", "body")
    if ts - rec["punch_in"] > timedelta(hours=24):
        raise unprocessable("punched_at", "punched_at is more than 24 hours after the punch-in", "body")

    values = derive(rec["status"], rec["punch_in"], ts, emp)
    values["punch_out"] = ts
    # Atomic: only succeeds if the record is STILL open. Of two simultaneous
    # punch-outs exactly one matches this filter; the other gets None back.
    updated = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None},
        {"$set": values},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "already punched out")
    return record_out(updated)


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: RegularizeIn):
    if not is_valid_date(date):
        raise unprocessable("date", "must be a real date in YYYY-MM-DD format", "path")
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")

    # If someone else saves the same record between our read and our write, we read it
    # again and redo the work on top of THEIR result, so no history entry is ever lost.
    for _attempt in range(10):
        rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
        if rec is None:
            raise HTTPException(404, "no attendance record for that date")

        # ---- 1. work out the new status and punch times ---------------------
        new_status = body.status or rec["status"]
        if new_status in ("ABSENT", "LEAVE"):
            if body.punch_in is not None or body.punch_out is not None:
                raise unprocessable("status", "ABSENT/LEAVE cannot have punch times", "body")
            new_in, new_out = None, None
        else:
            new_in = (
                from_millis(body.punch_in).replace(microsecond=0)
                if body.punch_in is not None else rec.get("punch_in")
            )
            new_out = (
                from_millis(body.punch_out).replace(microsecond=0)
                if body.punch_out is not None else rec.get("punch_out")
            )
            if new_in is None:
                raise unprocessable("punch_in", "a presence status needs a punch_in", "body")
            if shift_window(new_in, emp["shift_start"], emp["shift_end"])[0] != date:
                raise unprocessable("punch_in", "punch_in must stay on the record's attendance date", "body")
            if new_out is not None:
                if new_out <= new_in:
                    raise unprocessable("punch_out", "punch_out must be after punch_in", "body")
                if new_out - new_in > timedelta(hours=24):
                    raise unprocessable("punch_out", "punch_out is more than 24 hours after punch_in", "body")

        # ---- 2. did the request change anything? ---------------------------
        if (new_status, new_in, new_out) == (rec["status"], rec.get("punch_in"), rec.get("punch_out")):
            raise unprocessable("body", "the request changes nothing", "body")

        # ---- 3. recompute the derived fields and list what really changed --
        new_values = {"status": new_status, "punch_in": new_in, "punch_out": new_out}
        new_values.update(derive(new_status, new_in, new_out, emp))
        old_values = {
            "status": rec["status"],
            "punch_in": rec.get("punch_in"),
            "punch_out": rec.get("punch_out"),
            "work_hours": rec.get("work_hours"),
            "late_minutes": rec.get("late_minutes", 0),
            "overtime_minutes": rec.get("overtime_minutes", 0),
            "half_day": rec.get("half_day", False),
        }
        changes = {
            key: {"from": old_values[key], "to": new_values[key]}
            for key in new_values if new_values[key] != old_values[key]
        }
        entry = {
            "at": datetime.now(timezone.utc),
            "by": body.regularized_by,
            "reason": body.reason,
            "changes": changes,
        }

        # ---- 4. save, but only if nobody changed the record since we read it
        n = len(rec.get("history", []))
        updated = db.attendance_logs.find_one_and_update(
            {
                "_id": rec["_id"],
                "status": rec["status"],
                "punch_in": rec.get("punch_in"),
                "punch_out": rec.get("punch_out"),
                f"history.{n}": {"$exists": False},   # nobody appended an entry meanwhile
            },
            {"$set": new_values, "$push": {"history": entry}},
            return_document=ReturnDocument.AFTER,
        )
        if updated is not None:
            return record_out(updated)
        sleep(random.uniform(0.005, 0.03))            # lost the race: wait a moment, then retry

    raise HTTPException(409, "the record is being changed by others; please retry")


# --------------------------------------------------------------------------- #
# Analytics helpers
# --------------------------------------------------------------------------- #
MONTH_RE = r"^\d{4}-(0[1-9]|1[0-2])$"
PRESENCE = ["PRESENT", "WFH", "ON_DUTY"]               # R6


def month_bounds(month: str):
    """'2026-09' -> (date(2026,9,1), date(2026,9,30))."""
    year, mon = int(month[:4]), int(month[5:7])
    if year < 1:
        raise unprocessable("month", "year must be 0001 or later")
    return Date(year, mon, 1), Date(year, mon, calendar.monthrange(year, mon)[1])


def count_weekdays(start: Date, end: Date) -> int:
    """Mon - Fri days from start to end inclusive (R7). At most 31 steps."""
    days = (end - start).days + 1
    return sum(1 for i in range(max(days, 0)) if (start + timedelta(days=i)).weekday() < 5)


def to_decimal(value) -> Decimal:
    if isinstance(value, Decimal128):
        return value.to_decimal()
    return Decimal(str(value))


def half_up(value: Decimal, places: int) -> float:
    """R8: round half UP (Python's round() and MongoDB's $round are not)."""
    return float(value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


def present_value(prefix: str) -> dict:
    """What ONE record adds to present_days: 1 (0.5 if half day), only for a
    presence status on Mon - Fri; otherwise 0.  prefix is '$' for a document
    field or '$$l.' for an element of a looked-up array."""
    weekday = {"$lte": [{"$isoDayOfWeek": {"$dateFromString": {"dateString": prefix + "date"}}}, 5]}
    return {
        "$cond": [
            {"$and": [{"$in": [prefix + "status", PRESENCE]}, weekday]},
            {"$cond": [{"$ifNull": [prefix + "half_day", False]}, 0.5, 1]},
            0,
        ]
    }


def run_plan(plan):
    collection, pipeline, hint = plan
    return list(db[collection].aggregate(pipeline, hint=hint))


# --------------------------------------------------------------------------- #
# Analytics: employee monthly summary
# --------------------------------------------------------------------------- #
def employee_monthly_plan(emp_code: str, first: str, last: str):
    pipeline = [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first, "$lte": last}}},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": present_value("$")},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ]
    return "attendance_logs", pipeline, "emp_date_unique"


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=MONTH_RE)):
    first, last = month_bounds(month)
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")

    rows = run_plan(employee_monthly_plan(emp_code, first.isoformat(), last.isoformat()))
    s = rows[0] if rows else {}

    joined = Date.fromisoformat(emp["joined_on"])
    working_days = count_weekdays(max(first, joined), last)       # R7
    present = to_decimal(s.get("present_days", 0))
    pct = half_up(present / working_days * 100, 2) if working_days else None
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": half_up(present, 2),
        "leave_days": s.get("leave_days", 0),
        "late_count": s.get("late_count", 0),
        "total_late_minutes": s.get("total_late_minutes", 0),
        "total_overtime_minutes": s.get("total_overtime_minutes", 0),
        "attendance_pct": pct,
    }


# --------------------------------------------------------------------------- #
# Analytics: department summary (employees + attendance_logs)
# --------------------------------------------------------------------------- #
def department_summary_plan(first: str, last: str, department: Optional[str]):
    match = {"joined_on": {"$lte": last}}                         # R9: joined by month end
    if department:
        match["department"] = department

    def over_logs(cond):                                           # logs that satisfy cond
        return {"$filter": {"input": "$logs", "as": "l", "cond": cond}}

    has_hours = {"$and": [{"$in": ["$$l.status", PRESENCE]}, {"$isNumber": "$$l.work_hours"}]}
    late = {"$ifNull": ["$$l.late_minutes", 0]}

    pipeline = [
        {"$match": match},                                         # starts from EMPLOYEES
        {"$lookup": {                                              # so no-log people stay
            "from": "attendance_logs",
            "localField": "emp_code",
            "foreignField": "emp_code",
            "pipeline": [{"$match": {"date": {"$gte": first, "$lte": last}}}],
            "as": "logs",
        }},
        {"$project": {                                             # one row per employee
            "_id": 0,
            "department": 1,
            "present_days": {"$sum": {"$map": {"input": "$logs", "as": "l", "in": present_value("$$l.")}}},
            "late_count": {"$size": over_logs({"$gt": [late, 0]})},
            "total_late_minutes": {"$sum": {"$map": {"input": "$logs", "as": "l", "in": late}}},
            "leave_count": {"$size": over_logs({"$eq": ["$$l.status", "LEAVE"]})},
            "on_duty_count": {"$size": over_logs({"$eq": ["$$l.status", "ON_DUTY"]})},
            "wh_sum": {"$sum": {"$map": {
                "input": "$logs", "as": "l",
                "in": {"$cond": [has_hours, {"$toDecimal": "$$l.work_hours"}, 0]},
            }}},
            "wh_count": {"$size": over_logs(has_hours)},
        }},
        {"$group": {                                               # one row per department
            "_id": "$department",
            "headcount": {"$sum": 1},
            "present_days": {"$sum": "$present_days"},
            "late_count": {"$sum": "$late_count"},
            "total_late_minutes": {"$sum": "$total_late_minutes"},
            "leave_count": {"$sum": "$leave_count"},
            "on_duty_count": {"$sum": "$on_duty_count"},
            "wh_sum": {"$sum": "$wh_sum"},
            "wh_count": {"$sum": "$wh_count"},
        }},
        {"$sort": {"_id": 1}},
    ]
    return "employees", pipeline, ("dept_joined_on" if department else "joined_on_idx")


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=MONTH_RE),
    department: Optional[str] = None,
):
    first, last = month_bounds(month)
    rows = run_plan(department_summary_plan(first.isoformat(), last.isoformat(), department))
    items = []
    for r in rows:
        avg = None
        if r["wh_count"]:                       # average over RECORDS, not over employees
            avg = half_up(to_decimal(r["wh_sum"]) / r["wh_count"], 2)
        items.append({
            "department": r["_id"],
            "headcount": r["headcount"],
            "present_days": half_up(to_decimal(r["present_days"]), 2),
            "avg_work_hours": avg,
            "late_count": r["late_count"],
            "total_late_minutes": r["total_late_minutes"],
            "leave_count": r["leave_count"],
            "on_duty_count": r["on_duty_count"],
        })
    return {"month": month, "items": items}


# --------------------------------------------------------------------------- #
# Analytics: late leaderboard (standard competition ranking)
# --------------------------------------------------------------------------- #
def late_leaderboard_plan(first: str, last: str, department: Optional[str], limit: int):
    late = {"$ifNull": ["$late_minutes", 0]}
    pipeline = [
        {"$match": {"date": {"$gte": first, "$lte": last}}},
        {"$group": {
            "_id": "$emp_code",
            "total_late_minutes": {"$sum": late},
            "late_count": {"$sum": {"$cond": [{"$gt": [late, 0]}, 1, 0]}},
        }},
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {"$lookup": {"from": "employees", "localField": "_id",
                     "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},                    # drops logs whose employee does not exist
    ]
    if department:
        pipeline.append({"$match": {"emp.department": department}})   # BEFORE ranking
    pipeline += [
        {"$setWindowFields": {                  # $rank = 1, 2, 2, 4 (ties share, next skipped)
            "sortBy": {"total_late_minutes": -1},
            "output": {"rank": {"$rank": {}}},
        }},
        {"$match": {"rank": {"$lte": limit}}},  # limit AFTER ranking
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {
            "_id": 0, "rank": 1, "emp_code": "$_id", "name": "$emp.name",
            "department": "$emp.department", "total_late_minutes": 1, "late_count": 1,
        }},
    ]
    return "attendance_logs", pipeline, "date_desc_emp"


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=MONTH_RE),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    first, last = month_bounds(month)
    items = run_plan(late_leaderboard_plan(first.isoformat(), last.isoformat(), department, limit))
    return {"month": month, "items": items}


# --------------------------------------------------------------------------- #
# Analytics: department trend (gap filling + 7-day moving average)
# --------------------------------------------------------------------------- #
def half_up_expr(expr, places: int) -> dict:
    """Round half UP inside MongoDB (its own $round rounds ties to even).
    Works for non-negative numbers, which is all we have here."""
    scale = 10 ** places
    return {"$divide": [{"$floor": {"$add": [{"$multiply": [expr, scale]}, 0.5]}}, scale]}


def parse_trend_range(date_from: str, date_to: str):
    for name, value in (("from", date_from), ("to", date_to)):
        if not is_valid_date(value):
            raise unprocessable(name, "must be a real date in YYYY-MM-DD format")
    start, end = Date.fromisoformat(date_from), Date.fromisoformat(date_to)
    if end < start:
        raise unprocessable("to", "to must not be before from")
    if (end - start).days + 1 > 92:
        raise unprocessable("to", "the range must not exceed 92 days")
    return start, end


def department_trend_plan(department: str, start: Date, end: Date):
    codes = db.employees.distinct("emp_code", {"department": department})
    day_first = datetime(start.year, start.month, start.day, tzinfo=timezone.utc)
    day_last = datetime(end.year, end.month, end.day, tzinfo=timezone.utc)
    present = {"$cond": [{"$in": ["$status", PRESENCE]},
                         {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]}, 0]}
    late = {"$ifNull": ["$late_minutes", 0]}
    pipeline = [
        # 1. a single seed row (the first day), taken from the department's employees
        {"$match": {"department": department}},
        {"$limit": 1},
        {"$project": {"_id": 0, "day": {"$literal": day_first}}},
        # 2. gap filling: one row for EVERY calendar day (the final $match is a safety net)
        {"$densify": {"field": "day", "range": {
            "step": 1, "unit": "day", "bounds": [day_first, day_last + timedelta(days=1)]}}},
        {"$match": {"day": {"$gte": day_first, "$lte": day_last}}},
        {"$addFields": {"date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$day"}}}},
        # 3. that day's records for the department, and that day's headcount (R9)
        {"$lookup": {
            "from": "attendance_logs", "localField": "date", "foreignField": "date",
            "pipeline": [
                {"$match": {"emp_code": {"$in": codes}}},
                {"$group": {"_id": None,
                            "present_count": {"$sum": present},
                            "late_count": {"$sum": {"$cond": [{"$gt": [late, 0]}, 1, 0]}}}},
            ],
            "as": "stats"}},
        {"$lookup": {
            "from": "employees", "let": {"d": "$date"},
            "pipeline": [
                {"$match": {"department": department, "$expr": {"$lte": ["$joined_on", "$$d"]}}},
                {"$count": "n"},
            ],
            "as": "hc"}},
        {"$addFields": {
            "present_count": {"$ifNull": [{"$arrayElemAt": ["$stats.present_count", 0]}, 0]},
            "late_count": {"$ifNull": [{"$arrayElemAt": ["$stats.late_count", 0]}, 0]},
            "headcount": {"$ifNull": [{"$arrayElemAt": ["$hc.n", 0]}, 0]},
            "is_working_day": {"$lte": [{"$isoDayOfWeek": "$day"}, 5]},
        }},
        # 4. the daily rate: only on working days that have people (otherwise null)
        {"$addFields": {"attendance_rate": {"$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            half_up_expr({"$divide": [{"$toDecimal": "$present_count"}, "$headcount"]}, 4),
            None]}}},
        # 5. moving average: this row + up to 6 earlier rows, nulls ignored
        {"$setWindowFields": {"sortBy": {"day": 1}, "output": {
            "avg_raw": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}}}},
        {"$addFields": {"moving_avg_7d": {"$cond": [
            {"$eq": [{"$type": "$avg_raw"}, "null"]}, None, half_up_expr("$avg_raw", 4)]}}},
        {"$sort": {"day": 1}},
        {"$project": {"_id": 0, "date": 1, "is_working_day": 1, "headcount": 1,
                      "present_count": 1, "late_count": 1, "attendance_rate": 1,
                      "moving_avg_7d": 1}},
    ]
    return "employees", pipeline, "dept_emp_code"


def _number_or_none(value):
    return None if value is None else float(to_decimal(value))


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    date_from: str = Query(..., alias="from"),
    date_to: str = Query(..., alias="to"),
):
    start, end = parse_trend_range(date_from, date_to)
    if db.employees.find_one({"department": department}, {"_id": 1}) is None:
        raise HTTPException(404, "unknown department")
    rows = run_plan(department_trend_plan(department, start, end))
    items = [
        {
            "date": r["date"],
            "is_working_day": r["is_working_day"],
            "headcount": r["headcount"],
            "present_count": float(to_decimal(r["present_count"])),
            "late_count": r["late_count"],
            "attendance_rate": _number_or_none(r.get("attendance_rate")),
            "moving_avg_7d": _number_or_none(r.get("moving_avg_7d")),
        }
        for r in rows
    ]
    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Part C: explain endpoint
# --------------------------------------------------------------------------- #
@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary",
                      "late_leaderboard", "department_trend"],
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=MONTH_RE),
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
    def need(name, value):
        if value is None or value == "":
            raise unprocessable(name, f"is required for {endpoint}")

    if endpoint == "attendance_list":
        plan = attendance_list_plan(emp_code, date_from, date_to, status, page, page_size)
        collection = "attendance_logs"
        command = {"find": collection, "filter": plan["filter"], "sort": dict(plan["sort"]),
                   "skip": plan["skip"], "limit": plan["limit"], "hint": plan["hint"]}
    else:
        if endpoint == "employee_monthly":
            need("emp_code", emp_code)
            need("month", month)
            first, last = month_bounds(month)
            plan = employee_monthly_plan(emp_code, first.isoformat(), last.isoformat())
        elif endpoint == "department_summary":
            need("month", month)
            first, last = month_bounds(month)
            plan = department_summary_plan(first.isoformat(), last.isoformat(), department)
        elif endpoint == "late_leaderboard":
            need("month", month)
            first, last = month_bounds(month)
            plan = late_leaderboard_plan(first.isoformat(), last.isoformat(), department, limit)
        else:  # department_trend
            need("department", department)
            need("from", from_)
            need("to", to)
            start, end = parse_trend_range(from_, to)
            plan = department_trend_plan(department, start, end)
        collection, pipeline, hint = plan
        command = {"aggregate": collection, "pipeline": pipeline, "cursor": {}, "hint": hint}

    result = db.command({"explain": command, "verbosity": "executionStats"})
    # explain contains BSON types (dates, timestamps); convert them to plain JSON
    return {"endpoint": endpoint, "collection": collection,
            "explain": json.loads(json_util.dumps(result))}