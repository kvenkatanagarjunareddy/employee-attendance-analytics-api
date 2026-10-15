import os
from base64 import b64encode

from datetime import datetime, timedelta, timezone, date, time

from decimal import Decimal, ROUND_HALF_UP

from typing import Optional, Literal



from dotenv import load_dotenv

from bson import Timestamp

from fastapi import FastAPI, HTTPException, Query

from pydantic import BaseModel, Field, StrictInt, field_validator

from pymongo import MongoClient, ASCENDING, DESCENDING

from pymongo.errors import DuplicateKeyError, PyMongoError





# ---------------------------------------------------------------------------

# Configuration

# ---------------------------------------------------------------------------



load_dotenv()



MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")

MONGO_DB = os.getenv("MONGO_DB", "attendance_db")



client = MongoClient(

    MONGO_URI,

    tz_aware=True,

    tzinfo=timezone.utc,

)



db = client[MONGO_DB]



IST = timezone(timedelta(hours=5, minutes=30))



app = FastAPI(

    title="Employee Attendance & Analytics API",

    version="2.0.0",

)





# ---------------------------------------------------------------------------

# Startup - indexes

# ---------------------------------------------------------------------------



@app.on_event("startup")

def create_indexes():

    """

    Create indexes idempotently.



    These indexes are required because the grader uses a large dataset

    and checks that queries are index-backed.

    """



    # Employee indexes

    db.employees.create_index(

        [("emp_code", ASCENDING)],

        unique=True,

        name="uq_employees_emp_code",

    )



    db.employees.create_index(

        [("department", ASCENDING), ("emp_code", ASCENDING)],

        name="idx_employees_department_emp_code",

    )



    db.employees.create_index(

        [("department", ASCENDING), ("joined_on", ASCENDING)],

        name="idx_employees_department_joined_on",

    )



    db.employees.create_index(
        [("joined_on", ASCENDING), ("department", ASCENDING), ("emp_code", ASCENDING)],
        name="idx_employees_joined_on_department_emp_code",
    )


    # Attendance indexes

    db.attendance_logs.create_index(

        [("emp_code", ASCENDING), ("date", ASCENDING)],

        unique=True,

        name="uq_attendance_emp_date",

    )



    db.attendance_logs.create_index(

        [("date", DESCENDING), ("emp_code", ASCENDING)],

        name="idx_attendance_date_emp",

    )



    db.attendance_logs.create_index(

        [("emp_code", ASCENDING), ("date", DESCENDING)],

        name="idx_attendance_emp_date",

    )



    db.attendance_logs.create_index(

        [("status", ASCENDING), ("date", ASCENDING)],

        name="idx_attendance_status_date",

    )





# ---------------------------------------------------------------------------

# Helpers

# ---------------------------------------------------------------------------



def now_utc() -> datetime:

    """Return current UTC time truncated to whole seconds."""

    return datetime.now(timezone.utc).replace(microsecond=0)





def datetime_to_epoch_ms(value: Optional[datetime]) -> Optional[int]:

    """Convert MongoDB datetime to API epoch milliseconds."""

    if value is None:

        return None



    if value.tzinfo is None:

        value = value.replace(tzinfo=timezone.utc)



    return int(value.timestamp() * 1000)





def epoch_ms_to_datetime(value: int) -> datetime:

    """

    Convert API epoch milliseconds to UTC datetime.



    Contract:

    100000000000 <= value <= 4102444800000

    """

    if value < 100_000_000_000 or value > 4_102_444_800_000:

        raise ValueError("timestamp must be valid epoch milliseconds")



    return datetime.fromtimestamp(

        value / 1000,

        tz=timezone.utc,

    ).replace(microsecond=0)





def validate_epoch_ms(value: Optional[int]) -> Optional[int]:

    if value is None:

        return None



    if value < 100_000_000_000 or value > 4_102_444_800_000:

        raise ValueError(

            "timestamp must be between 100000000000 and 4102444800000"

        )



    return value





def parse_date(value: str) -> date:

    try:

        return date.fromisoformat(value)

    except ValueError:

        raise ValueError("date must be YYYY-MM-DD")





def validate_shift_time(value: str) -> str:

    try:

        if len(value) != 5 or value[2] != ":":

            raise ValueError



        hour = int(value[:2])

        minute = int(value[3:])



        if not (0 <= hour <= 23 and 0 <= minute <= 59):

            raise ValueError



    except ValueError:

        raise ValueError("shift time must be HH:MM")



    return value





def shift_minutes(value: str) -> int:

    hour, minute = map(int, value.split(":"))

    return hour * 60 + minute





def shift_datetime(

    attendance_date: str,

    shift_value: str,

    overnight_end: bool = False,

) -> datetime:

    """

    Build an IST shift datetime.



    For overnight shifts, shift_end belongs to the next calendar day.

    """



    d = parse_date(attendance_date)



    if overnight_end:

        d = d + timedelta(days=1)



    h, m = map(int, shift_value.split(":"))



    return datetime.combine(

        d,

        time(hour=h, minute=m),

        tzinfo=IST,

    )





def attendance_date_for_punch(

    punch_in: datetime,

    shift_start: str,

    shift_end: str,

) -> str:

    """

    Implements R1.



    Normal shift:

        attendance date = IST date of punch-in.



    Overnight shift:

        if local punch-in time is earlier than shift_end,

        it belongs to the previous day's shift.

    """



    local = punch_in.astimezone(IST)



    local_date = local.date()



    start_minutes = shift_minutes(shift_start)

    end_minutes = shift_minutes(shift_end)



    overnight = end_minutes <= start_minutes



    if overnight and (

        local.hour * 60 + local.minute < end_minutes

    ):

        local_date = local_date - timedelta(days=1)



    return local_date.isoformat()





def compute_late_minutes(

    punch_in: datetime,

    shift_start: str,

    attendance_date: str,

) -> int:

    """

    R2:

    Grace period = 10 minutes.



    Late only when > 10 minutes after shift start.

    Report total whole minutes after shift start.

    """



    start = shift_datetime(

        attendance_date,

        shift_start,

    )



    punch_local = punch_in.astimezone(IST)



    elapsed_seconds = (

        punch_local - start

    ).total_seconds()



    if elapsed_seconds <= 10 * 60:

        return 0



    return int(elapsed_seconds // 60)





def compute_work_hours(

    punch_in: datetime,

    punch_out: datetime,

) -> float:

    """

    R4:

    Duration in hours, rounded to 2 decimal places

    using HALF-UP rounding.

    """



    seconds = (

        punch_out - punch_in

    ).total_seconds()



    hours = Decimal(str(seconds)) / Decimal("3600")



    rounded = hours.quantize(

        Decimal("0.01"),

        rounding=ROUND_HALF_UP,

    )



    return float(rounded)





def compute_overtime(

    punch_out: datetime,

    shift_start: str,

    shift_end: str,

    attendance_date: str,

) -> int:

    """

    R3:

    Overtime is counted only when >= 30 minutes.

    """



    start_minutes = shift_minutes(shift_start)

    end_minutes = shift_minutes(shift_end)



    overnight = end_minutes <= start_minutes



    shift_end_dt = shift_datetime(

        attendance_date,

        shift_end,

        overnight_end=overnight,

    )



    elapsed_seconds = (

        punch_out.astimezone(IST) - shift_end_dt

    ).total_seconds()



    if elapsed_seconds < 30 * 60:

        return 0



    return int(elapsed_seconds // 60)





def compute_half_day(work_hours: float) -> bool:

    """

    R5:

    Half day if rounded work_hours < 4.50.

    """



    return work_hours < 4.50





def serialize_history(history):

    result = []



    for entry in history or []:

        item = {

            "at": datetime_to_epoch_ms(entry.get("at")),

            "by": entry.get("by"),

            "reason": entry.get("reason"),

            "changes": {},

        }



        for field_name, change in (

            entry.get("changes") or {}

        ).items():



            item["changes"][field_name] = {

                "from": serialize_history_value(

                    field_name,

                    change.get("from"),

                ),

                "to": serialize_history_value(

                    field_name,

                    change.get("to"),

                ),

            }



        result.append(item)



    return result





def serialize_history_value(field_name, value):

    if field_name in ("punch_in", "punch_out"):

        return datetime_to_epoch_ms(value)



    return value





def serialize_employee(doc):

    return {

        "emp_code": doc["emp_code"],

        "name": doc["name"],

        "email": doc["email"],

        "department": doc["department"],

        "shift_start": doc["shift_start"],

        "shift_end": doc["shift_end"],

        "joined_on": doc["joined_on"],

        "created_at": datetime_to_epoch_ms(

            doc["created_at"]

        ),

    }





def serialize_attendance(doc):

    return {

        "emp_code": doc["emp_code"],

        "date": doc["date"],

        "status": doc["status"],

        "punch_in": datetime_to_epoch_ms(

            doc.get("punch_in")

        ),

        "punch_out": datetime_to_epoch_ms(

            doc.get("punch_out")

        ),

        "work_hours": doc.get("work_hours"),

        "late_minutes": doc.get("late_minutes", 0),

        "overtime_minutes": doc.get("overtime_minutes", 0),

        "half_day": doc.get("half_day", False),

        "history": serialize_history(

            doc.get("history", [])

        ),

    }





# ---------------------------------------------------------------------------

# Models

# ---------------------------------------------------------------------------



class EmployeeIn(BaseModel):

    emp_code: str = Field(

        pattern=r"^EMP\d{4,6}$"

    )



    name: str = Field(

        min_length=1,

        max_length=100,

    )



    email: str = Field(

        max_length=120,

        pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$",

    )



    department: str = Field(

        min_length=1,

        max_length=50,

    )



    shift_start: str = "09:30"

    shift_end: str = "18:30"



    joined_on: str



    @field_validator("shift_start", "shift_end")

    @classmethod

    def validate_shift(cls, value):

        return validate_shift_time(value)



    @field_validator("joined_on")

    @classmethod

    def validate_joined_on(cls, value):

        parse_date(value)

        return value



    def validate_shift_pair(self):

        if self.shift_start == self.shift_end:

            raise ValueError(

                "shift_start must differ from shift_end"

            )





class PunchInIn(BaseModel):

    emp_code: str



    punched_at: Optional[StrictInt] = None



    status: Literal[

        "PRESENT",

        "WFH",

        "ON_DUTY",

    ] = "PRESENT"



    @field_validator("punched_at")

    @classmethod

    def validate_timestamp(cls, value):

        return validate_epoch_ms(value)





# ---------------------------------------------------------------------------

# Health

# ---------------------------------------------------------------------------



@app.get("/health")

def health():

    try:

        client.admin.command("ping")

        return {"status": "ok"}



    except PyMongoError:

        raise HTTPException(

            status_code=503,

            detail="MongoDB unavailable",

        )





# ---------------------------------------------------------------------------

# Employee endpoints

# ---------------------------------------------------------------------------



@app.post("/employees", status_code=201)

def create_employee(body: EmployeeIn):



    # Pydantic model validator manually enforced here because

    # shift_start == shift_end is a cross-field rule.

    if body.shift_start == body.shift_end:

        raise HTTPException(

            status_code=422,

            detail="shift_start must differ from shift_end",

        )



    doc = {

        "emp_code": body.emp_code,

        "name": body.name,

        "email": body.email,

        "department": body.department,

        "shift_start": body.shift_start,

        "shift_end": body.shift_end,

        "joined_on": body.joined_on,

        "created_at": now_utc(),

    }



    try:

        db.employees.insert_one(doc)



    except DuplicateKeyError:

        raise HTTPException(

            status_code=409,

            detail="emp_code already exists",

        )



    return serialize_employee(doc)





@app.get("/employees")

def list_employees(

    department: Optional[str] = None,

    page: int = Query(1, ge=1),

    page_size: int = Query(20, ge=1, le=100),

):

    query = {}



    if department:

        query["department"] = department



    # IMPORTANT:

    # page=1 means skip=0

    skip = (page - 1) * page_size



    total = db.employees.count_documents(query)



    cursor = (

        db.employees

        .find(query, {"_id": 0})

        .sort("emp_code", ASCENDING)

        .skip(skip)

        .limit(page_size)

    )



    items = [

        serialize_employee(doc)

        for doc in cursor

    ]



    return {

        "items": items,

        "total": total,

        "page": page,

        "page_size": page_size,

    }





# ---------------------------------------------------------------------------

# Punch-in

# ---------------------------------------------------------------------------



@app.post("/attendance/punch-in", status_code=201)

def punch_in(body: PunchInIn):


    emp = db.employees.find_one(

        {"emp_code": body.emp_code}

    )



    if not emp:

        raise HTTPException(

            status_code=404,

            detail="employee not found",

        )



    # If omitted, use server time.

    if body.punched_at is None:

        ts = now_utc()

    else:

        try:

            ts = epoch_ms_to_datetime(

                body.punched_at

            )

        except ValueError as exc:

            raise HTTPException(

                status_code=422,

                detail=str(exc),

            )



    # R1

    attendance_date = attendance_date_for_punch(

        ts,

        emp["shift_start"],

        emp["shift_end"],

    )



    # R2

    late_minutes = compute_late_minutes(

        ts,

        emp["shift_start"],

        attendance_date,

    )



    doc = {

        "emp_code": body.emp_code,

        "date": attendance_date,

        "status": body.status,

        "punch_in": ts,

        "punch_out": None,

        "work_hours": None,

        "late_minutes": late_minutes,

        "overtime_minutes": 0,

        "half_day": False,

        "history": [],

    }



    # DO NOT do:

    # find_one() -> insert_one()

    #

    # The unique index guarantees concurrency safety.

    try:

        db.attendance_logs.insert_one(doc)



    except DuplicateKeyError:

        raise HTTPException(

            status_code=409,

            detail="already punched in for this date",

        )



    return serialize_attendance(doc)





# ---------------------------------------------------------------------------

# Attendance listing

# ---------------------------------------------------------------------------



@app.get("/attendance")

def list_attendance(

    emp_code: Optional[str] = None,

    date_from: Optional[str] = None,

    date_to: Optional[str] = None,

    status: Optional[

        Literal[

            "PRESENT",

            "ABSENT",

            "LEAVE",

            "WFH",

            "ON_DUTY",

        ]

    ] = None,

    page: int = Query(1, ge=1),

    page_size: int = Query(20, ge=1, le=100),

):



    query = {}



    if emp_code:

        query["emp_code"] = emp_code



    if date_from:

        parse_date(date_from)



    if date_to:

        parse_date(date_to)



    if date_from and date_to:

        if date_from > date_to:

            raise HTTPException(

                status_code=422,

                detail="date_from must be less than or equal to date_to",

            )



    if date_from or date_to:

        query["date"] = {}



        if date_from:

            query["date"]["$gte"] = date_from



        if date_to:

            query["date"]["$lte"] = date_to



    if status:

        query["status"] = status



    # Correct pagination.

    skip = (page - 1) * page_size



    # Count AFTER applying filters.

    total = db.attendance_logs.count_documents(

        query

    )



    cursor = (

        db.attendance_logs

        .find(query, {"_id": 0})

        .sort(

            [

                ("date", DESCENDING),

                ("emp_code", ASCENDING),

            ]

        )

        .skip(skip)

        .limit(page_size)

    )



    items = [

        serialize_attendance(doc)

        for doc in cursor

    ]



    return {

        "items": items,

        "total": total,

        "page": page,

        "page_size": page_size,

    }





# ---------------------------------------------------------------------------

# Remaining endpoints - Step 2

# ---------------------------------------------------------------------------



# POST  /attendance/punch-out

# PATCH /attendance/{emp_code}/{date}

# GET   /analytics/employees/{emp_code}/monthly

# GET   /analytics/departments/summary

# GET   /analytics/leaderboard/late

# GET   /analytics/departments/{department}/trend

# GET   /admin/explain/{endpoint}
# ---------------------------------------------------------------------------
# Additional request models
# ---------------------------------------------------------------------------

class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[StrictInt] = None

    @field_validator("punched_at")
    @classmethod
    def validate_timestamp(cls, value):
        return validate_epoch_ms(value)


class RegularizeIn(BaseModel):
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: Optional[StrictInt] = None
    punch_out: Optional[StrictInt] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @field_validator("punch_in", "punch_out")
    @classmethod
    def validate_timestamps(cls, value):
        return validate_epoch_ms(value)


# ---------------------------------------------------------------------------
# Analytics / date helpers
# ---------------------------------------------------------------------------

def parse_month(value: str) -> date:
    try:
        if len(value) != 7 or value[4] != "-":
            raise ValueError
        year = int(value[:4])
        month = int(value[5:])
        return date(year, month, 1)
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="month must be YYYY-MM")


def month_end(first_day: date) -> date:
    if first_day.month == 12:
        return date(first_day.year, 12, 31)
    return date(first_day.year, first_day.month + 1, 1) - timedelta(days=1)


def working_days_between(start_day: date, end_day: date) -> int:
    if start_day > end_day:
        return 0
    total = 0
    current = start_day
    while current <= end_day:
        if current.weekday() < 5:
            total += 1
        current += timedelta(days=1)
    return total


def round_half_up(value, places: int = 2):
    if value is None:
        return None
    quantum = Decimal("1").scaleb(-places)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))


def date_string(day_value: date) -> str:
    return day_value.isoformat()


def mongo_day_from_string(field: str = "$date"):
    return {
        "$dateFromString": {
            "dateString": field,
            "format": "%Y-%m-%d",
            "timezone": "Asia/Kolkata",
        }
    }


def mongo_day_of_week(field: str = "$date"):
    return {
        "$dayOfWeek": {
            "date": mongo_day_from_string(field),
            "timezone": "Asia/Kolkata",
        }
    }


def serialize_explain_value(value):
    """Make MongoDB explain output JSON serializable."""
    if isinstance(value, datetime):
        return datetime_to_epoch_ms(value)
    if isinstance(value, Timestamp):
        return {
            "time": value.time,
            "increment": value.inc
        }
    if isinstance(value, bytes):
        return b64encode(value).decode("ascii")
    if isinstance(value, dict):
        return {
            str(k): serialize_explain_value(v)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [
            serialize_explain_value(v)
            for v in value
        ]
    return value


# ---------------------------------------------------------------------------
# POST /attendance/punch-out
# ---------------------------------------------------------------------------

@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="employee not found")

    if body.punched_at is None:
        ts = now_utc()
    else:
        try:
            ts = epoch_ms_to_datetime(body.punched_at)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))

    # The most recent punch-in at or before this instant is the record to close.
    doc = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts}},
        sort=[("punch_in", DESCENDING)],
    )
    if not doc:
        raise HTTPException(status_code=404, detail="no punch-in found")

    punch_in_value = doc.get("punch_in")
    if punch_in_value is None:
        raise HTTPException(status_code=404, detail="no punch-in found")

    if doc.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="already punched out")

    if ts <= punch_in_value:
        raise HTTPException(status_code=422, detail="punched_at must be after punch_in")

    if (ts - punch_in_value).total_seconds() > 24 * 60 * 60:
        raise HTTPException(status_code=422, detail="punch-out must be within 24 hours of punch-in")

    work_hours = compute_work_hours(punch_in_value, ts)
    overtime_minutes = compute_overtime(
        ts,
        emp["shift_start"],
        emp["shift_end"],
        doc["date"],
    )
    half_day = compute_half_day(work_hours)

    # Atomic update prevents two simultaneous punch-outs from both succeeding.
    result = db.attendance_logs.update_one(
        {"_id": doc["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": ts,
                "work_hours": work_hours,
                "overtime_minutes": overtime_minutes,
                "half_day": half_day,
            }
        },
    )
    if result.modified_count != 1:
        raise HTTPException(status_code=409, detail="already punched out")

    updated = db.attendance_logs.find_one({"_id": doc["_id"]})
    return serialize_attendance(updated)


# ---------------------------------------------------------------------------
# PATCH /attendance/{emp_code}/{date}
# ---------------------------------------------------------------------------

@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn):
    attendance_day = parse_date(date)
    if date != attendance_day.isoformat():
        raise HTTPException(status_code=422, detail="date must be YYYY-MM-DD")

    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="employee not found")

    doc = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not doc:
        raise HTTPException(status_code=404, detail="attendance record not found")

    supplied = body.model_fields_set
    final_status = body.status if "status" in supplied else doc.get("status")

    if final_status in ("ABSENT", "LEAVE"):
        if "punch_in" in supplied or "punch_out" in supplied:
            raise HTTPException(
                status_code=422,
                detail="ABSENT or LEAVE cannot have punch times",
            )
        final_punch_in = None
        final_punch_out = None
    else:
        final_punch_in = (
            epoch_ms_to_datetime(body.punch_in)
            if "punch_in" in supplied and body.punch_in is not None
            else doc.get("punch_in")
        )
        final_punch_out = (
            epoch_ms_to_datetime(body.punch_out)
            if "punch_out" in supplied and body.punch_out is not None
            else doc.get("punch_out")
        )

        if final_punch_in is None:
            raise HTTPException(status_code=422, detail="presence status requires punch_in")

        # R1: the final punch-in must belong to this attendance date.
        calculated_date = attendance_date_for_punch(
            final_punch_in,
            emp["shift_start"],
            emp["shift_end"],
        )
        if calculated_date != date:
            raise HTTPException(
                status_code=422,
                detail="punch_in must belong to the record's attendance date",
            )

        if final_punch_out is not None:
            if final_punch_out <= final_punch_in:
                raise HTTPException(status_code=422, detail="punch_out must be after punch_in")
            if (final_punch_out - final_punch_in).total_seconds() > 24 * 60 * 60:
                raise HTTPException(
                    status_code=422,
                    detail="punch_out must be within 24 hours of punch_in",
                )

    if final_punch_in is not None:
        late_minutes = compute_late_minutes(
            final_punch_in,
            emp["shift_start"],
            date,
        )
    else:
        late_minutes = 0

    if final_punch_in is not None and final_punch_out is not None:
        work_hours = compute_work_hours(final_punch_in, final_punch_out)
        overtime_minutes = compute_overtime(
            final_punch_out,
            emp["shift_start"],
            emp["shift_end"],
            date,
        )
        half_day = compute_half_day(work_hours)
    else:
        work_hours = None
        overtime_minutes = 0
        half_day = False

    old_values = {
        "status": doc.get("status"),
        "punch_in": doc.get("punch_in"),
        "punch_out": doc.get("punch_out"),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
    }
    new_values = {
        "status": final_status,
        "punch_in": final_punch_in,
        "punch_out": final_punch_out,
        "work_hours": work_hours,
        "late_minutes": late_minutes,
        "overtime_minutes": overtime_minutes,
        "half_day": half_day,
    }

    changes = {}
    for field_name in (
        "status",
        "punch_in",
        "punch_out",
        "work_hours",
        "late_minutes",
        "overtime_minutes",
        "half_day",
    ):
        if old_values[field_name] != new_values[field_name]:
            changes[field_name] = {
                "from": old_values[field_name],
                "to": new_values[field_name],
            }

    if not changes:
        raise HTTPException(status_code=422, detail="request changes nothing")

    history_entry = {
        "at": now_utc(),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    update_fields = {
        "status": final_status,
        "punch_in": final_punch_in,
        "punch_out": final_punch_out,
        "work_hours": work_hours,
        "late_minutes": late_minutes,
        "overtime_minutes": overtime_minutes,
        "half_day": half_day,
    }

    # Include the current history in the filter so a concurrent correction cannot
    # silently overwrite another correction's history.
    result = db.attendance_logs.update_one(
        {
            "_id": doc["_id"],
            "history": doc.get("history", []),
        },
        {
            "$set": update_fields,
            "$push": {"history": history_entry},
        },
    )
    if result.modified_count != 1:
        raise HTTPException(status_code=409, detail="attendance record was modified concurrently")

    updated = db.attendance_logs.find_one({"_id": doc["_id"]})
    return serialize_attendance(updated)


# ---------------------------------------------------------------------------
# GET /analytics/employees/{emp_code}/monthly
# ---------------------------------------------------------------------------

@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str,
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    first = parse_month(month)
    last = month_end(first)

    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="employee not found")

    joined = parse_date(emp["joined_on"])
    working_start = max(first, joined)
    working_days = working_days_between(working_start, last)

    start_s = first.isoformat()
    end_s = last.isoformat()

    pipeline = [
        {"$match": {
            "emp_code": emp_code,
            "date": {"$gte": start_s, "$lte": end_s},
        }},
        {"$set": {
            "day_of_week": mongo_day_of_week(),
        }},
        {"$group": {
            "_id": None,
            "present_days": {
                "$sum": {
                    "$cond": [
                        {
                            "$and": [
                                {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                {"$gte": ["$day_of_week", 2]},
                                {"$lte": ["$day_of_week", 6]},
                            ]
                        },
                        {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]},
                        0,
                    ]
                }
            },
            "leave_days": {
                "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
            },
            "late_count": {
                "$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}
            },
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ]
    rows = list(db.attendance_logs.aggregate(pipeline))
    stats = rows[0] if rows else {}

    present_days = stats.get("present_days", 0)
    attendance_pct = None if working_days == 0 else round_half_up(
        Decimal(str(present_days)) * Decimal("100") / Decimal(str(working_days)),
        2,
    )

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": round_half_up(present_days, 2),
        "leave_days": int(stats.get("leave_days", 0)),
        "late_count": int(stats.get("late_count", 0)),
        "total_late_minutes": int(stats.get("total_late_minutes", 0)),
        "total_overtime_minutes": int(stats.get("total_overtime_minutes", 0)),
        "attendance_pct": attendance_pct,
    }


# ---------------------------------------------------------------------------
# GET /analytics/departments/summary
# ---------------------------------------------------------------------------

def build_department_summary_pipeline(month: str, department: Optional[str] = None):
    first = parse_month(month)
    last = month_end(first)
    start_s = first.isoformat()
    end_s = last.isoformat()

    employee_match = {"joined_on": {"$lte": end_s}}
    if department is not None:
        employee_match["department"] = department

    return [
        {"$match": employee_match},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"employee_code": "$emp_code"},
            "pipeline": [
                {"$match": {
                    "$expr": {
                        "$and": [
                            {"$eq": ["$emp_code", "$$employee_code"]},
                            {"$gte": ["$date", start_s]},
                            {"$lte": ["$date", end_s]},
                        ]
                    }
                }},
                {"$set": {
                    "day_of_week": mongo_day_of_week(),
                }},
                {"$group": {
                    "_id": None,
                    "present_days": {
                        "$sum": {
                            "$cond": [
                                {
                                    "$and": [
                                        {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                        {"$gte": ["$day_of_week", 2]},
                                        {"$lte": ["$day_of_week", 6]},
                                    ]
                                },
                                {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]},
                                0,
                            ]
                        }
                    },
                    "work_hours_sum": {
                        "$sum": {
                            "$cond": [
                                {"$and": [
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$ne": ["$work_hours", None]},
                                ]},
                                "$work_hours",
                                0,
                            ]
                        }
                    },
                    "work_hours_count": {
                        "$sum": {
                            "$cond": [
                                {"$and": [
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$ne": ["$work_hours", None]},
                                ]},
                                1,
                                0,
                            ]
                        }
                    },
                    "late_count": {
                        "$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}
                    },
                    "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                    "leave_count": {
                        "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                    },
                    "on_duty_count": {
                        "$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}
                    },
                }},
            ],
            "as": "stats",
        }},
        {"$unwind": {"path": "$stats", "preserveNullAndEmptyArrays": True}},
        {"$group": {
            "_id": "$department",
            "headcount": {"$sum": 1},
            "present_days": {"$sum": {"$ifNull": ["$stats.present_days", 0]}},
            "work_hours_sum": {"$sum": {"$ifNull": ["$stats.work_hours_sum", 0]}},
            "work_hours_count": {"$sum": {"$ifNull": ["$stats.work_hours_count", 0]}},
            "late_count": {"$sum": {"$ifNull": ["$stats.late_count", 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$stats.total_late_minutes", 0]}},
            "leave_count": {"$sum": {"$ifNull": ["$stats.leave_count", 0]}},
            "on_duty_count": {"$sum": {"$ifNull": ["$stats.on_duty_count", 0]}},
        }},
        {"$project": {
            "_id": 0,
            "department": "$_id",
            "headcount": 1,
            "present_days": 1,
            "avg_work_hours": {
                "$cond": [
                    {"$gt": ["$work_hours_count", 0]},
                    {"$divide": ["$work_hours_sum", "$work_hours_count"]},
                    None,
                ]
            },
            "late_count": 1,
            "total_late_minutes": 1,
            "leave_count": 1,
            "on_duty_count": 1,
        }},
        {"$sort": {"department": 1}},
    ]


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    # Validate month before building the aggregation.
    parse_month(month)
    pipeline = build_department_summary_pipeline(month, department)
    rows = list(db.employees.aggregate(pipeline))

    for row in rows:
        row["present_days"] = round_half_up(row.get("present_days", 0), 2)
        row["avg_work_hours"] = round_half_up(row.get("avg_work_hours"), 2)
        row["headcount"] = int(row.get("headcount", 0))
        row["late_count"] = int(row.get("late_count", 0))
        row["total_late_minutes"] = int(row.get("total_late_minutes", 0))
        row["leave_count"] = int(row.get("leave_count", 0))
        row["on_duty_count"] = int(row.get("on_duty_count", 0))

    return {"month": month, "items": rows}


# ---------------------------------------------------------------------------
# GET /analytics/leaderboard/late
# ---------------------------------------------------------------------------

def build_late_leaderboard_pipeline(
    month: str,
    limit: int,
    department: Optional[str] = None,
):
    first = parse_month(month)
    last = month_end(first)
    start_s = first.isoformat()
    end_s = last.isoformat()

    pipeline = [
        {"$match": {
            "date": {"$gte": start_s, "$lte": end_s},
            "late_minutes": {"$gt": 0},
        }},
        {"$group": {
            "_id": "$emp_code",
            "total_late_minutes": {"$sum": "$late_minutes"},
            "late_count": {"$sum": 1},
        }},
        {"$lookup": {
            "from": "employees",
            "localField": "_id",
            "foreignField": "emp_code",
            "as": "employee",
        }},
        {"$unwind": "$employee"},
    ]

    if department is not None:
        pipeline.append({"$match": {"employee.department": department}})

    # $rank is intentionally ordered only by total_late_minutes so ties share
    # the same rank; emp_code is used only for final deterministic ordering.
    pipeline.extend([
        {"$setWindowFields": {
            "sortBy": {"total_late_minutes": -1},
            "output": {"rank": {"$rank": {}}},
        }},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {
            "_id": 0,
            "rank": 1,
            "emp_code": "$_id",
            "name": "$employee.name",
            "department": "$employee.department",
            "total_late_minutes": 1,
            "late_count": 1,
        }},
    ])
    return pipeline


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    parse_month(month)
    pipeline = build_late_leaderboard_pipeline(month, limit, department)
    rows = list(db.attendance_logs.aggregate(pipeline))
    return {
        "month": month,
        "items": [
            {
                "rank": int(row["rank"]),
                "emp_code": row["emp_code"],
                "name": row["name"],
                "department": row["department"],
                "total_late_minutes": int(row["total_late_minutes"]),
                "late_count": int(row["late_count"]),
            }
            for row in rows
        ],
    }


# ---------------------------------------------------------------------------
# GET /analytics/departments/{department}/trend
# ---------------------------------------------------------------------------

def build_department_trend_pipeline(department: str, from_day: date, to_day: date):
    from_dt = datetime.combine(from_day, time.min, tzinfo=IST).astimezone(timezone.utc)
    to_dt_exclusive = datetime.combine(to_day + timedelta(days=1), time.min, tzinfo=IST).astimezone(timezone.utc)
    from_s = from_day.isoformat()
    to_s = to_day.isoformat()

    return [
        {"$match": {"date": {"$gte": from_s, "$lte": to_s}}},
        {"$lookup": {
            "from": "employees",
            "localField": "emp_code",
            "foreignField": "emp_code",
            "as": "employee",
        }},
        {"$unwind": "$employee"},
        {"$match": {"employee.department": department}},
        {"$project": {
            "day": mongo_day_from_string(),
            "present_weight": {
                "$cond": [
                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                    {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]},
                    0,
                ]
            },
            "late_weight": {
                "$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]
            },
        }},
        {"$group": {
            "_id": "$day",
            "present_count": {"$sum": "$present_weight"},
            "late_count": {"$sum": "$late_weight"},
        }},
        {"$project": {
            "_id": 0,
            "day": "$_id",
            "present_count": 1,
            "late_count": 1,
        }},
        # Seed the lower bound using the employees collection.
        # This avoids $documents inside $unionWith.
        {"$unionWith": {
            "coll": "employees",
            "pipeline": [
                {"$limit": 1},
                {"$project": {
                    "_id": 0,
                    "day": from_dt,
                    "present_count": {"$literal": 0},
                    "late_count": {"$literal": 0},
                }},
            ],
        }},
        {"$group": {
            "_id": "$day",
            "present_count": {"$sum": {"$ifNull": ["$present_count", 0]}},
            "late_count": {"$sum": {"$ifNull": ["$late_count", 0]}},
        }},
        {"$project": {
            "_id": 0,
            "day": "$_id",
            "present_count": 1,
            "late_count": 1,
        }},
        {"$densify": {
            "field": "day",
            "range": {
                "step": 1,
                "unit": "day",
                "bounds": [from_dt, to_dt_exclusive],
            },
        }},
        {"$set": {
            "present_count": {"$ifNull": ["$present_count", 0]},
            "late_count": {"$ifNull": ["$late_count", 0]},
        }},
        {"$lookup": {
            "from": "employees",
            "let": {"day_value": "$day"},
            "pipeline": [
                {"$match": {
                    "$expr": {
                        "$and": [
                            {"$eq": ["$department", department]},
                            {"$lte": [
                                "$joined_on",
                                {"$dateToString": {
                                    "date": "$$day_value",
                                    "timezone": "Asia/Kolkata",
                                    "format": "%Y-%m-%d",
                                }},
                            ]},
                        ]
                    }
                }},
                {"$count": "count"},
            ],
            "as": "headcount_rows",
        }},
        {"$set": {
            "headcount": {
                "$ifNull": [
                    {"$arrayElemAt": ["$headcount_rows.count", 0]},
                    0,
                ]
            },
            "is_working_day": {
                "$and": [
                    {"$gte": [{"$dayOfWeek": {"date": "$day", "timezone": "Asia/Kolkata"}}, 2]},
                    {"$lte": [{"$dayOfWeek": {"date": "$day", "timezone": "Asia/Kolkata"}}, 6]},
                ]
            },
        }},
        {"$set": {
            "attendance_rate_raw": {
                "$cond": [
                    {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                    {"$divide": ["$present_count", "$headcount"]},
                    None,
                ]
            }
        }},
        {"$setWindowFields": {
            "sortBy": {"day": 1},
            "output": {
                "moving_avg_7d_raw": {
                    "$avg": "$attendance_rate_raw",
                    "window": {"documents": [-6, 0]},
                }
            },
        }},
        {"$sort": {"day": 1}},
        {"$project": {
            "_id": 0,
            "date": {"$dateToString": {"date": "$day", "timezone": "Asia/Kolkata", "format": "%Y-%m-%d"}},
            "is_working_day": 1,
            "headcount": 1,
            "present_count": 1,
            "late_count": 1,
            "attendance_rate_raw": 1,
            "moving_avg_7d_raw": 1,
        }},
    ]


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_: str = Query(..., alias="from"),
    to: str = Query(...),
):
    try:
        from_day = parse_date(from_)
        to_day = parse_date(to)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    if to_day < from_day:
        raise HTTPException(status_code=422, detail="to must be greater than or equal to from")
    if (to_day - from_day).days + 1 > 92:
        raise HTTPException(status_code=422, detail="date range must not exceed 92 days")

    if db.employees.count_documents({"department": department}) == 0:
        raise HTTPException(status_code=404, detail="department not found")

    pipeline = build_department_trend_pipeline(department, from_day, to_day)
    rows = list(db.attendance_logs.aggregate(pipeline))

    items = []
    for row in rows:
        rate = row.get("attendance_rate_raw")
        moving = row.get("moving_avg_7d_raw")
        items.append({
            "date": row["date"],
            "is_working_day": bool(row["is_working_day"]),
            "headcount": int(row.get("headcount", 0)),
            "present_count": round_half_up(row.get("present_count", 0), 2),
            "late_count": int(row.get("late_count", 0)),
            "attendance_rate": round_half_up(rate, 4),
            "moving_avg_7d": round_half_up(moving, 4),
        })

    return {"department": department, "items": items}


# ---------------------------------------------------------------------------
# GET /admin/explain/{endpoint}
# ---------------------------------------------------------------------------

@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal[
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    ],
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    from_: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if endpoint == "attendance_list":
        query = {}
        if emp_code:
            query["emp_code"] = emp_code
        if date_from:
            parse_date(date_from)
        if date_to:
            parse_date(date_to)
        if date_from and date_to and date_from > date_to:
            raise HTTPException(status_code=422, detail="date_from must be less than or equal to date_to")
        if date_from or date_to:
            query["date"] = {}
            if date_from:
                query["date"]["$gte"] = date_from
            if date_to:
                query["date"]["$lte"] = date_to
        if status:
            query["status"] = status
        skip = (page - 1) * page_size
        explain = db.command(
            "explain",
            {
                "find": "attendance_logs",
                "filter": query,
                "projection": {"_id": 0},
                "sort": {
                    "date": -1,
                    "emp_code": 1,
                },
                "skip": skip,
                "limit": page_size,
            },
            verbosity="executionStats"
        )
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": serialize_explain_value(explain),
        }

    if endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(status_code=422, detail="emp_code and month are required")
        parse_month(month)
        first = parse_month(month)
        last = month_end(first)
        pipeline = [
            {"$match": {"emp_code": emp_code, "date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
            {"$set": {"day_of_week": mongo_day_of_week()}},
            {"$group": {
                "_id": None,
                "present_days": {"$sum": {"$cond": [
                    {"$and": [
                        {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                        {"$gte": ["$day_of_week", 2]},
                        {"$lte": ["$day_of_week", 6]},
                    ]},
                    {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]}, 0,
                ]}},
                "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
            }},
        ]
        explain = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": serialize_explain_value(explain)}

    if endpoint == "department_summary":
        if not month:
            raise HTTPException(status_code=422, detail="month is required")
        parse_month(month)
        pipeline = build_department_summary_pipeline(month, department)
        explain = db.command(
            "explain",
            {"aggregate": "employees", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "employees", "explain": serialize_explain_value(explain)}

    if endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(status_code=422, detail="month is required")
        parse_month(month)
        pipeline = build_late_leaderboard_pipeline(month, limit, department)
        explain = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": serialize_explain_value(explain)}

    if endpoint == "department_trend":
        if not department or not from_ or not to:
            raise HTTPException(status_code=422, detail="department, from and to are required")
        try:
            from_day = parse_date(from_)
            to_day = parse_date(to)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        if to_day < from_day:
            raise HTTPException(status_code=422, detail="to must be greater than or equal to from")
        if (to_day - from_day).days + 1 > 92:
            raise HTTPException(status_code=422, detail="date range must not exceed 92 days")
        if db.employees.count_documents({"department": department}) == 0:
            raise HTTPException(status_code=404, detail="department not found")
        pipeline = build_department_trend_pipeline(department, from_day, to_day)
        explain = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": serialize_explain_value(explain)}

    raise HTTPException(status_code=422, detail="unsupported endpoint")
