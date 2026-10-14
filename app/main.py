"""
Employee Attendance & Analytics API - STARTER

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)

This file was written quickly by a colleague who has left the company. The happy path
works, but nobody has reviewed it. Read PROBLEM_STATEMENT.docx for what is expected of you,
openapi.yaml for the contract and DATA_MODEL.md for what is stored in MongoDB.
"""
import os
import re
from datetime import datetime, timedelta, timezone,date,time
from decimal import Decimal,ROUND_HALF_UP
from typing import Optional,Literal
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException,Query
from pydantic import BaseModel,ConfigDict
from pymongo import MongoClient,ReturnDocument
from pymongo.errors import DuplicateKeyError,PyMongoError
from fastapi.responses import Response
from bson import json_util

load_dotenv()

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))
db = client[os.getenv("MONGO_DB", "attendance_db")]
# Employees
db.employees.create_index([("emp_code", 1)],unique=True,name="employees_emp_code")
db.employees.create_index([("department", 1), ("joined_on", 1)],name="employees_department_joined")
# Attendance: one record per employee per date
db.attendance_logs.create_index([("emp_code", 1), ("date", 1)],unique=True,name="attendance_emp_code_date")
# Attendance listing and date-based queries
db.attendance_logs.create_index([("date", -1), ("emp_code", 1)],name="attendance_date_emp_code")
# Attendance filtering by date and status
db.attendance_logs.create_index([("date", 1), ("status", 1)],name="attendance_date_status")

IST = timezone(timedelta(hours=5, minutes=30))

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")

PRESENT_STATUSES = {"PRESENT", "WFH", "ON_DUTY"}
NON_PRESENT_STATUSES = {"ABSENT", "LEAVE"}
VALID_STATUSES = PRESENT_STATUSES | NON_PRESENT_STATUSES
# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def normalize_timestamp(value):
    if value is None:
        return None

    # Unix timestamp in milliseconds
    if isinstance(value, (int, float)):
        value = datetime.fromtimestamp(
            value / 1000,
            tz=timezone.utc
        )

    # Handle datetime values
    if not isinstance(value, datetime):
        raise ValueError("Timestamp must be milliseconds or datetime")

    if value.tzinfo is None:
        value = value.replace(tzinfo=IST)

    # Truncate to whole seconds
    return value.replace(microsecond=0)


def stored_timestamp_to_ist(value: datetime | None) -> datetime | None:
    """MongoDB commonly returns BSON datetimes as naive UTC values."""
    if value is None:
        return None

    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)

    return value.astimezone(IST).replace(microsecond=0)


def get_attendance_date(punch_in: datetime,shift_start: str,shift_end: str) -> date:
    """Apply R1 to determine the attendance date from punch-in."""
    start_h, start_m = map(int, shift_start.split(":"))
    end_h, end_m = map(int, shift_end.split(":"))

    start_minutes = start_h * 60 + start_m
    end_minutes = end_h * 60 + end_m
    punch_minutes = punch_in.hour * 60 + punch_in.minute

    attendance_date = punch_in.date()

    if end_minutes <= start_minutes and punch_minutes < end_minutes:
        attendance_date -= timedelta(days=1)

    return attendance_date


def compute_late_minutes(punch_in: datetime,shift_start: str,attendance_date: date) -> int:
    """R2: grace period is 10 minutes; count whole elapsed minutes."""
    start_h, start_m = map(int, shift_start.split(":"))

    shift_start_dt = datetime.combine(
        attendance_date,
        time(start_h, start_m),
        tzinfo=IST,
    )

    elapsed_seconds = (punch_in - shift_start_dt).total_seconds()

    if elapsed_seconds <= 10 * 60:
        return 0

    return int(elapsed_seconds // 60)


def compute_work_hours(punch_in: datetime,punch_out: datetime) -> float:
    """R4: round to two decimal places using ROUND_HALF_UP."""
    elapsed_seconds = Decimal(
        str((punch_out - punch_in).total_seconds())
    )

    hours = elapsed_seconds / Decimal("3600")

    return float(
        hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    )


def compute_overtime(punch_out: datetime,shift_start: str,shift_end: str,attendance_date: date) -> int:
    """R3: count whole overtime minutes only when at least 30."""
    start_h, start_m = map(int, shift_start.split(":"))
    end_h, end_m = map(int, shift_end.split(":"))

    shift_end_dt = datetime.combine(
        attendance_date,
        time(end_h, end_m),
        tzinfo=IST,
    )

    if (end_h * 60 + end_m) <= (start_h * 60 + start_m):
        shift_end_dt += timedelta(days=1)

    overtime_seconds = (punch_out - shift_end_dt).total_seconds()
    overtime_minutes = int(overtime_seconds // 60)

    return overtime_minutes if overtime_minutes >= 30 else 0
    
def count_working_days(start_date: date, end_date: date) -> int:
    """Count weekdays inclusively, excluding no holidays."""
    if start_date > end_date:
        return 0

    days = 0
    current = start_date

    while current <= end_date:
        if current.weekday() < 5:  # Monday=0, Friday=4
            days += 1
        current += timedelta(days=1)

    return days


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None
    status: str = "PRESENT"
class PunchOutIn(BaseModel):
    emp_code:str
    punched_at:Optional[int]= None
    status :str ="PRESENT"
    
class RegularizeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Optional[
        Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]
    ] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None

# --------------------------------------------------------------------------- #
# Endpoints provided
# --------------------------------------------------------------------------- #
@app.get("/health",tags=["system"],operation_id="health")
def health():
    try :
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception : 
        raise HTTPException(
            status_code = 503,
            detail={"status":"error","message":"MongoDB unavailable"}
            )


@app.post("/employees", status_code=201,tags=["employees"],operation_id="createEmployee")
def create_employee(body: EmployeeIn):
    if body.shift_start == body.shift_end:
        raise HTTPException(status_code = 422,detail = "shift_start must differ from shift_end")
        
    doc = body.model_dump()
    doc["created_at"] = datetime.now(timezone.utc)
    
    try :
        result = db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code = 409 , detail = "emp_code already exists ")
    doc["_id"] = result.inserted_id
    doc.pop("_id",None)
    return doc


@app.get("/employees",tags=["employees"],operation_id="listEmployees")
def list_employees(department: Optional[str] = None, page: int = Query(1,ge=1), page_size: int = Query(20,ge=1,le=100)):
    q = {}
    if department is not None :
        q["department"]=department
    total = db.employees.count_documents(q)
    skip = (page - 1)*page_size
    items = list(db.employees.find(q,{"_id":0}).sort("emp_code",1).skip(skip).limit(page_size))
    return {"items":items ,"total":total,"page":page,"page_size":page_size}


@app.post("/attendance/punch-in", status_code=201,tags=["attendance"],operation_id="puchIn")
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None :
        raise HTTPException(status_code=404,detail="Employee not found")
    status = body.status or "PRESENT"
    if status not in {"PRESENT","WFH","ON_DUTY"}:
        raise HTTPException(status_code=422,detail="Invalid attendance status")
    if body.punched_at is not None:
        ts =datetime.fromtimestamp(body.punched_at/1000,tz=timezone.utc).astimezone(IST)
    else :
        ts =  datetime.now(IST)
    ts = ts.replace(microsecond=0)
    
    shift_start = emp["shift_start"]
    shift_end = emp["shift_end"]
    
    sh,sm = map(int,shift_start.split(":"))
    eh,em = map(int,shift_end.split(":"))
    
    punch_time=ts.hour*60+ts.minute
    start_minutes = sh*60 + sm
    end_minutes = eh *60 + em
    
    attendance_date = ts.date()
    
    if end_minutes <= start_minutes and punch_time < end_minutes :
        attendance_date = -timedelta(days=1)
    date_str = attendance_date.isoformat()
    
    shift_start_dt = datetime(attendance_date.year,attendance_date.month,attendance_date.day,sh,sm,tzinfo=IST)
    elapsed_minutes =int((ts-shift_start_dt).total_seconds()//60)
    late_minutes = (elapsed_minutes if elapsed_minutes > 10 else 0)
    
    doc = {"emp_code": body.emp_code,"date": date_str,"status": status,"punch_in": ts,"punch_out": None,"work_hours": None,"late_minutes": late_minutes,"overtime_minutes": 0,"half_day": False,"history": []}
    
    try :
        result = db.attendance_logs.insert_one(doc)
    except DuplicateKerError :
        raise HTTPException(status_code = 409,detail="Already punched in for this date")
    doc.pop("_id",None)
    doc["id"]=str(result.inserted_id)
    return doc
    
@app.post("/attendance/punch-out",tags=["attendance"],operation_id="punchOut",status_code=200)
def punch_out(body:PunchOutIn):
    print(body)
    emp = db.employees.find_one({"emp_code":body.emp_code})
    if emp is None :
        raise HTTPException(status_code=404,detail="Employee not found")
    if body.punched_at is not None:
        ts=datetime.fromtimestamp(body.punched_at/1000,tz = timezone.utc).astimezone(IST)
    else :
        ts = datetime.now(IST)
    ts = ts.replace(microsecond=0)
    
    ts_utc_naive = ts.astimezone(timezone.utc).replace(tzinfo=None)
    record = db.attendance_logs.find_one({"emp_code":body.emp_code,"punch_in":{"$lte":ts_utc_naive}},sort = [("punch_in",-1),("_id",-1)] )
    if record is None :
        raise HTTPException(status_code=404,detail = "No Punch-in record found")
        
    punch_in = record["punch_in"]
    if punch_in.tzinfo is None :
        punch_in = punch_in.replace(tzinfo=timezone.utc)
    punch_in = punch_in.astimezone(IST)
    elapsed_seconds = (ts - punch_in).total_seconds()
    if elapsed_seconds <= 0 or elapsed_seconds > 24 * 60 * 60:
        raise HTTPException(status_code=422,detail="Punch-out must be after punch-in and within 24 hours")
    if record.get("punch_out") is not None:
        raise HTTPException(status_code=409,detail="This attendance record is already punched out")
     #  R4: work hours with ROUND_HALF_UP.
    seconds = Decimal(str(elapsed_seconds))
    hours = seconds / Decimal("3600")
    rounded_hours = hours.quantize(Decimal("0.01"),rounding=ROUND_HALF_UP)
    work_hours = float(rounded_hours)

    # R3: calculate overtime against the correct shift end.
    shift_start = emp["shift_start"]
    shift_end = emp["shift_end"]
    sh, sm = map(int, shift_start.split(":"))
    eh, em = map(int, shift_end.split(":"))

    attendance_date = datetime.fromisoformat(record["date"]).date()

    shift_end_dt = datetime(attendance_date.year,attendance_date.month,attendance_date.day,eh,em, tzinfo=IST)

    # Overnight shift: its end falls on the next calendar day.
    if (eh * 60 + em) <= (sh * 60 + sm):
        shift_end_dt += timedelta(days=1)

    overtime_seconds = (ts - shift_end_dt).total_seconds()
    overtime_minutes = max(0, int(overtime_seconds // 60))

    # Only count overtime if it reaches 30 whole minutes.
    if overtime_minutes < 30:
        overtime_minutes = 0

    # 8. R5: use rounded work hours.
    half_day = rounded_hours < Decimal("4.50")

    # 9. Atomic update: only one simultaneous request can close
    # this record because the update requires punch_out == None.
    updated = db.attendance_logs.find_one_and_update(
        {
            "_id": record["_id"],
            "punch_out": None
        },
        {
            "$set": {
                "punch_out": ts,
                "work_hours": work_hours,
                "overtime_minutes": overtime_minutes,
                "half_day": half_day
            }
        },
        return_document=ReturnDocument.AFTER
    )

    if updated is None:
        raise HTTPException(status_code=409,detail="This attendance record is already punched out")

    # 10. Return the updated attendance record.
    updated["id"] = str(updated.pop("_id"))

    return updated
@app.get("/attendance",tags=["attendance"],operation_id="listAttendance",)
def list_attendance(emp_code: Optional[str] = None,date_from: Optional[date] = None,date_to: Optional[date] = None,status: Optional[str] = None,page: int = Query(1, ge=1),page_size: int = Query(20, ge=1, le=100)):
    # Validate the date range
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422,detail="date_from must be less than or equal to date_to")

    # Build MongoDB query
    q = {}
    if emp_code is not None:
        q["emp_code"] = emp_code
    if date_from or date_to:
        date_filter = {}
        if date_from:
            date_filter["$gte"] = date_from.isoformat()
        if date_to:
            date_filter["$lte"] = date_to.isoformat()
        q["date"] = date_filter
    if status is not None:
        allowed_statuses = {"PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"}
        if status not in allowed_statuses:
            raise HTTPException(status_code=422,detail="Invalid attendance status")
        q["status"] = status

    # Count only matching records; do not load them into Python
    total = db.attendance_logs.count_documents(q)

    # Pagination and sorting happen in MongoDB
    skip = (page - 1) * page_size

    cursor = (db.attendance_logs.find(q, {"_id": 1, "emp_code": 1,
                                     "date": 1, "status": 1,
                                     "punch_in": 1, "punch_out": 1,
                                     "work_hours": 1, "late_minutes": 1,
                                     "overtime_minutes": 1, "half_day": 1,
                                     "history": 1}).sort([("date", -1), ("emp_code", 1)]).skip(skip).limit(page_size))

    items = []
    for doc in cursor:
        doc["id"] = str(doc.pop("_id"))
        items.append(doc)

    return {"items": items,"total": total,"page": page,"page_size": page_size}
    
    
@app.patch("/attendance/{emp_code}/{date}",tags=["attendance"],operation_id="regularizeAttendance")
def regularize_attendance(emp_code: str,date: date,body: RegularizeRequest):
    # Verify employee and attendance record.
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(status_code=404,detail="Employee not found")
    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date.isoformat()})
    if record is None:
        raise HTTPException(status_code=404,detail="Attendance record not found")
    # Capture only fields explicitly supplied by the client.
    supplied = body.model_dump(exclude_unset=True)
    if not supplied:
        raise HTTPException(status_code=422,detail="At least one field must be supplied")
    requested_status = supplied.get("status")

    if requested_status is not None and requested_status not in VALID_STATUSES:
        raise HTTPException(status_code=422,detail="Invalid attendance status")
    #A non-presence status cannot be combined with supplied punch fields.
    if (requested_status in NON_PRESENT_STATUSES and ("punch_in" in supplied or "punch_out" in supplied)):
        raise HTTPException(status_code=422,detail="ABSENT or LEAVE cannot be supplied with punch times")
    # Build the final state without modifying the stored record yet.
    old = dict(record)
    #print(old)
    old["punch_in"] = stored_timestamp_to_ist(old.get("punch_in"))
    old["punch_out"] = stored_timestamp_to_ist(old.get("punch_out"))
    final = {
        key: old.get(key)
        for key in ("status","punch_in","punch_out","late_minutes","work_hours","overtime_minutes","half_day")
        }

    for field in ("status", "punch_in", "punch_out"):
        if field in supplied:
            value = supplied[field]
            if field in {"punch_in", "punch_out"} and value is not None:
                value = normalize_timestamp(value)
            final[field] = value

    # Setting ABSENT or LEAVE clears both punch times.
    if requested_status in NON_PRESENT_STATUSES:
        final["punch_in"] = None
        final["punch_out"] = None
    #print(final)
    status = final["status"]
    punch_in = final["punch_in"]
    punch_out = final["punch_out"]

    # 4. Validate final status and punch times.
    if status in PRESENT_STATUSES and punch_in is None:
        raise HTTPException(status_code=422,detail="A presence status requires punch_in")

    if status in NON_PRESENT_STATUSES:
        if punch_in is not None or punch_out is not None:
            raise HTTPException(status_code=422,detail="ABSENT or LEAVE must not have punch times" )

    if punch_in is not None:
        calculated_date = get_attendance_date(punch_in,emp["shift_start"],emp["shift_end"])

        if calculated_date != date:
            raise HTTPException(status_code=422,detail="punch_in does not belong to the specified attendance date")

    if punch_out is not None:
        if punch_in is None:
            raise HTTPException(status_code=422,detail="punch_out requires punch_in")

        elapsed_seconds = (punch_out - punch_in).total_seconds()

        if elapsed_seconds <= 0 or elapsed_seconds > 24 * 60 * 60:
            raise HTTPException(status_code=422,detail="punch_out must be after punch_in and within 24 hours")

    # Recompute all derived fields using the final punch times.
    if punch_in is not None:
        final["late_minutes"] = compute_late_minutes(punch_in,emp["shift_start"],date)
    else:
        final["late_minutes"] = 0

    if punch_in is not None and punch_out is not None:
        final["work_hours"] = compute_work_hours(punch_in, punch_out)

        final["overtime_minutes"] = compute_overtime(punch_out,emp["shift_start"],emp["shift_end"],date)

        final["half_day"] = (Decimal(str(final["work_hours"])) < Decimal("4.50"))
    else:
        final["work_hours"] = None
        final["overtime_minutes"] = 0
        final["half_day"] = False
    #print(final)
    #  Build history changes using only values that actually changed.
    # History format assumed here:
    # {"timestamp": ..., "changes": {"field": {"old": ..., "new": ...}}}
    mutable_fields = (
        "status",
        "punch_in",
        "punch_out",
        "late_minutes",
        "work_hours",
        "overtime_minutes",
        "half_day",
    )

    changes = {}

    for field in mutable_fields:
        old_value = old.get(field)
        new_value = final.get(field)

        if old_value != new_value:
            changes[field] = {"old": old_value,"new": new_value}
    print(changes)
    if not changes:
        raise HTTPException(status_code=422,detail="Request does not change the attendance record")

    now = datetime.now(IST).replace(microsecond=0)

    history_entry = {"timestamp": now,"changes": changes}

    # 7. Atomic compare-and-update. If another request has modified the
    # record/history since it was read, this update fails rather than
    # silently losing the other request's changes.
    update_fields = {field: final[field] for field in mutable_fields if field in changes}

    updated = db.attendance_logs.find_one_and_update(
        {
            "_id": record["_id"],
            "history": record.get("history", []),
            **{
                field: record.get(field)
                for field in ("status", "punch_in", "punch_out")
            },
        },
        {
            "$set": update_fields,
            "$push": {"history": history_entry},
        },
        return_document=ReturnDocument.AFTER,
    )

    if updated is None:
        raise HTTPException(status_code=409,detail="Attendance record changed concurrently; retry the request")

    # 8. Format the response.
    updated["id"] = str(updated.pop("_id"))

    return updated
    
    
    
@app.get("/analytics/employees/{emp_code}/monthly",tags=["analytics"],operation_id="employeeMonthly",)
def employee_monthly(emp_code: str,month: str = Query(..., description="Month in YYYY-MM format"),):
    # Validate month.
    try:
        if (len(month) != 7 or month[4] != "-" or not month[:4].isdigit() or not month[5:].isdigit()):
            raise ValueError

        month_start = date.fromisoformat(f"{month}-01")

    except ValueError:
        raise HTTPException(status_code=422,detail="month must be in YYYY-MM format")

    #  Find employee.
    emp = db.employees.find_one({"emp_code": emp_code})

    if emp is None:
        raise HTTPException(status_code=404, detail="Employee not found")

    # Calculate the month's date range.
    if month_start.month == 12:
        next_month = date(month_start.year + 1, 1, 1)
    else:
        next_month = date(month_start.year, month_start.month + 1, 1)

    month_end = next_month - timedelta(days=1)

    # Assumes employees.joined_on is stored as YYYY-MM-DD.
    joined_value = emp.get("joined_on")

    if isinstance(joined_value, datetime):
        joined_on = joined_value.date()
    elif isinstance(joined_value, date):
        joined_on = joined_value
    elif joined_value:
        try:
            joined_on = date.fromisoformat(str(joined_value)[:10])
        except ValueError:
            raise HTTPException(status_code=500,detail="Invalid employee joined_on date")
    else:
        # If joined_on is optional in your schema, the employee is
        # treated as eligible from the beginning of the month.
        joined_on = month_start

    effective_start = max(month_start, joined_on)

    working_days = count_working_days(effective_start, month_end)

    # Aggregate attendance records in MongoDB.
    # ISO date strings can be compared lexicographically.
    pipeline = [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {
                    "$gte": effective_start.isoformat(),
                    "$lte": month_end.isoformat(),
                },
            }
        },
        {
            "$set": {
                "_parsed_date": {
                    "$dateFromString": {
                        "dateString": "$date",
                        "format": "%Y-%m-%d",
                    }
                }
            }
        },
        {
            "$set": {
                "_weekday": {
                    "$dayOfWeek": "$_parsed_date"
                }
            }
        },
        {
            "$group": {
                "_id": None,

                # PRESENT, WFH, and ON_DUTY count only on weekdays.
                # A half-day contributes 0.5.
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {
                                        "$in": [
                                            "$status",
                                            ["PRESENT", "WFH", "ON_DUTY"],
                                        ]
                                    },
                                    {"$gte": ["$_weekday", 2]},
                                    {"$lte": ["$_weekday", 6]},
                                ]
                            },
                            {
                                "$cond": [
                                    {"$eq": ["$half_day", True]},
                                    0.5,
                                    1.0,
                                ]
                            },
                            0,
                        ]
                    }
                },

                # Count LEAVE records in the selected period.
                "leave_days": {
                    "$sum": {
                        "$cond": [
                            {"$eq": ["$status", "LEAVE"]},
                            1,
                            0,
                        ]
                    }
                },

                # Late count includes weekend records too.
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": ["$late_minutes", 0]},
                            1,
                            0,
                        ]
                    }
                },
            }
        },
    ]

    result = list(db.attendance_logs.aggregate(pipeline))

    if result:
        summary = result[0]
        present_days = float(summary["present_days"])
        leave_days = int(summary["leave_days"])
        late_count = int(summary["late_count"])
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0

    # 5. Calculate attendance percentage using half-up rounding.
    if working_days == 0:
        attendance_pct = None
    else:
        percentage = (Decimal(str(present_days))/ Decimal(working_days)* Decimal("100"))
        attendance_pct = float(percentage.quantize(Decimal("0.0001"),rounding=ROUND_HALF_UP))

    # 6. Return monthly summary.
    return {"emp_code": emp_code,"month": month,"working_days": working_days,"present_days": present_days,"leave_days": leave_days,"late_count": late_count,"attendance_pct": attendance_pct}
    

@app.get("/analytics/departments/summary",tags=["analytics"],operation_id="departmentSummary")
def department_summary(month: str = Query(..., description="Month in YYYY-MM format"),department: str | None = None,):
    #  Validate month.
    try:
        if (len(month) != 7 or month[4] != "-" or not month[:4].isdigit() or not month[5:].isdigit()):
            raise ValueError

        month_start = date.fromisoformat(f"{month}-01")

    except ValueError:
        raise HTTPException(status_code=422,detail="month must be in YYYY-MM format")

    if month_start.month == 12:
        next_month = date(month_start.year + 1, 1, 1)
    else:
        next_month = date(month_start.year, month_start.month + 1, 1)

    month_end = next_month - timedelta(days=1)

    start_str = month_start.isoformat()
    end_str = month_end.isoformat()

    # Select employees who have joined by the end of the month.
    employee_match = {
        "joined_on": {"$lte": end_str}
    }

    if department is not None:
        employee_match["department"] = department

    # Aggregate across employees and their attendance records.
    pipeline = [
        {"$match": employee_match},

        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"employee_code": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {
                                        "$eq": [
                                            "$emp_code",
                                            "$$employee_code",
                                        ]
                                    },
                                    {"$gte": ["$date", start_str]},
                                    {"$lte": ["$date", end_str]},
                                ]
                            }
                        }
                    }
                ],
                "as": "attendance",
            }
        },

        # Keep employees with no attendance records for headcount.
        {
            "$unwind": {
                "path": "$attendance",
                "preserveNullAndEmptyArrays": True,
            }
        },

        # MongoDB $dayOfWeek: Sunday=1, Monday=2, ..., Friday=6.
        {
            "$set": {
                "_weekday": {
                    "$cond": [
                        {"$ne": [{"$ifNull": ["$attendance.date", None]}, None]},
                        {
                            "$dayOfWeek": {
                                "$dateFromString": {
                                    "dateString": "$attendance.date",
                                    "format": "%Y-%m-%d",
                                }
                            }
                        },
                        None,
                    ]
                }
            }
        },

        # First group by employee so multiple attendance records do not
        # increase headcount.
        {
            "$group": {
                "_id": {
                    "department": "$department",
                    "emp_code": "$emp_code",
                },

                "headcount": {"$first": 1},

                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {
                                        "$in": [
                                            "$attendance.status",
                                            ["PRESENT", "WFH", "ON_DUTY"],
                                        ]
                                    },
                                    {"$gte": ["$_weekday", 2]},
                                    {"$lte": ["$_weekday", 6]},
                                ]
                            },
                            {
                                "$cond": [
                                    {"$eq": ["$attendance.half_day", True]},
                                    0.5,
                                    1.0,
                                ]
                            },
                            0,
                        ]
                    }
                },

                # Sum hours only for present-status records with work_hours.
                "work_hours_sum": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {
                                        "$in": [
                                            "$attendance.status",
                                            ["PRESENT", "WFH", "ON_DUTY"],
                                        ]
                                    },
                                    {
                                        "$ne": [
                                            {
                                                "$ifNull": [
                                                    "$attendance.work_hours",
                                                    None,
                                                ]
                                            },
                                            None,
                                        ]
                                    },
                                ]
                            },
                            {"$toDouble": "$attendance.work_hours"},
                            0,
                        ]
                    }
                },

                "work_hours_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {
                                        "$in": [
                                            "$attendance.status",
                                            ["PRESENT", "WFH", "ON_DUTY"],
                                        ]
                                    },
                                    {
                                        "$ne": [
                                            {
                                                "$ifNull": [
                                                    "$attendance.work_hours",
                                                    None,
                                                ]
                                            },
                                            None,
                                        ]
                                    },
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },

                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": ["$attendance.late_minutes", 0]},
                            1,
                            0,
                        ]
                    }
                },

                "total_late_minutes": {
                    "$sum": {
                        "$cond": [
                            {"$gt": ["$attendance.late_minutes", 0]},
                            "$attendance.late_minutes",
                            0,
                        ]
                    }
                },

                "leave_count": {
                    "$sum": {
                        "$cond": [
                            {"$eq": ["$attendance.status", "LEAVE"]},
                            1,
                            0,
                        ]
                    }
                },

                "on_duty_count": {
                    "$sum": {
                        "$cond": [
                            {"$eq": ["$attendance.status", "ON_DUTY"]},
                            1,
                            0,
                        ]
                    }
                },
            }
        },

        # Then combine employee-level results by department.
        {
            "$group": {
                "_id": "$_id.department",
                "headcount": {"$sum": "$headcount"},
                "present_days": {"$sum": "$present_days"},
                "work_hours_sum": {"$sum": "$work_hours_sum"},
                "work_hours_count": {"$sum": "$work_hours_count"},
                "late_count": {"$sum": "$late_count"},
                "total_late_minutes": {"$sum": "$total_late_minutes"},
                "leave_count": {"$sum": "$leave_count"},
                "on_duty_count": {"$sum": "$on_duty_count"},
            }
        },

        # Exclude departments with zero headcount and calculate the mean
        # across qualifying attendance records, not employee averages.
        {
            "$match": {
                "headcount": {"$gt": 0}
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": 1,
                "present_days": 1,
                "avg_work_hours": {
                    "$cond": [
                        {"$gt": ["$work_hours_count", 0]},
                        {
                            "$divide": [
                                "$work_hours_sum",
                                "$work_hours_count",
                            ]
                        },
                        None,
                    ]
                },
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
            }
        },
        {"$sort": {"department": 1}},
    ]

    summaries = list(db.employees.aggregate(pipeline))

    # 4. Apply explicit half-up rounding to avg_work_hours.
    # Avoid Python's built-in round(), which uses ties-to-even.
    for item in summaries:
        avg_hours = item["avg_work_hours"]

        if avg_hours is not None:
            item["avg_work_hours"] = float(Decimal(str(avg_hours)).quantize(Decimal("0.01"),rounding=ROUND_HALF_UP))

        # Normalize integral sums to integers for clean JSON output.
        item["present_days"] = float(item["present_days"])

    return {"month": month,"items": summaries}
    
    

@app.get("/analytics/leaderboard/late",tags=["analytics"],operation_id="lateLeaderboard",)
def late_leaderboard(month: str = Query(..., description="Month in YYYY-MM format"),limit: int = Query(10, ge=1, le=50),department: str | None = None):
    #  Validate month.
    try:
        if (len(month) != 7 or month[4] != "-" or not month[:4].isdigit() or not month[5:].isdigit()):
            raise ValueError

        month_start = date.fromisoformat(f"{month}-01")

    except ValueError:
        raise HTTPException(status_code=422,detail="month must be in YYYY-MM format")

    if month_start.month == 12:
        next_month = date(month_start.year + 1, 1, 1)
    else:
        next_month = date(month_start.year, month_start.month + 1, 1)

    month_end = next_month - timedelta(days=1)

    # 2. Aggregate monthly late minutes for each employee.
    pipeline = [
        {
            "$match": {
                "date": {
                    "$gte": month_start.isoformat(),
                    "$lte": month_end.isoformat(),
                }
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$late_minutes", 0]}
                },
            }
        },
        # Only employees with positive total late minutes.
        {
            "$match": {
                "total_late_minutes": {"$gt": 0}
            }
        },
        # Get employee details; attendance logs for deleted/unknown
        # employees are ignored.
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "employee",
            }
        },
        {"$unwind": "$employee"},
    ]

    #  Apply department filtering before calculating ranks.
    if department is not None:
        pipeline.append(
            {
                "$match": {
                    "employee.department": department
                }
            }
        )

    # Competition ranking: ties share a rank and the next rank skips.
    pipeline.extend(
        [
            {
                "$setWindowFields": {
                    "sortBy": {"total_late_minutes": -1},
                    "output": {
                        "rank": {"$rank": {}}
                    },
                }
            },
            # Apply limit to rank, not to the number of rows.
            {
                "$match": {
                    "rank": {"$lte": limit}
                }
            },
            # Required final ordering.
            {
                "$sort": {
                    "total_late_minutes": -1,
                    "_id": 1,
                }
            },
            {
                "$project": {
                    "_id": 0,
                    "emp_code": "$_id",
                    "name": "$employee.name",
                    "department": "$employee.department",
                    "total_late_minutes": 1,
                    "rank": 1,
                }
            },
        ]
    )

    items = list(db.attendance_logs.aggregate(pipeline))

    return {"month": month,"items": items}
    

@app.get("/analytics/departments/{department}/trend",tags=["analytics"],operation_id="departmentTrend")
def department_trend(department: str,from_date: date = Query(..., alias="from"),to_date: date = Query(..., alias="to"),):
    # Validate date range. The maximum is 92 calendar days inclusive.
    if to_date < from_date:
        raise HTTPException(status_code=422,detail="'to' must be greater than or equal to 'from'")

    if (to_date - from_date).days + 1 > 92:
        raise HTTPException(status_code=422,detail="Date range must not exceed 92 calendar days")

    #  A department is known if at least one employee belongs to it.
    if db.employees.find_one({"department": department}, {"_id": 1}) is None:
        raise HTTPException(status_code=404,detail="Department not found")

    # Use UTC midnight as a date-series anchor. These are calendar-date
    # labels, not attendance instants.
    start_dt = datetime.combine(from_date, time.min, tzinfo=timezone.utc)
    end_dt = datetime.combine(to_date, time.min, tzinfo=timezone.utc)
    end_exclusive = end_dt + timedelta(days=1)

    pipeline = [
        # Produce one seed row. $densify generates all requested days.
        {"$match": {"department": department}},
        {"$group": {"_id": None}},
        {
            "$project": {
                "_id": 0,
                "day": {"$literal": start_dt},
            }
        },
        {
            "$densify": {
                "field": "day",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [start_dt, end_exclusive],
                },
            }
        },
        # Trim the upper boundary in case the server includes it.
        {"$match": {"day": {"$lte": end_dt}}},

        # Convert the generated date to the ISO date-string format used
        # by attendance_logs.date and employees.joined_on.
        {
            "$set": {
                "_day_string": {
                    "$dateToString": {
                        "format": "%Y-%m-%d",
                        "date": "$day",
                        "timezone": "UTC",
                    }
                },
                "_weekday": {"$dayOfWeek": "$day"},
            }
        },

        # R9: count department employees who have joined by this day.
        # Employees with no attendance logs are still included.
        {
            "$lookup": {
                "from": "employees",
                "let": {"day_string": "$_day_string"},
                "pipeline": [
                    {"$match": {"department": department}},
                    {
                        "$match": {
                            "$expr": {
                                "$lte": ["$joined_on", "$$day_string"]
                            }
                        }
                    },
                    {"$project": {"_id": 0, "emp_code": 1}},
                ],
                "as": "_employees",
            }
        },

        # Find attendance records for this date belonging to the
        # department's employees who have joined by that date.
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {
                    "day_string": "$_day_string",
                    "employee_codes": {
                        "$map": {
                            "input": "$_employees",
                            "as": "employee",
                            "in": "$$employee.emp_code",
                        }
                    },
                },
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$date", "$$day_string"]},
                                    {
                                        "$in": [
                                            "$emp_code",
                                            "$$employee_codes",
                                        ]
                                    },
                                ]
                            }
                        }
                    },
                    {
                        "$project": {
                            "_id": 0,
                            "status": 1,
                            "half_day": 1,
                            "late_minutes": 1,
                        }
                    },
                ],
                "as": "_logs",
            }
        },

        # Calculate the daily metrics. Presence counts include weekends.
        {
            "$set": {
                "is_working_day": {
                    "$in": ["$_weekday", [2, 3, 4, 5, 6]]
                },
                "headcount": {"$size": "$_employees"},
                "present_count": {
                    "$reduce": {
                        "input": "$_logs",
                        "initialValue": 0.0,
                        "in": {
                            "$add": [
                                "$$value",
                                {
                                    "$cond": [
                                        {
                                            "$in": [
                                                "$$this.status",
                                                ["PRESENT", "WFH", "ON_DUTY"],
                                            ]
                                        },
                                        {
                                            "$cond": [
                                                {"$eq": ["$$this.half_day", True]},
                                                0.5,
                                                1.0,
                                            ]
                                        },
                                        0.0,
                                    ]
                                },
                            ]
                        },
                    }
                },
                "late_count": {
                    "$size": {
                        "$filter": {
                            "input": "$_logs",
                            "as": "log",
                            "cond": {
                                "$gt": [
                                    {"$ifNull": ["$$log.late_minutes", 0]},
                                    0,
                                ]
                            },
                        }
                    }
                },
            }
        },

        # Attendance rate is rounded half-up to four decimals.
        # Since the rate is non-negative, floor(x * 10000 + 0.5)
        # implements half-up rounding.
        {
            "$set": {
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                "$is_working_day",
                                {"$gt": ["$headcount", 0]},
                            ]
                        },
                        {
                            "$divide": [
                                {
                                    "$floor": {
                                        "$add": [
                                            {
                                                "$multiply": [
                                                    {
                                                        "$divide": [
                                                            "$present_count",
                                                            "$headcount",
                                                        ]
                                                    },
                                                    10000,
                                                ]
                                            },
                                            0.5,
                                        ]
                                    }
                                },
                                10000,
                            ]
                        },
                        None,
                    ]
                }
            }
        },

        # Moving average over this row and six preceding rows.
        # MongoDB $avg ignores null attendance_rate values.
        {
            "$setWindowFields": {
                "sortBy": {"day": 1},
                "output": {
                    "_moving_avg_raw": {
                        "$avg": "$attendance_rate",
                        "window": {
                            "documents": [-6, 0]
                        },
                    }
                },
            }
        },

        # Round moving average half-up to four decimals.
        {
            "$set": {
                "moving_avg_7d": {
                    "$cond": [
                        {"$eq": ["$_moving_avg_raw", None]},
                        None,
                        {
                            "$divide": [
                                {
                                    "$floor": {
                                        "$add": [
                                            {
                                                "$multiply": [
                                                    "$_moving_avg_raw",
                                                    10000,
                                                ]
                                            },
                                            0.5,
                                        ]
                                    }
                                },
                                10000,
                            ]
                        },
                    ]
                }
            }
        },

        {
            "$project": {
                "_id": 0,
                "date": "$_day_string",
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": 1,
            }
        },
        {"$sort": {"date": 1}},
    ]

    rows = list(db.employees.aggregate(pipeline))

    return {"department": department,"from": from_date.isoformat(),"to": to_date.isoformat(),"items": rows}
    
    
    
    

@app.get("/admin/explain/{endpoint}",tags=["admin"],operation_id="explainEndpoint")
def explain_endpoint(
    endpoint: str,
    emp_code: str | None = None,
    month: str | None = None,
    department: str | None = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: date | None = Query(None, alias="date_from"),
    date_to: date | None = Query(None, alias="date_to"),
    status: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    from_date: date | None = Query(None, alias="from"),
    to_date: date | None = Query(None, alias="to"),
):
    allowed = {
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    }

    if endpoint not in allowed:
        raise HTTPException(422, detail="Invalid endpoint")

    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, detail="date_from cannot exceed date_to")

    if endpoint in {
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
    } and not month:
        raise HTTPException(422, detail="month is required")

    if month and not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
        raise HTTPException(422, detail="month must use YYYY-MM format")

    if endpoint == "employee_monthly" and not emp_code:
        raise HTTPException(422, detail="emp_code is required")

    if endpoint in {"department_summary", "department_trend"} and not department:
        raise HTTPException(422, detail="department is required")

    if endpoint == "department_trend":
        if from_date is None or to_date is None:
            raise HTTPException(422, detail="from and to are required")

        if to_date < from_date:
            raise HTTPException(422, detail="to cannot be before from")

        if (to_date - from_date).days + 1 > 92:
            raise HTTPException(422, detail="Date range cannot exceed 92 days")

        if db.employees.find_one(
            {"department": department}, {"_id": 1}
        ) is None:
            raise HTTPException(404, detail="Department not found")

    try:
        pipeline = None

        # ---------------------------------------------------------------
        # 1. Attendance list
        # ---------------------------------------------------------------
        if endpoint == "attendance_list":
            query = {}

            if date_from or date_to:
                query["date"] = {}

                if date_from:
                    query["date"]["$gte"] = date_from.isoformat()

                if date_to:
                    query["date"]["$lte"] = date_to.isoformat()

            if status:
                query["status"] = status

            result = db.command(
                "explain",
                {
                    "find": db.attendance_logs.name,
                    "filter": query,
                    "sort": {"date": -1, "emp_code": 1},
                    "skip": (page - 1) * page_size,
                    "limit": page_size,
                },
                verbosity="executionStats",
            )

            collection = db.attendance_logs.name

        else:
            # Common monthly date boundaries.
            if month:
                year, month_number = map(int, month.split("-"))

                start = date(year, month_number, 1)

                if month_number == 12:
                    next_month = date(year + 1, 1, 1)
                else:
                    next_month = date(year, month_number + 1, 1)

                end = next_month - timedelta(days=1)

                start_s = start.isoformat()
                end_s = end.isoformat()

            # -----------------------------------------------------------
            # 2. Employee monthly analytics
            # -----------------------------------------------------------
            if endpoint == "employee_monthly":
                pipeline = [
                    {
                        "$match": {
                            "emp_code": emp_code,
                            "date": {"$gte": start_s, "$lte": end_s},
                        }
                    },
                    {"$sort": {"date": 1}},
                    {
                        "$project": {
                            "_id": 0,
                            "emp_code": 1,
                            "date": 1,
                            "status": 1,
                            "punch_in": 1,
                            "punch_out": 1,
                            "work_hours": 1,
                            "late_minutes": 1,
                            "half_day": 1,
                        }
                    },
                ]

                collection = db.attendance_logs.name

            # -----------------------------------------------------------
            # 3. Department summary
            # -----------------------------------------------------------
            elif endpoint == "department_summary":
                pipeline = [
                    {
                        "$match": {
                            "joined_on": {"$lte": end_s},
                            "department": department,
                        }
                    },
                    {
                        "$lookup": {
                            "from": db.attendance_logs.name,
                            "let": {"employee_code": "$emp_code"},
                            "pipeline": [
                                {
                                    "$match": {
                                        "$expr": {
                                            "$and": [
                                                {
                                                    "$eq": [
                                                        "$emp_code",
                                                        "$$employee_code",
                                                    ]
                                                },
                                                {
                                                    "$gte": [
                                                        "$date",
                                                        start_s,
                                                    ]
                                                },
                                                {
                                                    "$lte": [
                                                        "$date",
                                                        end_s,
                                                    ]
                                                },
                                            ]
                                        }
                                    }
                                },
                                {
                                    "$project": {
                                        "_id": 0,
                                        "date": 1,
                                        "status": 1,
                                        "half_day": 1,
                                        "work_hours": 1,
                                        "late_minutes": 1,
                                    }
                                },
                            ],
                            "as": "logs",
                        }
                    },
                    {
                        "$unwind": {
                            "path": "$logs",
                            "preserveNullAndEmptyArrays": True,
                        }
                    },
                    {
                        "$set": {
                            "_weekday": {
                                "$cond": [
                                    {"$ne": ["$logs.date", None]},
                                    {
                                        "$dayOfWeek": {
                                            "$dateFromString": {
                                                "dateString": "$logs.date",
                                                "format": "%Y-%m-%d",
                                                "timezone": "UTC",
                                            }
                                        }
                                    },
                                    None,
                                ]
                            }
                        }
                    },
                    {
                        "$group": {
                            "_id": {
                                "department": "$department",
                                "emp_code": "$emp_code",
                            },
                            "present_days": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$and": [
                                                {
                                                    "$in": [
                                                        "$logs.status",
                                                        [
                                                            "PRESENT",
                                                            "WFH",
                                                            "ON_DUTY",
                                                        ],
                                                    ]
                                                },
                                                {"$ne": ["$_weekday", None]},
                                            ]
                                        },
                                        {
                                            "$cond": [
                                                {"$eq": ["$logs.half_day", True]},
                                                0.5,
                                                1.0,
                                            ]
                                        },
                                        0.0,
                                    ]
                                }
                            },
                            "late_count": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$gt": [
                                                {
                                                    "$ifNull": [
                                                        "$logs.late_minutes",
                                                        0,
                                                    ]
                                                },
                                                0,
                                            ]
                                        },
                                        1,
                                        0,
                                    ]
                                }
                            },
                            "total_late_minutes": {
                                "$sum": {
                                    "$ifNull": ["$logs.late_minutes", 0]
                                }
                            },
                            "leave_count": {
                                "$sum": {
                                    "$cond": [
                                        {"$eq": ["$logs.status", "LEAVE"]},
                                        1,
                                        0,
                                    ]
                                }
                            },
                            "on_duty_count": {
                                "$sum": {
                                    "$cond": [
                                        {"$eq": ["$logs.status", "ON_DUTY"]},
                                        1,
                                        0,
                                    ]
                                }
                            },
                            "work_hours_values": {
                                "$push": {
                                    "$cond": [
                                        {
                                            "$and": [
                                                {
                                                    "$in": [
                                                        "$logs.status",
                                                        [
                                                            "PRESENT",
                                                            "WFH",
                                                            "ON_DUTY",
                                                        ],
                                                    ]
                                                },
                                                {
                                                    "$ne": [
                                                        "$logs.work_hours",
                                                        None,
                                                    ]
                                                },
                                            ]
                                        },
                                        "$logs.work_hours",
                                        None,
                                    ]
                                }
                            },
                        }
                    },
                    {
                        "$set": {
                            "_valid_work_hours": {
                                "$filter": {
                                    "input": "$work_hours_values",
                                    "as": "hours",
                                    "cond": {"$ne": ["$$hours", None]},
                                }
                            }
                        }
                    },
                    {
                        "$group": {
                            "_id": "$_id.department",
                            "headcount": {"$sum": 1},
                            "present_days": {"$sum": "$present_days"},
                            "late_count": {"$sum": "$late_count"},
                            "total_late_minutes": {
                                "$sum": "$total_late_minutes"
                            },
                            "leave_count": {"$sum": "$leave_count"},
                            "on_duty_count": {"$sum": "$on_duty_count"},
                            "all_work_hours": {
                                "$push": "$_valid_work_hours"
                            },
                        }
                    },
                    {
                        "$set": {
                            "_all_hours": {
                                "$reduce": {
                                    "input": "$all_work_hours",
                                    "initialValue": [],
                                    "in": {
                                        "$concatArrays": [
                                            "$$value",
                                            "$$this",
                                        ]
                                    },
                                }
                            }
                        }
                    },
                    {
                        "$project": {
                            "_id": 0,
                            "department": "$_id",
                            "headcount": 1,
                            "present_days": 1,
                            "late_count": 1,
                            "total_late_minutes": 1,
                            "leave_count": 1,
                            "on_duty_count": 1,
                            "avg_work_hours": {
                                "$cond": [
                                    {"$gt": [{"$size": "$_all_hours"}, 0]},
                                    {"$avg": "$_all_hours"},
                                    None,
                                ]
                            },
                        }
                    },
                    {"$sort": {"department": 1}},
                ]

                collection = db.employees.name

            # -----------------------------------------------------------
            # 4. Late leaderboard
            # -----------------------------------------------------------
            elif endpoint == "late_leaderboard":
                pipeline = [
                    {
                        "$match": {
                            "date": {"$gte": start_s, "$lte": end_s}
                        }
                    },
                    {
                        "$group": {
                            "_id": "$emp_code",
                            "total_late_minutes": {
                                "$sum": {"$ifNull": ["$late_minutes", 0]}
                            },
                        }
                    },
                    {"$match": {"total_late_minutes": {"$gt": 0}}},
                    {
                        "$lookup": {
                            "from": db.employees.name,
                            "localField": "_id",
                            "foreignField": "emp_code",
                            "as": "employee",
                        }
                    },
                    {"$unwind": "$employee"},
                ]

                if department:
                    pipeline.append(
                        {"$match": {"employee.department": department}}
                    )

                pipeline.extend(
                    [
                        {
                            "$setWindowFields": {
                                "sortBy": {"total_late_minutes": -1},
                                "output": {
                                    "rank": {"$rank": {}}
                                },
                            }
                        },
                        {"$match": {"rank": {"$lte": limit}}},
                        {
                            "$sort": {
                                "total_late_minutes": -1,
                                "_id": 1,
                            }
                        },
                        {
                            "$project": {
                                "_id": 0,
                                "emp_code": "$_id",
                                "name": "$employee.name",
                                "department": "$employee.department",
                                "total_late_minutes": 1,
                                "rank": 1,
                            }
                        },
                    ]
                )

                collection = db.attendance_logs.name

            # -----------------------------------------------------------
            # 5. Department daily trend
            # -----------------------------------------------------------
            else:
                start_dt = datetime.combine(
                    from_date,
                    datetime.min.time(),
                    tzinfo=timezone.utc,
                )
                end_dt = datetime.combine(
                    to_date,
                    datetime.min.time(),
                    tzinfo=timezone.utc,
                )
                end_exclusive = end_dt + timedelta(days=1)

                pipeline = [
                    {"$match": {"department": department}},
                    {"$group": {"_id": None}},
                    {
                        "$project": {
                            "_id": 0,
                            "day": {"$literal": start_dt},
                        }
                    },
                    {
                        "$densify": {
                            "field": "day",
                            "range": {
                                "step": 1,
                                "unit": "day",
                                "bounds": [start_dt, end_exclusive],
                            },
                        }
                    },
                    {"$match": {"day": {"$lte": end_dt}}},
                    {
                        "$set": {
                            "_day_string": {
                                "$dateToString": {
                                    "format": "%Y-%m-%d",
                                    "date": "$day",
                                    "timezone": "UTC",
                                }
                            },
                            "_weekday": {"$dayOfWeek": "$day"},
                        }
                    },
                    {
                        "$lookup": {
                            "from": db.employees.name,
                            "let": {"day_string": "$_day_string"},
                            "pipeline": [
                                {"$match": {"department": department}},
                                {
                                    "$match": {
                                        "$expr": {
                                            "$lte": [
                                                "$joined_on",
                                                "$$day_string",
                                            ]
                                        }
                                    }
                                },
                                {"$project": {"_id": 0, "emp_code": 1}},
                            ],
                            "as": "_employees",
                        }
                    },
                    {
                        "$lookup": {
                            "from": db.attendance_logs.name,
                            "let": {
                                "day_string": "$_day_string",
                                "employee_codes": {
                                    "$map": {
                                        "input": "$_employees",
                                        "as": "employee",
                                        "in": "$$employee.emp_code",
                                    }
                                },
                            },
                            "pipeline": [
                                {
                                    "$match": {
                                        "$expr": {
                                            "$and": [
                                                {
                                                    "$eq": [
                                                        "$date",
                                                        "$$day_string",
                                                    ]
                                                },
                                                {
                                                    "$in": [
                                                        "$emp_code",
                                                        "$$employee_codes",
                                                    ]
                                                },
                                            ]
                                        }
                                    }
                                },
                                {
                                    "$project": {
                                        "_id": 0,
                                        "status": 1,
                                        "half_day": 1,
                                        "late_minutes": 1,
                                    }
                                },
                            ],
                            "as": "_logs",
                        }
                    },
                    {
                        "$set": {
                            "is_working_day": {
                                "$in": ["$_weekday", [2, 3, 4, 5, 6]]
                            },
                            "headcount": {"$size": "$_employees"},
                            "present_count": {
                                "$reduce": {
                                    "input": "$_logs",
                                    "initialValue": 0.0,
                                    "in": {
                                        "$add": [
                                            "$$value",
                                            {
                                                "$cond": [
                                                    {
                                                        "$in": [
                                                            "$$this.status",
                                                            [
                                                                "PRESENT",
                                                                "WFH",
                                                                "ON_DUTY",
                                                            ],
                                                        ]
                                                    },
                                                    {
                                                        "$cond": [
                                                            {
                                                                "$eq": [
                                                                    "$$this.half_day",
                                                                    True,
                                                                ]
                                                            },
                                                            0.5,
                                                            1.0,
                                                        ]
                                                    },
                                                    0.0,
                                                ]
                                            },
                                        ]
                                    },
                                }
                            },
                            "late_count": {
                                "$size": {
                                    "$filter": {
                                        "input": "$_logs",
                                        "as": "log",
                                        "cond": {
                                            "$gt": [
                                                {
                                                    "$ifNull": [
                                                        "$$log.late_minutes",
                                                        0,
                                                    ]
                                                },
                                                0,
                                            ]
                                        },
                                    }
                                }
                            },
                        }
                    },
                    {
                        "$set": {
                            "attendance_rate": {
                                "$cond": [
                                    {
                                        "$and": [
                                            "$is_working_day",
                                            {"$gt": ["$headcount", 0]},
                                        ]
                                    },
                                    {
                                        "$divide": [
                                            {
                                                "$floor": {
                                                    "$add": [
                                                        {
                                                            "$multiply": [
                                                                {
                                                                    "$divide": [
                                                                        "$present_count",
                                                                        "$headcount",
                                                                    ]
                                                                },
                                                                10000,
                                                            ]
                                                        },
                                                        0.5,
                                                    ]
                                                }
                                            },
                                            10000,
                                        ]
                                    },
                                    None,
                                ]
                            }
                        }
                    },
                    {
                        "$setWindowFields": {
                            "sortBy": {"day": 1},
                            "output": {
                                "_moving_avg_raw": {
                                    "$avg": "$attendance_rate",
                                    "window": {"documents": [-6, 0]},
                                }
                            },
                        }
                    },
                    {
                        "$set": {
                            "moving_avg_7d": {
                                "$cond": [
                                    {"$eq": ["$_moving_avg_raw", None]},
                                    None,
                                    {
                                        "$divide": [
                                            {
                                                "$floor": {
                                                    "$add": [
                                                        {
                                                            "$multiply": [
                                                                "$_moving_avg_raw",
                                                                10000,
                                                            ]
                                                        },
                                                        0.5,
                                                    ]
                                                }
                                            },
                                            10000,
                                        ]
                                    },
                                ]
                            }
                        }
                    },
                    {
                        "$project": {
                            "_id": 0,
                            "date": "$_day_string",
                            "is_working_day": 1,
                            "headcount": 1,
                            "present_count": 1,
                            "late_count": 1,
                            "attendance_rate": 1,
                            "moving_avg_7d": 1,
                        }
                    },
                    {"$sort": {"date": 1}},
                ]

                collection = db.employees.name

            result = db.command(
                "explain",
                {
                    "aggregate": collection,
                    "pipeline": pipeline,
                    "cursor": {},
                },
                verbosity="executionStats",
            )

        return Response(
            content=json_util.dumps(
                {
                    "endpoint": endpoint,
                    "collection": collection,
                    "explain": result,
                }
            ),
            media_type="application/json",
        )

    except HTTPException:
        raise
    except PyMongoError as exc:
        raise HTTPException(
            status_code=500,
            detail=f"MongoDB explain failed: {str(exc)}",
        ) from exc
# --------------------------------------------------------------------------- #
# TODO - the rest of the contract (see openapi.yaml):
#   POST  /attendance/punch-out
#   PATCH /attendance/{emp_code}/{date}
#   GET   /analytics/employees/{emp_code}/monthly
#   GET   /analytics/departments/summary
#   GET   /analytics/leaderboard/late
#   GET   /analytics/departments/{department}/trend
#   GET   /admin/explain/{endpoint}
# --------------------------------------------------------------------------- #
