"""
Employee Attendance & Analytics API - STARTER

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)

This file was written quickly by a colleague who has left the company. The happy path
works, but nobody has reviewed it. Read PROBLEM_STATEMENT.docx for what is expected of you,
openapi.yaml for the contract and DATA_MODEL.md for what is stored in MongoDB.
"""
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError

load_dotenv()

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def compute_late_minutes(punch_in: datetime, shift_start: str) -> int:
    h, m = map(int, shift_start.split(":"))
    start = punch_in.replace(hour=h, minute=m, second=0, microsecond=0)
    minutes = int((punch_in - start).total_seconds() / 60)
    return minutes if minutes > 10 else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    return round((punch_out - punch_in).total_seconds() / 3600, 2)


def compute_overtime(punch_out: datetime, shift_end: str, date_str: str) -> int:
    h, m = map(int, shift_end.split(":"))
    end = datetime.fromisoformat(date_str).replace(hour=h, minute=m, tzinfo=IST)
    return max(0, int((punch_out - end).total_seconds() / 60))


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


@app.post("/employees", status_code=201,tags=["employees"],operation_id="createEmployee)
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
def list_employees(department: Optional[str] = None, page: int = Query(1,ge=1), page_size: Query(20,ge=1,le=100)):
    q = {}
    if department is not None :
        q["department"]=department
    total = db.employees.count_documents(q)
    skip = (page - 1)*page_size
    items = list(db.employees.find(q,{"_id":0}).sort("emp_code",1).skip(skip).limit(page_size))
    return {"items":items ,"total":total,"page":page,"page_size":page_size}


@app.post("/attendance/punch-in", status_code=201,tags=201,tags=["attendance"],operation_id="puchIn")
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None :
        raise HTTPException(status_code=404,detail="Employee not found")
    status = body.status or "PRESENT"
    if status not in {"PRESENT","WFH","ON_DUTY"}:
        raise HTTPException(status_code=422,detail="Invalid attendance status")
    if body.punched_ar is not None:
        ts =datetime.fromtimestamp(body.punched_at/1000,tz=timezone.utc).astimezone(IST)
    else :
        ts =  datetime.now(IST)
    ts = ts.replace(microsecond=0)
    
    shift_start = emp["shift_start"]
    shift_end = emp["shift_end"]
    
    sh,sm = map(int,shift_start.split(":"))
    eh,em = map(int(shift_end.split(":"))
    
    punch_time=ts.hour*60+ts.minute
    start_minutes = sh*60 + sm
    end_minutes = eh *60 + em
    
    attendacne_date = ts.date()
    
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
    emp = db.employee.find_one({"emp_code":body.emp_code})
    if emp is None :
        raise HTTPException(status_code=404,detail="Employee not found")
    if body.punched_at is not None:
        ts.datetime.fromtimestamp(body.punched_at/1000,tz = timezone.utc).astimezone(IST)
    else :
        ts = datetime.now(IST)
    ts = ts.replace(microsecond=0)
    
    ts_utc_naive = ts.astimezone(timezone.utc).replace(tzinfo=None)
    record = db.attendance_logs.find_one({"emp_code":body.emp_code,"punch_in":{"$lte":ts_utc_naive}},sort = [("punch_in",-1),("_id",-1)] )
    if record in None :
        raise HTTPException(status_code=404.detail = "No Punch-in record found")
        
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
