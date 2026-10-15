"""
Employee Attendance & Analytics API
===================================

A single-file FastAPI + MongoDB service that implements the HROne assignment
contract (``openapi.yaml``) and the storage model (``DATA_MODEL.md``).

Run:
    uvicorn app.main:app --port 8000
Environment (a local ``.env`` is loaded for convenience; real env vars win):
    MONGO_URI   e.g. mongodb://localhost:27017
    MONGO_DB    e.g. attendance_db
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from datetime import date as date_cls
from datetime import datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from enum import Enum
from typing import Annotated, Any, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

# --------------------------------------------------------------------------- #
# Configuration and database handle
# --------------------------------------------------------------------------- #
load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client: MongoClient = MongoClient(MONGO_URI, tz_aware=True)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))

GRACE_SECONDS = 10 * 60          # R2
OVERTIME_MIN_MINUTES = 30        # R3
HALF_DAY_HOURS = Decimal("4.50")  # R5
MAX_SHIFT_SECONDS = 24 * 3600    # punch-out
PRESENCE_STATUSES = ("PRESENT", "WFH", "ON_DUTY")
# MongoDB $dayOfWeek
WEEKDAY_DOW = [2, 3, 4, 5, 6]


# --------------------------------------------------------------------------- #
# Index management
# --------------------------------------------------------------------------- #
def ensure_indexes() -> None:
    # -- employees -------------------------------------------------------- #
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="uniq_emp_code")
    db.employees.create_index(
        [("department", ASCENDING), ("joined_on", ASCENDING)], name="dept_joined"
    )
    db.employees.create_index([("joined_on", ASCENDING)], name="joined_on")

    # -- attendance_logs -------------------------------------------------- #
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uniq_emp_date"
    )
    db.attendance_logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)], name="date_emp"
    )
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", DESCENDING)], name="emp_date_desc"
    )
    # Punch-out finds the most recent open record by punch_in for an employee.
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("punch_in", DESCENDING)], name="emp_punchin"
    )


@asynccontextmanager
async def lifespan(_app: FastAPI):
    try:
        ensure_indexes()
    except PyMongoError as exc: 
        print(f"[startup] index creation deferred: {exc}")
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Time helpers
# --------------------------------------------------------------------------- #
def ms_to_dt(ms: int) -> datetime:
    seconds = ms // 1000
    return datetime.fromtimestamp(seconds, tz=timezone.utc)


def dt_to_ms(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(round(dt.timestamp())) * 1000


def now_dt() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _is_overnight(shift_start: str, shift_end: str) -> bool:
    return shift_end <= shift_start


def _shift_instant(date_str: str, hhmm: str, day_offset: int = 0) -> datetime:
    y, m, d = (int(x) for x in date_str.split("-"))
    h, mi = (int(x) for x in hhmm.split(":"))
    base = datetime(y, m, d, h, mi, 0, tzinfo=IST)
    return base + timedelta(days=day_offset)


def attendance_date_for(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    ist = punch_in.astimezone(IST)
    if _is_overnight(shift_start, shift_end):
        se_h, se_m = (int(x) for x in shift_end.split(":"))
        if ist.time() < time(se_h, se_m):
            return (ist.date() - timedelta(days=1)).isoformat()
    return ist.date().isoformat()


def compute_late_minutes(punch_in: datetime, date_str: str, shift_start: str) -> int:
    start = _shift_instant(date_str, shift_start)
    delta = (punch_in - start).total_seconds()
    if delta > GRACE_SECONDS:
        return int(delta // 60)
    return 0


def compute_overtime_minutes(
    punch_out: datetime, date_str: str, shift_start: str, shift_end: str
) -> int:
    offset = 1 if _is_overnight(shift_start, shift_end) else 0
    end = _shift_instant(date_str, shift_end, offset)
    delta = (punch_out - end).total_seconds()
    if delta <= 0:
        return 0
    minutes = int(delta // 60)
    return minutes if minutes >= OVERTIME_MIN_MINUTES else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    seconds = int((punch_out - punch_in).total_seconds())
    hours = Decimal(seconds) / Decimal(3600)
    return float(hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def round_half_up(value: Optional[float], places: int) -> Optional[float]:
    if value is None:
        return None
    q = Decimal(1).scaleb(-places)  
    return float(Decimal(str(value)).quantize(q, rounding=ROUND_HALF_UP))


def count_working_days(first_date: date_cls, last_date: date_cls, joined: date_cls) -> int:
    start = max(first_date, joined)
    count = 0
    d = start
    while d <= last_date:
        if d.weekday() < 5:  
            count += 1
        d += timedelta(days=1)
    return count


def rank_leaderboard(rows: list[dict], limit: int) -> list[dict]:
    items: list[dict] = []
    rank = 0
    prev: Optional[int] = None
    for i, r in enumerate(rows):
        total = int(r["total_late_minutes"])
        if total != prev:
            rank = i + 1  
            prev = total
        if rank > limit:
            break 
        items.append({
            "rank": rank,
            "emp_code": r["emp_code"],
            "name": r["name"],
            "department": r["department"],
            "total_late_minutes": total,
            "late_count": int(r["late_count"]),
        })
    return items


def month_bounds(month: str) -> tuple[str, str, date_cls, date_cls]:
    y, m = (int(x) for x in month.split("-"))
    first = date_cls(y, m, 1)
    nxt = date_cls(y + 1, 1, 1) if m == 12 else date_cls(y, m + 1, 1)
    last = nxt - timedelta(days=1)
    return first.isoformat(), last.isoformat(), first, last


def parse_date_param(value: str, field: str) -> date_cls:
    try:
        return date_cls.fromisoformat(value)
    except ValueError:
        raise HTTPException(422, f"{field} must be a valid date (YYYY-MM-DD)")


# --------------------------------------------------------------------------- #
# Serialization helpers
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
        "created_at": dt_to_ms(doc["created_at"]),
    }


def _history_out(entry: dict) -> dict:
    changes: dict[str, Any] = {}
    for field, ft in entry.get("changes", {}).items():
        if field in ("punch_in", "punch_out"):
            changes[field] = {"from": dt_to_ms(ft.get("from")), "to": dt_to_ms(ft.get("to"))}
        else:
            changes[field] = {"from": ft.get("from"), "to": ft.get("to")}
    return {
        "at": dt_to_ms(entry["at"]),
        "by": entry["by"],
        "reason": entry["reason"],
        "changes": changes,
    }


def record_out(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": dt_to_ms(doc.get("punch_in")),
        "punch_out": dt_to_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": int(doc.get("late_minutes", 0) or 0),
        "overtime_minutes": int(doc.get("overtime_minutes", 0) or 0),
        "half_day": bool(doc.get("half_day", False)),
        "history": [_history_out(h) for h in doc.get("history", [])],
    }


# --------------------------------------------------------------------------- #
# Request models
# --------------------------------------------------------------------------- #
EpochMillis = Annotated[StrictInt, Field(ge=100_000_000_000, le=4_102_444_800_000)]

class Status(str, Enum):
    PRESENT = "PRESENT"
    ABSENT = "ABSENT"
    LEAVE = "LEAVE"
    WFH = "WFH"
    ON_DUTY = "ON_DUTY"

class PresenceStatus(str, Enum):
    PRESENT = "PRESENT"
    WFH = "WFH"
    ON_DUTY = "ON_DUTY"

class EmployeeCreate(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: Annotated[str, Field(pattern=r"^EMP\d{4,6}$")]
    name: Annotated[str, Field(min_length=1, max_length=100)]
    email: Annotated[str, Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")]
    department: Annotated[str, Field(min_length=1, max_length=50)]
    shift_start: Annotated[str, Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")] = "09:30"
    shift_end: Annotated[str, Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")] = "18:30"
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def _valid_joined_on(cls, v: str) -> str:
        try:
            date_cls.fromisoformat(v)
        except ValueError:
            raise ValueError("joined_on must be a valid date (YYYY-MM-DD)")
        return v

    @model_validator(mode="after")
    def _shifts_differ(self) -> "EmployeeCreate":
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end must differ")
        return self

class PunchInRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str
    punched_at: Optional[EpochMillis] = None
    status: PresenceStatus = PresenceStatus.PRESENT

class PunchOutRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str
    punched_at: Optional[EpochMillis] = None

class RegularizeRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    status: Optional[Status] = None
    punch_in: Optional[EpochMillis] = None
    punch_out: Optional[EpochMillis] = None
    reason: Annotated[str, Field(min_length=5, max_length=200)]
    regularized_by: Annotated[str, Field(min_length=1, max_length=50)]

# --------------------------------------------------------------------------- #
# Aggregation expression fragments
# --------------------------------------------------------------------------- #
def _dow_expr(date_field: str) -> dict:
    return {"$dayOfWeek": {"$dateFromString": {"dateString": date_field, "format": "%Y-%m-%d"}}}

def _present_contrib(status_f: str, date_f: str, half_f: str, weekday_only: bool) -> dict:
    """Per-record present-day contribution: 0.5 for a half day, 1 for a full
    presence day, 0 otherwise. ``weekday_only`` applies the Mon-Fri filter
    (monthly/department summaries); the trend counts any day."""
    conds: list[Any] = [{"$in": [status_f, list(PRESENCE_STATUSES)]}]
    if weekday_only:
        conds.append({"$in": [_dow_expr(date_f), WEEKDAY_DOW]})
    return {
        "$cond": [
            {"$and": conds},
            {"$cond": [{"$eq": [{"$ifNull": [half_f, False]}, True]}, 0.5, 1]},
            0,
        ]
    }

# --------------------------------------------------------------------------- #
# System
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except PyMongoError:
        raise HTTPException(503, "database unavailable")
    return {"status": "ok"}

# --------------------------------------------------------------------------- #
# Employees
# --------------------------------------------------------------------------- #
@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate):
    doc = body.model_dump()
    doc["created_at"] = now_dt()
    try:
        db.employees.insert_one(doc) 
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return employee_out(doc)

@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q: dict[str, Any] = {}
    if department is not None:
        q["department"] = department
    total = db.employees.count_documents(q)  # reflects the filter (R10)
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

# --------------------------------------------------------------------------- #
# Attendance: punch-in / punch-out
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "unknown employee")

    pin = ms_to_dt(body.punched_at) if body.punched_at is not None else now_dt()
    date_str = attendance_date_for(pin, emp["shift_start"], emp["shift_end"])
    late = compute_late_minutes(pin, date_str, emp["shift_start"])

    doc = {
        "emp_code": body.emp_code,
        "date": date_str,
        "status": body.status.value,
        "punch_in": pin,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return record_out(doc)

@app.post("/attendance/punch-out")
def punch_out(body: PunchOutRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "unknown employee")

    pout = ms_to_dt(body.punched_at) if body.punched_at is not None else now_dt()

    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$ne": None, "$lte": pout}},
        sort=[("punch_in", DESCENDING)],
    )
    if not rec:
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "record already punched out")

    pin = rec["punch_in"]
    if pout <= pin:
        raise HTTPException(422, "punch_out must be after punch_in")
    if (pout - pin).total_seconds() > MAX_SHIFT_SECONDS:
        raise HTTPException(422, "punch_out is more than 24 hours after punch_in")

    work_hours = compute_work_hours(pin, pout)
    overtime = compute_overtime_minutes(pout, rec["date"], emp["shift_start"], emp["shift_end"])
    half_day = Decimal(str(work_hours)) < HALF_DAY_HOURS

    updated = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None},  
        {"$set": {
            "punch_out": pout,
            "work_hours": work_hours,
            "overtime_minutes": overtime,
            "half_day": half_day,
        }},
        return_document=ReturnDocument.AFTER,
    )
    if not updated:
        raise HTTPException(409, "record already punched out")
    return record_out(updated)


# --------------------------------------------------------------------------- #
# Attendance: list
# --------------------------------------------------------------------------- #
def _attendance_filter(
    emp_code: Optional[str],
    date_from: Optional[str],
    date_to: Optional[str],
    status: Optional[Status],
) -> dict:
    q: dict[str, Any] = {}
    if emp_code is not None:
        q["emp_code"] = emp_code
    if date_from is not None or date_to is not None:
        rng: dict[str, Any] = {}
        if date_from is not None:
            rng["$gte"] = date_from
        if date_to is not None:
            rng["$lte"] = date_to
        q["date"] = rng
    if status is not None:
        q["status"] = status.value if isinstance(status, Status) else status
    return q


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    status: Optional[Status] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from is not None:
        parse_date_param(date_from, "date_from")
    if date_to is not None:
        parse_date_param(date_to, "date_to")
    if date_from is not None and date_to is not None and date_from > date_to:
        raise HTTPException(422, "date_from must be <= date_to")

    q = _attendance_filter(emp_code, date_from, date_to, status)
    total = db.attendance_logs.count_documents(q)
    cursor = (
        db.attendance_logs.find(q, {"_id": 0})
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {
        "items": [record_out(d) for d in cursor],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# --------------------------------------------------------------------------- #
# Attendance: regularize (manual correction + audit trail)
# --------------------------------------------------------------------------- #
@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: RegularizeRequest):
    parse_date_param(date, "date")
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "unknown employee")
    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not rec:
        raise HTTPException(404, "no record for that date")

    provided = body.model_dump(exclude_unset=True)
    new_status = body.status.value if body.status is not None else rec["status"]

    # --- figure out the final punch times ------------------------------- #
    if new_status in ("ABSENT", "LEAVE"):
        if "punch_in" in provided or "punch_out" in provided:
            raise HTTPException(422, "ABSENT/LEAVE cannot carry punch times")
        final_pin: Optional[datetime] = None
        final_pout: Optional[datetime] = None
    else:
        final_pin = ms_to_dt(body.punch_in) if "punch_in" in provided else rec.get("punch_in")
        if final_pin is None:
            raise HTTPException(422, "a presence status requires a punch_in")
        if "punch_in" in provided:
            if attendance_date_for(final_pin, emp["shift_start"], emp["shift_end"]) != date:
                raise HTTPException(422, "punch_in must stay on the record's attendance date")
        final_pout = ms_to_dt(body.punch_out) if "punch_out" in provided else rec.get("punch_out")
        if final_pout is not None:
            if final_pout <= final_pin:
                raise HTTPException(422, "punch_out must be after punch_in")
            if (final_pout - final_pin).total_seconds() > MAX_SHIFT_SECONDS:
                raise HTTPException(422, "punch_out is more than 24 hours after punch_in")

    if new_status in ("ABSENT", "LEAVE"):
        late_minutes, work_hours, overtime_minutes, half_day = 0, None, 0, False
    else:
        late_minutes = compute_late_minutes(final_pin, date, emp["shift_start"])
        if final_pout is not None:
            work_hours = compute_work_hours(final_pin, final_pout)
            overtime_minutes = compute_overtime_minutes(
                final_pout, date, emp["shift_start"], emp["shift_end"]
            )
            half_day = Decimal(str(work_hours)) < HALF_DAY_HOURS
        else:
            work_hours, overtime_minutes, half_day = None, 0, False

    new_vals = {
        "status": new_status,
        "punch_in": final_pin,
        "punch_out": final_pout,
        "work_hours": work_hours,
        "late_minutes": late_minutes,
        "overtime_minutes": overtime_minutes,
        "half_day": half_day,
    }
    old_vals = {
        "status": rec["status"],
        "punch_in": rec.get("punch_in"),
        "punch_out": rec.get("punch_out"),
        "work_hours": rec.get("work_hours"),
        "late_minutes": int(rec.get("late_minutes", 0) or 0),
        "overtime_minutes": int(rec.get("overtime_minutes", 0) or 0),
        "half_day": bool(rec.get("half_day", False)),
    }

    changes: dict[str, Any] = {}
    for field in ("status", "punch_in", "punch_out", "work_hours",
                  "late_minutes", "overtime_minutes", "half_day"):
        if old_vals[field] != new_vals[field]:
            changes[field] = {"from": old_vals[field], "to": new_vals[field]}
    if not changes:
        raise HTTPException(422, "request changes nothing")

    history_entry = {
        "at": now_dt(),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes, 
    }

    updated = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "history": {"$size": len(rec.get("history", []))}},
        {"$set": new_vals, "$push": {"history": history_entry}},
        return_document=ReturnDocument.AFTER,
    )
    if not updated:
        raise HTTPException(409, "record was modified concurrently")
    return record_out(updated)


# --------------------------------------------------------------------------- #
# Analytics pipeline builders (reused by the endpoints and by /admin/explain)
# --------------------------------------------------------------------------- #
def pipeline_employee_monthly(emp_code: str, month: str) -> list[dict]:
    first, last, _, _ = month_bounds(month)
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first, "$lte": last}}},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": _present_contrib("$status", "$date", "$half_day", True)},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [
                {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ]


def pipeline_department_summary(month: str, department: Optional[str]) -> list[dict]:
    first, last, _, _ = month_bounds(month)
    match: dict[str, Any] = {"joined_on": {"$lte": last}}  # R9 headcount basis
    if department is not None:
        match["department"] = department

    def reduce_sum(expr: dict) -> dict:
        return {"$reduce": {"input": "$logs", "initialValue": 0,
                            "in": {"$add": ["$$value", expr]}}}

    return [
        {"$match": match},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"ec": "$emp_code"},
            "pipeline": [
                {"$match": {"$expr": {"$and": [
                    {"$eq": ["$emp_code", "$$ec"]},
                    {"$gte": ["$date", first]},
                    {"$lte": ["$date", last]},
                ]}}},
            ],
            "as": "logs",
        }},
        {"$addFields": {
            "present_days_e": reduce_sum(
                _present_contrib("$$this.status", "$$this.date", "$$this.half_day", True)),
            "leave_e": reduce_sum({"$cond": [{"$eq": ["$$this.status", "LEAVE"]}, 1, 0]}),
            "onduty_e": reduce_sum({"$cond": [{"$eq": ["$$this.status", "ON_DUTY"]}, 1, 0]}),
            "late_cnt_e": reduce_sum({"$cond": [
                {"$gt": [{"$ifNull": ["$$this.late_minutes", 0]}, 0]}, 1, 0]}),
            "late_sum_e": reduce_sum({"$ifNull": ["$$this.late_minutes", 0]}),
            "wh_sum_e": reduce_sum({"$cond": [
                {"$and": [
                    {"$in": ["$$this.status", list(PRESENCE_STATUSES)]},
                    {"$ne": [{"$ifNull": ["$$this.work_hours", None]}, None]},
                ]},
                {"$ifNull": ["$$this.work_hours", 0]}, 0]}),
            "wh_cnt_e": reduce_sum({"$cond": [
                {"$and": [
                    {"$in": ["$$this.status", list(PRESENCE_STATUSES)]},
                    {"$ne": [{"$ifNull": ["$$this.work_hours", None]}, None]},
                ]}, 1, 0]}),
        }},
        {"$group": {
            "_id": "$department",
            "headcount": {"$sum": 1},
            "present_days": {"$sum": "$present_days_e"},
            "leave_count": {"$sum": "$leave_e"},
            "on_duty_count": {"$sum": "$onduty_e"},
            "late_count": {"$sum": "$late_cnt_e"},
            "total_late_minutes": {"$sum": "$late_sum_e"},
            "wh_sum": {"$sum": "$wh_sum_e"},
            "wh_cnt": {"$sum": "$wh_cnt_e"},
        }},
        {"$sort": {"_id": ASCENDING}},
    ]


def pipeline_late_leaderboard(month: str, department: Optional[str]) -> list[dict]:
    first, last, _, _ = month_bounds(month)
    pipeline: list[dict] = [
        {"$match": {"date": {"$gte": first, "$lte": last}}},
        {"$group": {
            "_id": "$emp_code",
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "late_count": {"$sum": {"$cond": [
                {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
        }},
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {"$lookup": {"from": "employees", "localField": "_id",
                     "foreignField": "emp_code", "as": "emp"}},
        {"$match": {"emp": {"$ne": []}}},  
        {"$addFields": {"emp": {"$arrayElemAt": ["$emp", 0]}}},
    ]
    if department is not None:
        pipeline.append({"$match": {"emp.department": department}})
    pipeline += [
        {"$project": {
            "_id": 0,
            "emp_code": "$_id",
            "name": "$emp.name",
            "department": "$emp.department",
            "total_late_minutes": 1,
            "late_count": 1,
        }},
        {"$sort": {"total_late_minutes": DESCENDING, "emp_code": ASCENDING}},
    ]
    return pipeline


def pipeline_department_trend(
    department: str, from_str: str, to_str: str, emp_codes: list[str], joined: list[str]
) -> list[dict]:
    from_dt = datetime.fromisoformat(from_str).replace(tzinfo=timezone.utc)
    to_excl = (datetime.fromisoformat(to_str) + timedelta(days=1)).replace(tzinfo=timezone.utc)
    return [
        {"$match": {"emp_code": {"$in": emp_codes}, "date": {"$gte": from_str, "$lte": to_str}}},
        {"$group": {
            "_id": "$date",
            "present_count": {"$sum": _present_contrib(
                "$status", "$date", "$half_day", False)},
            "late_count": {"$sum": {"$cond": [
                {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
        }},
        {"$set": {"d": {"$dateFromString": {"dateString": "$_id", "format": "%Y-%m-%d"}}}},

        {"$densify": {"field": "d",
                      "range": {"step": 1, "unit": "day", "bounds": [from_dt, to_excl]}}},
        {"$set": {
            "dstr": {"$dateToString": {"date": "$d", "format": "%Y-%m-%d"}},
            "present_count": {"$ifNull": ["$present_count", 0]},
            "late_count": {"$ifNull": ["$late_count", 0]},
        }},
        {"$set": {
            "is_working_day": {"$in": [{"$dayOfWeek": "$d"}, WEEKDAY_DOW]},
            
            "headcount": {"$size": {"$filter": {
                "input": {"$literal": joined}, "as": "j",
                "cond": {"$lte": ["$$j", "$dstr"]}}}},
        }},
        {"$set": {
            "attendance_rate": {"$cond": [
                {"$and": [{"$eq": ["$is_working_day", True]}, {"$gt": ["$headcount", 0]}]},
                                {"$divide": ["$present_count",
                             {"$cond": [{"$gt": ["$headcount", 0]}, "$headcount", 1]}]},
                None,
            ]},
        }},
        {"$setWindowFields": {
            "sortBy": {"d": ASCENDING},
            "output": {"moving_avg_7d": {
                "$avg": "$attendance_rate",  # $avg ignores nulls
                "window": {"documents": [-6, 0]},
            }},
        }},
        {"$sort": {"d": ASCENDING}},
        {"$project": {
            "_id": 0, "date": "$dstr", "is_working_day": 1, "headcount": 1,
            "present_count": 1, "late_count": 1, "attendance_rate": 1, "moving_avg_7d": 1,
        }},
    ]


# --------------------------------------------------------------------------- #
# Analytics endpoints
# --------------------------------------------------------------------------- #
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str,
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "unknown employee")

    _, _, first_date, last_date = month_bounds(month)
    working_days = count_working_days(first_date, last_date, date_cls.fromisoformat(emp["joined_on"]))

    agg = list(db.attendance_logs.aggregate(pipeline_employee_monthly(emp_code, month)))
    row = agg[0] if agg else {}
    present_days = float(row.get("present_days", 0) or 0)
    attendance_pct = (
        round_half_up(present_days / working_days * 100, 2) if working_days > 0 else None
    )
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": int(row.get("leave_days", 0) or 0),
        "late_count": int(row.get("late_count", 0) or 0),
        "total_late_minutes": int(row.get("total_late_minutes", 0) or 0),
        "total_overtime_minutes": int(row.get("total_overtime_minutes", 0) or 0),
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    rows = list(db.employees.aggregate(pipeline_department_summary(month, department)))
    items = []
    for r in rows:
        wh_cnt = int(r.get("wh_cnt", 0) or 0)
        avg_wh = round_half_up(r["wh_sum"] / wh_cnt, 2) if wh_cnt > 0 else None
        items.append({
            "department": r["_id"],
            "headcount": int(r.get("headcount", 0) or 0),
            "present_days": float(r.get("present_days", 0) or 0),
            "avg_work_hours": avg_wh,
            "late_count": int(r.get("late_count", 0) or 0),
            "total_late_minutes": int(r.get("total_late_minutes", 0) or 0),
            "leave_count": int(r.get("leave_count", 0) or 0),
            "on_duty_count": int(r.get("on_duty_count", 0) or 0),
        })
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    rows = list(db.attendance_logs.aggregate(pipeline_late_leaderboard(month, department)))
    return {"month": month, "items": rank_leaderboard(rows, limit)}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_: str = Query(..., alias="from"),
    to: str = Query(...),
):
    f = parse_date_param(from_, "from")
    t = parse_date_param(to, "to")
    if t < f:
        raise HTTPException(422, "to must be >= from")
    if (t - f).days > 92:
        raise HTTPException(422, "range must not exceed 92 days")

    emps = list(db.employees.find({"department": department}, {"_id": 0, "emp_code": 1, "joined_on": 1}))
    if not emps:
        raise HTTPException(404, "unknown department")
    emp_codes = [e["emp_code"] for e in emps]
    joined = sorted(e["joined_on"] for e in emps)

    rows = list(db.attendance_logs.aggregate(
        pipeline_department_trend(department, from_, to, emp_codes, joined)
    ))
    items = []
    for r in rows:
        items.append({
            "date": r["date"],
            "is_working_day": bool(r["is_working_day"]),
            "headcount": int(r["headcount"]),
            "present_count": float(r.get("present_count", 0) or 0),
            "late_count": int(r.get("late_count", 0) or 0),
            "attendance_rate": round_half_up(r.get("attendance_rate"), 4),
            "moving_avg_7d": round_half_up(r.get("moving_avg_7d"), 4),
        })
    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Admin: explain
# --------------------------------------------------------------------------- #
def _explain_command(spec: dict) -> dict:
    return db.command({"explain": spec, "verbosity": "executionStats"})


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str = Path(...),
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    status: Optional[Status] = None,
    from_: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    valid = {"attendance_list", "employee_monthly", "department_summary",
             "late_leaderboard", "department_trend"}
    if endpoint not in valid:
        raise HTTPException(422, "unknown endpoint")

    if endpoint == "attendance_list":
        q = _attendance_filter(emp_code, date_from, date_to, status)
        spec = {
            "find": "attendance_logs",
            "filter": q,
            "sort": {"date": -1, "emp_code": 1},
            "skip": (page - 1) * page_size,
            "limit": page_size,
        }
        explain = _explain_command(spec)
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain}

    if endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(422, "emp_code and month are required")
        spec = {"aggregate": "attendance_logs",
                "pipeline": pipeline_employee_monthly(emp_code, month), "cursor": {}}
        return {"endpoint": endpoint, "collection": "attendance_logs",
                "explain": _explain_command(spec)}

    if endpoint == "department_summary":
        if not month:
            raise HTTPException(422, "month is required")
        spec = {"aggregate": "employees",
                "pipeline": pipeline_department_summary(month, department), "cursor": {}}
        return {"endpoint": endpoint, "collection": "employees",
                "explain": _explain_command(spec)}

    if endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, "month is required")
        spec = {"aggregate": "attendance_logs",
                "pipeline": pipeline_late_leaderboard(month, department), "cursor": {}}
        return {"endpoint": endpoint, "collection": "attendance_logs",
                "explain": _explain_command(spec)}

    # department_trend
    if not department or not from_ or not to:
        raise HTTPException(422, "department, from and to are required")
    parse_date_param(from_, "from")
    parse_date_param(to, "to")
    emps = list(db.employees.find({"department": department}, {"_id": 0, "emp_code": 1, "joined_on": 1}))
    if not emps:
        raise HTTPException(404, "unknown department")
    emp_codes = [e["emp_code"] for e in emps]
    joined = sorted(e["joined_on"] for e in emps)
    spec = {"aggregate": "attendance_logs",
            "pipeline": pipeline_department_trend(department, from_, to, emp_codes, joined),
            "cursor": {}}
    return {"endpoint": endpoint, "collection": "attendance_logs",
            "explain": _explain_command(spec)}
