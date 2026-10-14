# """
# Employee Attendance & Analytics API

# Run:  uvicorn app.main:app --port 8000
# Env:  MONGO_URI, MONGO_DB (a local .env is fine)
# """
# import os
# import re
# from datetime import datetime, timedelta, timezone, date
# from decimal import Decimal, ROUND_HALF_UP
# from typing import Optional

# from dotenv import load_dotenv
# from fastapi import FastAPI, HTTPException, Query
# from fastapi.responses import JSONResponse
# from pydantic import BaseModel, field_validator, model_validator
# from pymongo import MongoClient, ASCENDING, DESCENDING
# from pymongo.errors import DuplicateKeyError

# load_dotenv()

# # ---------------------------------------------------------------------------
# # DB setup
# # ---------------------------------------------------------------------------
# MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
# MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

# client = MongoClient(MONGO_URI)
# db = client[MONGO_DB]

# IST = timezone(timedelta(hours=5, minutes=30))

# app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")

# # ---------------------------------------------------------------------------
# # Startup: create indexes idempotently
# # ---------------------------------------------------------------------------
# @app.on_event("startup")
# def create_indexes():
#     # employees: unique on emp_code (enforces concurrent-safe duplicate creates)
#     db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="emp_code_unique")

#     # attendance_logs: unique compound (emp_code, date) for concurrency-safe punch-in
#     db.attendance_logs.create_index(
#         [("emp_code", ASCENDING), ("date", ASCENDING)],
#         unique=True,
#         name="emp_date_unique",
#     )
#     # Support list queries: sort by date desc + emp_code asc, with all filter fields
#     db.attendance_logs.create_index(
#         [("date", DESCENDING), ("emp_code", ASCENDING)],
#         name="date_desc_emp_asc",
#     )
#     # Fast lookup by emp_code alone (used by analytics)
#     db.attendance_logs.create_index([("emp_code", ASCENDING)], name="emp_code_idx")


# # ---------------------------------------------------------------------------
# # Constants & helpers
# # ---------------------------------------------------------------------------
# PRESENCE_STATUSES = {"PRESENT", "WFH", "ON_DUTY"}
# ALL_STATUSES = {"PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"}

# EPOCH_MIN = 100_000_000_000   # 1973-03-03 – values below this look like seconds
# EPOCH_MAX = 4_102_444_800_000  # 2100-01-01


# def half_up(value: float, decimals: int) -> float:
#     """Round half-up to `decimals` places (not banker's rounding)."""
#     q = Decimal(10) ** -decimals
#     return float(Decimal(str(value)).quantize(q, rounding=ROUND_HALF_UP))


# def ms_to_dt(ms: int) -> datetime:
#     """Epoch milliseconds → aware UTC datetime truncated to seconds."""
#     return datetime.fromtimestamp(ms // 1000, tz=timezone.utc).replace(microsecond=0)


# def dt_to_ms(dt: datetime) -> int:
#     """Aware or naive datetime → epoch milliseconds."""
#     if dt.tzinfo is None:
#         dt = dt.replace(tzinfo=timezone.utc)
#     return int(dt.timestamp() * 1000)


# def dt_to_ist(dt: datetime) -> datetime:
#     """Convert a UTC datetime (naive or aware) to an IST-aware datetime."""
#     if dt.tzinfo is None:
#         dt = dt.replace(tzinfo=timezone.utc)
#     return dt.astimezone(IST)


# def attendance_date_for(punch_in: datetime, shift_start: str, shift_end: str) -> str:
#     """
#     R1: Return the IST calendar date string for the attendance record.
#     For overnight shifts (shift_end <= shift_start), a punch-in whose IST
#     time is earlier than shift_end belongs to the shift that started the
#     previous day.
#     """
#     ist = dt_to_ist(punch_in)
#     # Overnight shift check
#     s_h, s_m = map(int, shift_start.split(":"))
#     e_h, e_m = map(int, shift_end.split(":"))
#     is_overnight = (e_h * 60 + e_m) <= (s_h * 60 + s_m)
#     if is_overnight:
#         punch_ist_minutes = ist.hour * 60 + ist.minute
#         shift_end_minutes = e_h * 60 + e_m
#         if punch_ist_minutes < shift_end_minutes:
#             # This punch is before shift_end → belongs to previous calendar day
#             return (ist.date() - timedelta(days=1)).isoformat()
#     return ist.date().isoformat()


# def compute_late_minutes(punch_in: datetime, shift_start: str, attendance_date: str) -> int:
#     """
#     R2: Grace period 10 minutes. Late only if strictly more than 10:00 after shift_start.
#     `late_minutes` = floor of whole minutes since shift_start (not since end of grace).
#     punch_in is a UTC datetime (naive); attendance_date is the IST attendance date string.
#     """
#     h, m = map(int, shift_start.split(":"))
#     # Build shift_start as an IST-aware datetime on the attendance day
#     base_date = date.fromisoformat(attendance_date)
#     shift_start_ist = datetime(
#         base_date.year, base_date.month, base_date.day, h, m, 0, tzinfo=IST
#     )
#     # Convert punch_in to IST-aware
#     if punch_in.tzinfo is None:
#         punch_in_aware = punch_in.replace(tzinfo=timezone.utc)
#     else:
#         punch_in_aware = punch_in
#     delta_secs = (punch_in_aware - shift_start_ist).total_seconds()
#     # Grace: strictly more than 10 minutes (600 seconds) late
#     if delta_secs <= 600:
#         return 0
#     return int(delta_secs // 60)


# def compute_overtime_minutes(punch_out: datetime, shift_end: str, attendance_date: str, shift_start: str) -> int:
#     """
#     R3: overtime = floor minutes between shift_end and punch_out, only if >= 30; else 0.
#     For overnight shifts, shift_end is on the day after attendance_date.
#     """
#     s_h, s_m = map(int, shift_start.split(":"))
#     e_h, e_m = map(int, shift_end.split(":"))
#     is_overnight = (e_h * 60 + e_m) <= (s_h * 60 + s_m)
#     base_date = date.fromisoformat(attendance_date)
#     if is_overnight:
#         end_date = base_date + timedelta(days=1)
#     else:
#         end_date = base_date
#     shift_end_ist = datetime(
#         end_date.year, end_date.month, end_date.day, e_h, e_m, 0, tzinfo=IST
#     )
#     if punch_out.tzinfo is None:
#         punch_out_aware = punch_out.replace(tzinfo=timezone.utc)
#     else:
#         punch_out_aware = punch_out
#     delta_secs = (punch_out_aware - shift_end_ist).total_seconds()
#     minutes = int(delta_secs // 60)
#     return minutes if minutes >= 30 else 0


# def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
#     """R4: (punch_out - punch_in) / 3600, rounded half-up to 2 decimals."""
#     if punch_in.tzinfo is None:
#         punch_in = punch_in.replace(tzinfo=timezone.utc)
#     if punch_out.tzinfo is None:
#         punch_out = punch_out.replace(tzinfo=timezone.utc)
#     secs = (punch_out - punch_in).total_seconds()
#     return half_up(secs / 3600, 2)


# def compute_derived(punch_in: Optional[datetime], punch_out: Optional[datetime],
#                     shift_start: str, shift_end: str, attendance_date: str, status: str):
#     """Compute all four derived fields given the current state of the record."""
#     if status not in PRESENCE_STATUSES:
#         return {"late_minutes": 0, "overtime_minutes": 0, "work_hours": None, "half_day": False}
#     if punch_in is None:
#         return {"late_minutes": 0, "overtime_minutes": 0, "work_hours": None, "half_day": False}

#     late_minutes = compute_late_minutes(punch_in, shift_start, attendance_date)
#     if punch_out is None:
#         return {
#             "late_minutes": late_minutes,
#             "overtime_minutes": 0,
#             "work_hours": None,
#             "half_day": False,
#         }

#     work_hours = compute_work_hours(punch_in, punch_out)
#     overtime_minutes = compute_overtime_minutes(punch_out, shift_end, attendance_date, shift_start)
#     half_day = work_hours < 4.50  # R5: based on the rounded work_hours
#     return {
#         "late_minutes": late_minutes,
#         "overtime_minutes": overtime_minutes,
#         "work_hours": work_hours,
#         "half_day": half_day,
#     }


# def serialize_record(doc: dict) -> dict:
#     """Convert a MongoDB attendance document to the API response shape."""
#     def _maybe_ms(v):
#         if v is None:
#             return None
#         if isinstance(v, datetime):
#             return dt_to_ms(v)
#         return v

#     return {
#         "emp_code": doc["emp_code"],
#         "date": doc["date"],
#         "status": doc["status"],
#         "punch_in": _maybe_ms(doc.get("punch_in")),
#         "punch_out": _maybe_ms(doc.get("punch_out")),
#         "work_hours": doc.get("work_hours"),
#         "late_minutes": doc.get("late_minutes", 0),
#         "overtime_minutes": doc.get("overtime_minutes", 0),
#         "half_day": doc.get("half_day", False),
#         "history": [serialize_history_entry(h) for h in doc.get("history", [])],
#     }


# def serialize_history_entry(entry: dict) -> dict:
#     def _convert_change_value(v):
#         if isinstance(v, datetime):
#             return dt_to_ms(v)
#         return v

#     changes = {}
#     for field, chg in entry.get("changes", {}).items():
#         changes[field] = {
#             "from": _convert_change_value(chg.get("from")),
#             "to": _convert_change_value(chg.get("to")),
#         }

#     at_val = entry.get("at")
#     at_ms = dt_to_ms(at_val) if isinstance(at_val, datetime) else at_val

#     return {
#         "at": at_ms,
#         "by": entry["by"],
#         "reason": entry["reason"],
#         "changes": changes,
#     }


# def serialize_employee(doc: dict) -> dict:
#     return {
#         "emp_code": doc["emp_code"],
#         "name": doc["name"],
#         "email": doc["email"],
#         "department": doc["department"],
#         "shift_start": doc["shift_start"],
#         "shift_end": doc["shift_end"],
#         "joined_on": doc["joined_on"],
#         "created_at": dt_to_ms(doc["created_at"]) if isinstance(doc.get("created_at"), datetime) else doc.get("created_at"),
#     }


# # ---------------------------------------------------------------------------
# # Pydantic models
# # ---------------------------------------------------------------------------
# class EmployeeCreate(BaseModel):
#     emp_code: str
#     name: str
#     email: str
#     department: str
#     shift_start: str = "09:30"
#     shift_end: str = "18:30"
#     joined_on: str

#     @field_validator("emp_code")
#     @classmethod
#     def validate_emp_code(cls, v):
#         if not re.match(r'^EMP\d{4,6}$', v):
#             raise ValueError("emp_code must match EMP followed by 4-6 digits")
#         return v

#     @field_validator("name")
#     @classmethod
#     def validate_name(cls, v):
#         if not (1 <= len(v) <= 100):
#             raise ValueError("name must be 1-100 characters")
#         return v

#     @field_validator("email")
#     @classmethod
#     def validate_email(cls, v):
#         if not re.match(r'^[^@\s]+@[^@\s]+\.[^@\s]+$', v):
#             raise ValueError("invalid email format")
#         if len(v) > 120:
#             raise ValueError("email too long")
#         return v

#     @field_validator("department")
#     @classmethod
#     def validate_department(cls, v):
#         if not (1 <= len(v) <= 50):
#             raise ValueError("department must be 1-50 characters")
#         return v

#     @field_validator("shift_start", "shift_end")
#     @classmethod
#     def validate_shift_time(cls, v):
#         if not re.match(r'^([01]\d|2[0-3]):[0-5]\d$', v):
#             raise ValueError("shift time must be HH:MM (24-hour)")
#         return v

#     @field_validator("joined_on")
#     @classmethod
#     def validate_joined_on(cls, v):
#         try:
#             date.fromisoformat(v)
#         except ValueError:
#             raise ValueError("joined_on must be YYYY-MM-DD")
#         return v

#     @model_validator(mode="after")
#     def shift_start_ne_end(self):
#         if self.shift_start == self.shift_end:
#             raise ValueError("shift_start must differ from shift_end")
#         return self


# class PunchInRequest(BaseModel):
#     emp_code: str
#     punched_at: Optional[int] = None
#     status: str = "PRESENT"

#     @field_validator("punched_at")
#     @classmethod
#     def validate_punched_at(cls, v):
#         if v is not None:
#             if not isinstance(v, int):
#                 raise ValueError("punched_at must be an integer epoch milliseconds")
#             if v < EPOCH_MIN or v > EPOCH_MAX:
#                 raise ValueError("punched_at out of valid range (must be epoch milliseconds)")
#         return v

#     @field_validator("status")
#     @classmethod
#     def validate_status(cls, v):
#         if v not in PRESENCE_STATUSES:
#             raise ValueError(f"status must be one of {sorted(PRESENCE_STATUSES)}")
#         return v


# class PunchOutRequest(BaseModel):
#     emp_code: str
#     punched_at: Optional[int] = None

#     @field_validator("punched_at")
#     @classmethod
#     def validate_punched_at(cls, v):
#         if v is not None:
#             if not isinstance(v, int):
#                 raise ValueError("punched_at must be an integer epoch milliseconds")
#             if v < EPOCH_MIN or v > EPOCH_MAX:
#                 raise ValueError("punched_at out of valid range (must be epoch milliseconds)")
#         return v


# class RegularizeRequest(BaseModel):
#     status: Optional[str] = None
#     punch_in: Optional[int] = None
#     punch_out: Optional[int] = None
#     reason: str
#     regularized_by: str

#     @field_validator("punch_in", "punch_out")
#     @classmethod
#     def validate_ts(cls, v):
#         if v is not None:
#             if not isinstance(v, int):
#                 raise ValueError("must be integer epoch milliseconds")
#             if v < EPOCH_MIN or v > EPOCH_MAX:
#                 raise ValueError("out of valid epoch milliseconds range")
#         return v

#     @field_validator("status")
#     @classmethod
#     def validate_status(cls, v):
#         if v is not None and v not in ALL_STATUSES:
#             raise ValueError(f"status must be one of {sorted(ALL_STATUSES)}")
#         return v

#     @field_validator("reason")
#     @classmethod
#     def validate_reason(cls, v):
#         if not (5 <= len(v) <= 200):
#             raise ValueError("reason must be 5-200 characters")
#         return v

#     @field_validator("regularized_by")
#     @classmethod
#     def validate_regularized_by(cls, v):
#         if not (1 <= len(v) <= 50):
#             raise ValueError("regularized_by must be 1-50 characters")
#         return v


# # ---------------------------------------------------------------------------
# # Endpoints: system
# # ---------------------------------------------------------------------------
# @app.get("/health")
# def health():
#     try:
#         db.command("ping")
#         return {"status": "ok"}
#     except Exception:
#         return JSONResponse(status_code=503, content={"detail": "MongoDB not available"})


# # ---------------------------------------------------------------------------
# # Endpoints: employees
# # ---------------------------------------------------------------------------
# @app.post("/employees", status_code=201)
# def create_employee(body: EmployeeCreate):
#     doc = body.model_dump()
#     doc["created_at"] = datetime.now(tz=timezone.utc)
#     try:
#         db.employees.insert_one(doc)
#     except DuplicateKeyError:
#         raise HTTPException(status_code=409, detail="emp_code already exists")
#     return serialize_employee(doc)


# @app.get("/employees")
# def list_employees(
#     department: Optional[str] = None,
#     page: int = Query(default=1, ge=1),
#     page_size: int = Query(default=20, ge=1, le=100),
# ):
#     q = {}
#     if department:
#         q["department"] = department
#     skip = (page - 1) * page_size
#     total = db.employees.count_documents(q)
#     items = list(db.employees.find(q, {"_id": 0}).sort("emp_code", ASCENDING).skip(skip).limit(page_size))
#     return {
#         "items": [serialize_employee(e) for e in items],
#         "total": total,
#         "page": page,
#         "page_size": page_size,
#     }


# # ---------------------------------------------------------------------------
# # Endpoints: attendance
# # ---------------------------------------------------------------------------
# @app.post("/attendance/punch-in", status_code=201)
# def punch_in(body: PunchInRequest):
#     emp = db.employees.find_one({"emp_code": body.emp_code})
#     if not emp:
#         raise HTTPException(status_code=404, detail="Employee not found")

#     # Determine punch-in time (truncate to seconds per R1)
#     if body.punched_at is not None:
#         ts = ms_to_dt(body.punched_at)
#     else:
#         ts = datetime.now(tz=timezone.utc).replace(microsecond=0)

#     attendance_date = attendance_date_for(ts, emp["shift_start"], emp["shift_end"])
#     late_minutes = compute_late_minutes(ts, emp["shift_start"], attendance_date)

#     doc = {
#         "emp_code": body.emp_code,
#         "date": attendance_date,
#         "status": body.status,
#         "punch_in": ts,
#         "punch_out": None,
#         "work_hours": None,
#         "late_minutes": late_minutes,
#         "overtime_minutes": 0,
#         "half_day": False,
#         "history": [],
#     }
#     try:
#         db.attendance_logs.insert_one(doc)
#     except DuplicateKeyError:
#         raise HTTPException(status_code=409, detail="Already punched in for this attendance date")

#     return serialize_record(doc)


# @app.post("/attendance/punch-out")
# def punch_out(body: PunchOutRequest):
#     emp = db.employees.find_one({"emp_code": body.emp_code})
#     if not emp:
#         raise HTTPException(status_code=404, detail="Employee not found")

#     if body.punched_at is not None:
#         ts = ms_to_dt(body.punched_at)
#     else:
#         ts = datetime.now(tz=timezone.utc).replace(microsecond=0)

#     # Find the most recent punch-in record at or before punched_at (for overnight support)
#     record = db.attendance_logs.find_one(
#         {
#             "emp_code": body.emp_code,
#             "punch_in": {"$lte": ts},
#             "punch_out": None,
#             "status": {"$in": list(PRESENCE_STATUSES)},
#         },
#         sort=[("punch_in", DESCENDING)],
#     )
#     if not record:
#         raise HTTPException(status_code=404, detail="No open punch-in record found for this employee")

#     punch_in_dt = record["punch_in"]
#     if punch_in_dt.tzinfo is None:
#         punch_in_dt = punch_in_dt.replace(tzinfo=timezone.utc)

#     # Validate: punch_out > punch_in and within 24 hours
#     if ts <= punch_in_dt:
#         raise HTTPException(status_code=422, detail="punched_at must be after punch_in")
#     if (ts - punch_in_dt).total_seconds() > 86400:
#         raise HTTPException(status_code=422, detail="punched_at is more than 24 hours after punch_in")

#     derived = compute_derived(
#         punch_in_dt, ts,
#         emp["shift_start"], emp["shift_end"],
#         record["date"], record["status"]
#     )

#     # Atomic update: only if punch_out is still None (concurrency safety)
#     result = db.attendance_logs.find_one_and_update(
#         {"_id": record["_id"], "punch_out": None},
#         {
#             "$set": {
#                 "punch_out": ts,
#                 "work_hours": derived["work_hours"],
#                 "overtime_minutes": derived["overtime_minutes"],
#                 "half_day": derived["half_day"],
#                 "late_minutes": derived["late_minutes"],
#             }
#         },
#         return_document=True,
#     )
#     if result is None:
#         raise HTTPException(status_code=409, detail="Record already punched out (concurrent request)")

#     return serialize_record(result)


# @app.get("/attendance")
# def list_attendance(
#     emp_code: Optional[str] = None,
#     date_from: Optional[str] = None,
#     date_to: Optional[str] = None,
#     status: Optional[str] = None,
#     page: int = Query(default=1, ge=1),
#     page_size: int = Query(default=20, ge=1, le=100),
# ):
#     # Validate date_from <= date_to
#     if date_from and date_to and date_from > date_to:
#         raise HTTPException(status_code=422, detail="date_from must not be after date_to")
#     if status and status not in ALL_STATUSES:
#         raise HTTPException(status_code=422, detail=f"Invalid status: {status}")

#     q: dict = {}
#     if emp_code:
#         q["emp_code"] = emp_code
#     if date_from or date_to:
#         q["date"] = {}
#         if date_from:
#             q["date"]["$gte"] = date_from
#         if date_to:
#             q["date"]["$lte"] = date_to
#     if status:
#         q["status"] = status

#     skip = (page - 1) * page_size
#     total = db.attendance_logs.count_documents(q)
#     docs = list(
#         db.attendance_logs.find(q, {"_id": 0})
#         .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
#         .skip(skip)
#         .limit(page_size)
#     )
#     return {
#         "items": [serialize_record(d) for d in docs],
#         "total": total,
#         "page": page,
#         "page_size": page_size,
#     }


# @app.patch("/attendance/{emp_code}/{date}")
# def regularize_attendance(emp_code: str, date: str, body: RegularizeRequest):
#     # Validate date format
#     try:
#         date_obj = datetime.strptime(date, "%Y-%m-%d").date()
#     except ValueError:
#         raise HTTPException(status_code=422, detail="date must be YYYY-MM-DD")

#     emp = db.employees.find_one({"emp_code": emp_code})
#     if not emp:
#         raise HTTPException(status_code=404, detail="Employee not found")

#     record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
#     if not record:
#         raise HTTPException(status_code=404, detail="Attendance record not found for that date")

#     # Business-rule validations
#     new_status = body.status if body.status is not None else record["status"]
#     new_pi_ms = body.punch_in
#     new_po_ms = body.punch_out

#     # Setting ABSENT or LEAVE: must not provide punch times
#     if new_status in ("ABSENT", "LEAVE"):
#         if new_pi_ms is not None or new_po_ms is not None:
#             raise HTTPException(
#                 status_code=422,
#                 detail="Cannot supply punch_in or punch_out when setting status to ABSENT or LEAVE",
#             )

#     # Convert stored punch times to aware UTC datetimes
#     stored_pi = record.get("punch_in")
#     if stored_pi and stored_pi.tzinfo is None:
#         stored_pi = stored_pi.replace(tzinfo=timezone.utc)
#     stored_po = record.get("punch_out")
#     if stored_po and stored_po.tzinfo is None:
#         stored_po = stored_po.replace(tzinfo=timezone.utc)

#     # Determine final punch times
#     if new_status in ("ABSENT", "LEAVE"):
#         final_pi = None
#         final_po = None
#     else:
#         final_pi = ms_to_dt(new_pi_ms) if new_pi_ms is not None else stored_pi
#         final_po = ms_to_dt(new_po_ms) if new_po_ms is not None else stored_po

#     # Presence status requires punch_in
#     if new_status in PRESENCE_STATUSES and final_pi is None:
#         raise HTTPException(status_code=422, detail="A presence status requires punch_in")

#     # punch_in must be on the attendance date (R1)
#     if final_pi is not None:
#         pi_ist_date = dt_to_ist(final_pi).date().isoformat()
#         # For overnight: also check the previous day
#         pi_attendance_date = attendance_date_for(final_pi, emp["shift_start"], emp["shift_end"])
#         if pi_attendance_date != date:
#             raise HTTPException(
#                 status_code=422,
#                 detail="punch_in must belong to the attendance date (R1)",
#             )

#     # punch_out must be after punch_in and within 24 hours
#     if final_po is not None:
#         if final_pi is None:
#             raise HTTPException(status_code=422, detail="punch_out requires a punch_in")
#         if final_po <= final_pi:
#             raise HTTPException(status_code=422, detail="punch_out must be after punch_in")
#         if (final_po - final_pi).total_seconds() > 86400:
#             raise HTTPException(status_code=422, detail="punch_out more than 24 hours after punch_in")

#     # Compute derived fields
#     derived = compute_derived(final_pi, final_po, emp["shift_start"], emp["shift_end"], date, new_status)

#     # Build the set of changes (only fields that actually changed)
#     field_mapping = {
#         "status": (record.get("status"), new_status),
#         "punch_in": (stored_pi, final_pi),
#         "punch_out": (stored_po, final_po),
#         "late_minutes": (record.get("late_minutes", 0), derived["late_minutes"]),
#         "overtime_minutes": (record.get("overtime_minutes", 0), derived["overtime_minutes"]),
#         "work_hours": (record.get("work_hours"), derived["work_hours"]),
#         "half_day": (record.get("half_day", False), derived["half_day"]),
#     }

#     changes = {}
#     for field, (old_val, new_val) in field_mapping.items():
#         # Compare with type-aware equality for datetimes
#         if field in ("punch_in", "punch_out"):
#             old_ms = dt_to_ms(old_val) if old_val is not None else None
#             new_ms = dt_to_ms(new_val) if new_val is not None else None
#             if old_ms != new_ms:
#                 changes[field] = {"from": old_ms, "to": new_ms}
#         else:
#             if old_val != new_val:
#                 changes[field] = {"from": old_val, "to": new_val}

#     if not changes:
#         raise HTTPException(status_code=422, detail="Request would change nothing")

#     history_entry = {
#         "at": datetime.now(tz=timezone.utc),
#         "by": body.regularized_by,
#         "reason": body.reason,
#         "changes": {
#             k: {
#                 "from": datetime.fromtimestamp(v["from"] / 1000, tz=timezone.utc) if k in ("punch_in", "punch_out") and v["from"] is not None else v["from"],
#                 "to": datetime.fromtimestamp(v["to"] / 1000, tz=timezone.utc) if k in ("punch_in", "punch_out") and v["to"] is not None else v["to"],
#             }
#             for k, v in changes.items()
#         },
#     }

#     update_doc = {
#         "$set": {
#             "status": new_status,
#             "punch_in": final_pi,
#             "punch_out": final_po,
#             "work_hours": derived["work_hours"],
#             "late_minutes": derived["late_minutes"],
#             "overtime_minutes": derived["overtime_minutes"],
#             "half_day": derived["half_day"],
#         },
#         "$push": {"history": history_entry},
#     }

#     # Optimistic concurrency: check current history length
#     current_history_len = len(record.get("history", []))
#     result = db.attendance_logs.find_one_and_update(
#         {"_id": record["_id"], "$expr": {"$eq": [{"$size": {"$ifNull": ["$history", []]}}, current_history_len]}},
#         update_doc,
#         return_document=True,
#     )
#     if result is None:
#         raise HTTPException(status_code=409, detail="Concurrent modification detected; please retry")

#     return serialize_record(result)


# # ---------------------------------------------------------------------------
# # Endpoints: analytics
# # ---------------------------------------------------------------------------
# def _month_range(month: str) -> tuple[str, str]:
#     """Return (first_day, last_day) as YYYY-MM-DD strings for a YYYY-MM month."""
#     year, mon = map(int, month.split("-"))
#     first = date(year, mon, 1)
#     if mon == 12:
#         last = date(year + 1, 1, 1) - timedelta(days=1)
#     else:
#         last = date(year, mon + 1, 1) - timedelta(days=1)
#     return first.isoformat(), last.isoformat()


# def _validate_month(month: str):
#     if not re.match(r'^\d{4}-(0[1-9]|1[0-2])$', month):
#         raise HTTPException(status_code=422, detail="month must be YYYY-MM")


# def _count_working_days(start_date: date, end_date: date) -> int:
#     """Count Mon-Fri days between start_date and end_date inclusive."""
#     count = 0
#     d = start_date
#     while d <= end_date:
#         if d.weekday() < 5:  # Mon=0 … Fri=4
#             count += 1
#         d += timedelta(days=1)
#     return count


# @app.get("/analytics/employees/{emp_code}/monthly")
# def employee_monthly(emp_code: str, month: str = Query(..., pattern=r'^\d{4}-(0[1-9]|1[0-2])$')):
#     emp = db.employees.find_one({"emp_code": emp_code})
#     if not emp:
#         raise HTTPException(status_code=404, detail="Employee not found")

#     first_day_str, last_day_str = _month_range(month)
#     first_day = date.fromisoformat(first_day_str)
#     last_day = date.fromisoformat(last_day_str)

#     # R7: working days from max(first_day, joined_on) to last_day
#     joined = date.fromisoformat(emp["joined_on"])
#     effective_start = max(first_day, joined)
#     if effective_start > last_day:
#         working_days = 0
#     else:
#         working_days = _count_working_days(effective_start, last_day)

#     # Aggregation pipeline for attendance logs
#     pipeline = [
#         {"$match": {
#             "emp_code": emp_code,
#             "date": {"$gte": first_day_str, "$lte": last_day_str},
#         }},
#         {"$addFields": {
#             "date_obj": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}},
#             "is_working_day": {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}}}, [2, 3, 4, 5, 6]]},
#             "is_present": {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
#             "is_leave": {"$eq": ["$status", "LEAVE"]},
#             "is_half_day": {"$ifNull": ["$half_day", False]},
#         }},
#         {"$addFields": {
#             # present_days: Mon-Fri only; half_day = 0.5
#             "present_contribution": {
#                 "$cond": {
#                     "if": {"$and": ["$is_present", "$is_working_day"]},
#                     "then": {"$cond": {"if": "$is_half_day", "then": 0.5, "else": 1.0}},
#                     "else": 0.0,
#                 }
#             }
#         }},
#         {"$group": {
#             "_id": None,
#             "present_days": {"$sum": "$present_contribution"},
#             "leave_days": {"$sum": {"$cond": ["$is_leave", 1, 0]}},
#             "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
#             "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
#             "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
#         }},
#     ]

#     results = list(db.attendance_logs.aggregate(pipeline))
#     if results:
#         r = results[0]
#         present_days = half_up(r["present_days"], 2)
#         leave_days = int(r["leave_days"])
#         late_count = int(r["late_count"])
#         total_late = int(r["total_late_minutes"])
#         total_ot = int(r["total_overtime_minutes"])
#     else:
#         present_days = 0.0
#         leave_days = 0
#         late_count = 0
#         total_late = 0
#         total_ot = 0

#     if working_days == 0:
#         attendance_pct = None
#     else:
#         attendance_pct = half_up(present_days / working_days * 100, 2)

#     return {
#         "emp_code": emp_code,
#         "month": month,
#         "working_days": working_days,
#         "present_days": present_days,
#         "leave_days": leave_days,
#         "late_count": late_count,
#         "total_late_minutes": total_late,
#         "total_overtime_minutes": total_ot,
#         "attendance_pct": attendance_pct,
#     }


# @app.get("/analytics/departments/summary")
# def department_summary(
#     month: str = Query(..., pattern=r'^\d{4}-(0[1-9]|1[0-2])$'),
#     department: Optional[str] = None,
# ):
#     first_day_str, last_day_str = _month_range(month)

#     # Step 1: get employees headcount per department (R9: joined_on <= last_day_str)
#     emp_match: dict = {"joined_on": {"$lte": last_day_str}}
#     if department:
#         emp_match["department"] = department

#     # Aggregation: join employees with their logs
#     pipeline = [
#         # Start from employees collection (headcount anchored here)
#         {"$match": emp_match},
#         {"$group": {
#             "_id": "$department",
#             "headcount": {"$sum": 1},
#             "emp_codes": {"$push": "$emp_code"},
#         }},
#         # Lookup attendance logs for these employees in the month
#         {"$lookup": {
#             "from": "attendance_logs",
#             "let": {"emps": "$emp_codes"},
#             "pipeline": [
#                 {"$match": {"$expr": {
#                     "$and": [
#                         {"$in": ["$emp_code", "$$emps"]},
#                         {"$gte": ["$date", first_day_str]},
#                         {"$lte": ["$date", last_day_str]},
#                     ]
#                 }}},
#                 {"$addFields": {
#                     "is_working_day": {"$in": [
#                         {"$dayOfWeek": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}}},
#                         [2, 3, 4, 5, 6]
#                     ]},
#                     "is_present": {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
#                     "is_on_duty": {"$eq": ["$status", "ON_DUTY"]},
#                     "is_leave": {"$eq": ["$status", "LEAVE"]},
#                     "is_half_day": {"$ifNull": ["$half_day", False]},
#                 }},
#                 {"$addFields": {
#                     "present_contribution": {
#                         "$cond": {
#                             "if": {"$and": ["$is_present", "$is_working_day"]},
#                             "then": {"$cond": {"if": "$is_half_day", "then": 0.5, "else": 1.0}},
#                             "else": 0.0,
#                         }
#                     }
#                 }},
#             ],
#             "as": "logs",
#         }},
#         {"$addFields": {
#             "present_days": {"$sum": "$logs.present_contribution"},
#             # Filter logs to presence-status records with non-null work_hours, then extract values
#             "wh_logs": {
#                 "$filter": {
#                     "input": "$logs",
#                     "as": "l",
#                     "cond": {
#                         "$and": [
#                             {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
#                             {"$ne": ["$$l.work_hours", None]},
#                         ]
#                     }
#                 }
#             },
#             "late_count": {"$sum": {"$map": {
#                 "input": "$logs",
#                 "as": "l",
#                 "in": {"$cond": [{"$gt": [{"$ifNull": ["$$l.late_minutes", 0]}, 0]}, 1, 0]}
#             }}},
#             "total_late_minutes": {"$sum": {"$map": {
#                 "input": "$logs",
#                 "as": "l",
#                 "in": {"$ifNull": ["$$l.late_minutes", 0]}
#             }}},
#             "leave_count": {"$sum": {"$map": {
#                 "input": "$logs",
#                 "as": "l",
#                 "in": {"$cond": [{"$eq": ["$$l.status", "LEAVE"]}, 1, 0]}
#             }}},
#             "on_duty_count": {"$sum": {"$map": {
#                 "input": "$logs",
#                 "as": "l",
#                 "in": {"$cond": [{"$eq": ["$$l.status", "ON_DUTY"]}, 1, 0]}
#             }}},
#         }},
#         {"$addFields": {
#             # $avg of the extracted work_hours; null when wh_logs is empty
#             "avg_work_hours_raw": {"$avg": "$wh_logs.work_hours"},
#             "wh_count": {"$size": "$wh_logs"},
#         }},
#         {"$sort": {"_id": ASCENDING}},
#         {"$project": {
#             "department": "$_id",
#             "headcount": 1,
#             "present_days": 1,
#             "avg_work_hours_raw": 1,
#             "wh_count": 1,
#             "late_count": 1,
#             "total_late_minutes": 1,
#             "leave_count": 1,
#             "on_duty_count": 1,
#             "_id": 0,
#         }},
#     ]

#     raw_items = list(db.employees.aggregate(pipeline))

#     # Apply Python-side half-up rounding to avg_work_hours (MongoDB $avg uses float arithmetic)
#     items = []
#     for r in raw_items:
#         wh_count = r.get("wh_count", 0)
#         avg_wh_raw = r.get("avg_work_hours_raw")
#         if wh_count and wh_count > 0 and avg_wh_raw is not None:
#             avg_wh = half_up(float(avg_wh_raw), 2)
#         else:
#             avg_wh = None

#         items.append({
#             "department": r["department"],
#             "headcount": int(r["headcount"]),
#             "present_days": half_up(r.get("present_days", 0), 2),
#             "avg_work_hours": avg_wh,
#             "late_count": int(r.get("late_count", 0)),
#             "total_late_minutes": int(r.get("total_late_minutes", 0)),
#             "leave_count": int(r.get("leave_count", 0)),
#             "on_duty_count": int(r.get("on_duty_count", 0)),
#         })

#     return {"month": month, "items": items}


# @app.get("/analytics/leaderboard/late")
# def late_leaderboard(
#     month: str = Query(..., pattern=r'^\d{4}-(0[1-9]|1[0-2])$'),
#     limit: int = Query(default=10, ge=1, le=50),
#     department: Optional[str] = None,
# ):
#     first_day_str, last_day_str = _month_range(month)

#     # Build employee filter
#     emp_match: dict = {}
#     if department:
#         emp_match["department"] = department

#     pipeline = [
#         # Aggregate late minutes per employee
#         {"$match": {
#             "date": {"$gte": first_day_str, "$lte": last_day_str},
#             "late_minutes": {"$gt": 0},
#         }},
#         {"$group": {
#             "_id": "$emp_code",
#             "total_late_minutes": {"$sum": "$late_minutes"},
#             "late_count": {"$sum": 1},
#         }},
#         # Join with employees
#         {"$lookup": {
#             "from": "employees",
#             "localField": "_id",
#             "foreignField": "emp_code",
#             "as": "emp",
#         }},
#         {"$unwind": "$emp"},
#         # Filter by department if provided
#         *(
#             [{"$match": {"emp.department": department}}]
#             if department else []
#         ),
#         {"$sort": {"total_late_minutes": DESCENDING, "_id": ASCENDING}},
#         {"$project": {
#             "emp_code": "$_id",
#             "name": "$emp.name",
#             "department": "$emp.department",
#             "total_late_minutes": 1,
#             "late_count": 1,
#             "_id": 0,
#         }},
#     ]

#     rows = list(db.attendance_logs.aggregate(pipeline))

#     # Assign standard competition ranks
#     ranked = []
#     current_rank = 1
#     i = 0
#     while i < len(rows):
#         rank = i + 1
#         # Find all tied at same total_late_minutes
#         j = i
#         while j < len(rows) and rows[j]["total_late_minutes"] == rows[i]["total_late_minutes"]:
#             j += 1
#         for k in range(i, j):
#             ranked.append({**rows[k], "rank": rank})
#         i = j

#     # Apply limit: include all rows where rank <= limit
#     result_rows = [r for r in ranked if r["rank"] <= limit]

#     return {"month": month, "items": result_rows}


# @app.get("/analytics/departments/{department}/trend")
# def department_trend(
#     department: str,
#     from_date: str = Query(..., alias="from"),
#     to_date: str = Query(..., alias="to"),
# ):
#     # Validate dates
#     try:
#         from_d = date.fromisoformat(from_date)
#         to_d = date.fromisoformat(to_date)
#     except ValueError:
#         raise HTTPException(status_code=422, detail="from/to must be YYYY-MM-DD")

#     if to_d < from_d:
#         raise HTTPException(status_code=422, detail="to must not be before from")

#     if (to_d - from_d).days > 91:
#         raise HTTPException(status_code=422, detail="date range exceeds 92 days")

#     # Check department exists
#     dept_exists = db.employees.find_one({"department": department})
#     if not dept_exists:
#         raise HTTPException(status_code=404, detail="Department not found")

#     from_str = from_date
#     to_str = to_date

#     # Use MongoDB $densify to fill gaps
#     pipeline = [
#         # Get all attendance logs for this department's employees in the range
#         {"$match": {
#             "date": {"$gte": from_str, "$lte": to_str},
#         }},
#         # Join with employees to filter by department
#         {"$lookup": {
#             "from": "employees",
#             "localField": "emp_code",
#             "foreignField": "emp_code",
#             "as": "emp",
#         }},
#         {"$unwind": "$emp"},
#         {"$match": {"emp.department": department}},
#         {"$addFields": {
#             "date_obj": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}},
#             "is_present": {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
#             "is_half_day": {"$ifNull": ["$half_day", False]},
#         }},
#         {"$addFields": {
#             "present_contribution": {
#                 "$cond": {
#                     "if": "$is_present",
#                     "then": {"$cond": {"if": "$is_half_day", "then": 0.5, "else": 1.0}},
#                     "else": 0.0,
#                 }
#             }
#         }},
#         {"$group": {
#             "_id": "$date_obj",
#             "date_str": {"$first": "$date"},
#             "present_count": {"$sum": "$present_contribution"},
#             "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
#         }},
#         # Densify to fill missing dates
#         {"$densify": {
#             "field": "_id",
#             "range": {
#                 "step": 1,
#                 "unit": "day",
#                 "bounds": [
#                     datetime.combine(from_d, datetime.min.time(), tzinfo=timezone.utc),
#                     datetime.combine(to_d + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc),
#                 ],
#             },
#         }},
#         {"$addFields": {
#             "date_str": {
#                 "$cond": {
#                     "if": {"$ifNull": ["$date_str", False]},
#                     "then": "$date_str",
#                     "else": {"$dateToString": {"format": "%Y-%m-%d", "date": "$_id"}},
#                 }
#             },
#             "present_count": {"$ifNull": ["$present_count", 0]},
#             "late_count": {"$ifNull": ["$late_count", 0]},
#         }},
#         {"$sort": {"_id": ASCENDING}},
#     ]

#     raw = list(db.attendance_logs.aggregate(pipeline))

#     # Build headcount per day: employees with joined_on <= that day
#     all_dept_emps = list(db.employees.find({"department": department}, {"emp_code": 1, "joined_on": 1, "_id": 0}))

#     # Compute attendance_rate and moving_avg_7d in Python
#     items = []
#     non_null_rates = []  # rolling window for 7d moving average

#     for row in raw:
#         d_str = row["date_str"]
#         d_obj = date.fromisoformat(d_str)
#         is_working_day = d_obj.weekday() < 5

#         # Headcount: employees with joined_on <= d_str
#         headcount = sum(1 for e in all_dept_emps if e["joined_on"] <= d_str)

#         present_count = row.get("present_count", 0)
#         late_count = int(row.get("late_count", 0))

#         # attendance_rate: only on working days with headcount > 0
#         if is_working_day and headcount > 0:
#             attendance_rate = half_up(present_count / headcount, 4)
#         else:
#             attendance_rate = None

#         # 7-day moving average: only non-null rates in the current window
#         non_null_rates.append(attendance_rate)
#         window = non_null_rates[-7:]
#         valid_window = [r for r in window if r is not None]
#         if valid_window:
#             moving_avg_7d = half_up(sum(valid_window) / len(valid_window), 4)
#         else:
#             moving_avg_7d = None

#         items.append({
#             "date": d_str,
#             "is_working_day": is_working_day,
#             "headcount": headcount,
#             "present_count": half_up(present_count, 2),
#             "late_count": late_count,
#             "attendance_rate": attendance_rate,
#             "moving_avg_7d": moving_avg_7d,
#         })

#     return {"department": department, "items": items}


# # ---------------------------------------------------------------------------
# # Endpoints: admin/explain
# # ---------------------------------------------------------------------------
# @app.get("/admin/explain/{endpoint}")
# def explain_endpoint(
#     endpoint: str,
#     emp_code: Optional[str] = None,
#     month: Optional[str] = None,
#     department: Optional[str] = None,
#     limit: int = Query(default=10, ge=1, le=50),
#     date_from: Optional[str] = None,
#     date_to: Optional[str] = None,
#     status: Optional[str] = None,
#     from_date: Optional[str] = Query(default=None, alias="from"),
#     to_date: Optional[str] = Query(default=None, alias="to"),
#     page: int = Query(default=1, ge=1),
#     page_size: int = Query(default=20, ge=1, le=100),
# ):
#     valid_endpoints = ["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"]
#     if endpoint not in valid_endpoints:
#         raise HTTPException(status_code=422, detail=f"endpoint must be one of {valid_endpoints}")

#     if endpoint == "attendance_list":
#         if date_from and date_to and date_from > date_to:
#             raise HTTPException(status_code=422, detail="date_from must not be after date_to")
#         q: dict = {}
#         if emp_code:
#             q["emp_code"] = emp_code
#         if date_from or date_to:
#             q["date"] = {}
#             if date_from:
#                 q["date"]["$gte"] = date_from
#             if date_to:
#                 q["date"]["$lte"] = date_to
#         if status:
#             q["status"] = status
#         skip = (page - 1) * page_size
#         explain_result = db.attendance_logs.find(q).sort(
#             [("date", DESCENDING), ("emp_code", ASCENDING)]
#         ).skip(skip).limit(page_size).explain()
#         return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain_result}

#     elif endpoint == "employee_monthly":
#         if not emp_code:
#             raise HTTPException(status_code=422, detail="emp_code is required for employee_monthly")
#         if not month:
#             raise HTTPException(status_code=422, detail="month is required for employee_monthly")
#         first_day_str, last_day_str = _month_range(month)
#         pipeline = [
#             {"$match": {
#                 "emp_code": emp_code,
#                 "date": {"$gte": first_day_str, "$lte": last_day_str},
#             }},
#         ]
#         explain_result = db.attendance_logs.aggregate(pipeline, explain=True)
#         return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain_result}

#     elif endpoint == "department_summary":
#         if not month:
#             raise HTTPException(status_code=422, detail="month is required for department_summary")
#         _, last_day_str = _month_range(month)
#         emp_match: dict = {"joined_on": {"$lte": last_day_str}}
#         if department:
#             emp_match["department"] = department
#         explain_result = db.employees.find(emp_match).explain()
#         return {"endpoint": endpoint, "collection": "employees", "explain": explain_result}

#     elif endpoint == "late_leaderboard":
#         if not month:
#             raise HTTPException(status_code=422, detail="month is required for late_leaderboard")
#         first_day_str, last_day_str = _month_range(month)
#         match_q: dict = {
#             "date": {"$gte": first_day_str, "$lte": last_day_str},
#             "late_minutes": {"$gt": 0},
#         }
#         pipeline = [{"$match": match_q}]
#         explain_result = db.attendance_logs.aggregate(pipeline, explain=True)
#         return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain_result}

#     elif endpoint == "department_trend":
#         if not department:
#             raise HTTPException(status_code=422, detail="department is required for department_trend")
#         if not from_date:
#             raise HTTPException(status_code=422, detail="from is required for department_trend")
#         if not to_date:
#             raise HTTPException(status_code=422, detail="to is required for department_trend")
#         match_q = {
#             "date": {"$gte": from_date, "$lte": to_date},
#         }
#         pipeline = [{"$match": match_q}]
#         explain_result = db.attendance_logs.aggregate(pipeline, explain=True)
#         return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain_result}










# # Endpoints: admin/explain
# # ---------------------------------------------------------------------------
# def _explain_aggregate(collection, pipeline):
#     """Return MongoDB execution statistics for an aggregation pipeline.

#     PyMongo does not support aggregate(..., explain=True). Use the MongoDB
#     explain command instead so the response includes queryPlanner and
#     executionStats.
#     """
#     return db.command(
#         "explain",
#         {
#             "aggregate": collection.name,
#             "pipeline": pipeline,
#             "cursor": {},
#         },
#         verbosity="executionStats",
#     )


# @app.get("/admin/explain/{endpoint}")
# def explain_endpoint(
#     endpoint: str,
#     emp_code: Optional[str] = None,
#     month: Optional[str] = None,
#     department: Optional[str] = None,
#     limit: int = Query(default=10, ge=1, le=50),
#     date_from: Optional[str] = None,
#     date_to: Optional[str] = None,
#     status: Optional[str] = None,
#     from_date: Optional[str] = Query(default=None, alias="from"),
#     to_date: Optional[str] = Query(default=None, alias="to"),
#     page: int = Query(default=1, ge=1),
#     page_size: int = Query(default=20, ge=1, le=100),
# ):
#     valid_endpoints = ["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"]
#     if endpoint not in valid_endpoints:
#         raise HTTPException(status_code=422, detail=f"endpoint must be one of {valid_endpoints}")

#     if endpoint == "attendance_list":
#         if date_from and date_to and date_from > date_to:
#             raise HTTPException(status_code=422, detail="date_from must not be after date_to")
#         q: dict = {}
#         if emp_code:
#             q["emp_code"] = emp_code
#         if date_from or date_to:
#             q["date"] = {}
#             if date_from:
#                 q["date"]["$gte"] = date_from
#             if date_to:
#                 q["date"]["$lte"] = date_to
#         if status:
#             q["status"] = status
#         skip = (page - 1) * page_size
#         explain_result = db.attendance_logs.find(q).sort(
#             [("date", DESCENDING), ("emp_code", ASCENDING)]
#         ).skip(skip).limit(page_size).explain()
#         return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain_result}

#     elif endpoint == "employee_monthly":
#         if not emp_code:
#             raise HTTPException(status_code=422, detail="emp_code is required for employee_monthly")
#         if not month:
#             raise HTTPException(status_code=422, detail="month is required for employee_monthly")
#         first_day_str, last_day_str = _month_range(month)
#         pipeline = [
#             {"$match": {
#                 "emp_code": emp_code,
#                 "date": {"$gte": first_day_str, "$lte": last_day_str},
#             }},
#         ]
#         explain_result = _explain_aggregate(db.attendance_logs, pipeline)
#         return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain_result}

#     elif endpoint == "department_summary":
#         if not month:
#             raise HTTPException(status_code=422, detail="month is required for department_summary")
#         _, last_day_str = _month_range(month)
#         emp_match: dict = {"joined_on": {"$lte": last_day_str}}
#         if department:
#             emp_match["department"] = department
#         explain_result = db.employees.find(emp_match).explain()
#         return {"endpoint": endpoint, "collection": "employees", "explain": explain_result}

#     elif endpoint == "late_leaderboard":
#         if not month:
#             raise HTTPException(status_code=422, detail="month is required for late_leaderboard")
#         first_day_str, last_day_str = _month_range(month)
#         match_q: dict = {
#             "date": {"$gte": first_day_str, "$lte": last_day_str},
#             "late_minutes": {"$gt": 0},
#         }
#         pipeline = [{"$match": match_q}]
#         explain_result = _explain_aggregate(db.attendance_logs, pipeline)
#         return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain_result}

#     elif endpoint == "department_trend":
#         if not department:
#             raise HTTPException(status_code=422, detail="department is required for department_trend")
#         if not from_date:
#             raise HTTPException(status_code=422, detail="from is required for department_trend")
#         if not to_date:
#             raise HTTPException(status_code=422, detail="to is required for department_trend")
#         match_q = {
#             "date": {"$gte": from_date, "$lte": to_date},
#         }
#         pipeline = [{"$match": match_q}]
#         explain_result = _explain_aggregate(db.attendance_logs, pipeline)
#         return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain_result}






# Endpoints: admin/explain
# ---------------------------------------------------------------------------

# def _explain_aggregate(collection, pipeline):
#     """Return MongoDB execution statistics for an aggregation pipeline."""
#     return db.command(
#         "explain",
#         {
#             "aggregate": collection.name,
#             "pipeline": pipeline,
#             "cursor": {},
#         },
#         verbosity="executionStats",
#     )


# @app.get("/admin/explain/{endpoint}")
# def explain_endpoint(
#     endpoint: str,
#     emp_code: Optional[str] = None,
#     month: Optional[str] = None,
#     department: Optional[str] = None,
#     limit: int = Query(default=10, ge=1, le=50),
#     date_from: Optional[str] = None,
#     date_to: Optional[str] = None,
#     status: Optional[str] = None,
#     from_date: Optional[str] = Query(default=None, alias="from"),
#     to_date: Optional[str] = Query(default=None, alias="to"),
#     page: int = Query(default=1, ge=1),
#     page_size: int = Query(default=20, ge=1, le=100),
# ):
#     valid_endpoints = [
#         "attendance_list",
#         "employee_monthly",
#         "department_summary",
#         "late_leaderboard",
#         "department_trend",
#     ]

#     if endpoint not in valid_endpoints:
#         raise HTTPException(
#             status_code=422,
#             detail=f"endpoint must be one of {valid_endpoints}",
#         )

#     # 1. Attendance list
#     if endpoint == "attendance_list":
#         if date_from and date_to and date_from > date_to:
#             raise HTTPException(
#                 status_code=422,
#                 detail="date_from must not be after date_to",
#             )

#         query = {}

#         if emp_code:
#             query["emp_code"] = emp_code

#         if date_from or date_to:
#             query["date"] = {}

#             if date_from:
#                 query["date"]["$gte"] = date_from

#             if date_to:
#                 query["date"]["$lte"] = date_to

#         if status:
#             query["status"] = status

#         skip = (page - 1) * page_size

#         explain_result = (
#             db.attendance_logs
#             .find(query)
#             .sort([
#                 ("date", DESCENDING),
#                 ("emp_code", ASCENDING),
#             ])
#             .skip(skip)
#             .limit(page_size)
#             .explain()
#         )

#         return {
#             "endpoint": endpoint,
#             "collection": "attendance_logs",
#             "explain": explain_result,
#         }

#     # 2. Employee monthly attendance
#     elif endpoint == "employee_monthly":
#         if not emp_code:
#             raise HTTPException(
#                 status_code=422,
#                 detail="emp_code is required for employee_monthly",
#             )

#         if not month:
#             raise HTTPException(
#                 status_code=422,
#                 detail="month is required for employee_monthly",
#             )

#         first_day_str, last_day_str = _month_range(month)

#         pipeline = [
#             {
#                 "$match": {
#                     "emp_code": emp_code,
#                     "date": {
#                         "$gte": first_day_str,
#                         "$lte": last_day_str,
#                     },
#                 }
#             }
#         ]

#         explain_result = _explain_aggregate(
#             db.attendance_logs,
#             pipeline,
#         )

#         return {
#             "endpoint": endpoint,
#             "collection": "attendance_logs",
#             "explain": explain_result,
#         }

#     # 3. Department summary
#     elif endpoint == "department_summary":
#         if not month:
#             raise HTTPException(
#                 status_code=422,
#                 detail="month is required for department_summary",
#             )

#         _, last_day_str = _month_range(month)

#         employee_match = {
#             "joined_on": {"$lte": last_day_str}
#         }

#         if department:
#             employee_match["department"] = department

#         explain_result = (
#             db.employees
#             .find(employee_match)
#             .explain()
#         )

#         return {
#             "endpoint": endpoint,
#             "collection": "employees",
#             "explain": explain_result,
#         }

#     # 4. Late leaderboard
#     elif endpoint == "late_leaderboard":
#         if not month:
#             raise HTTPException(
#                 status_code=422,
#                 detail="month is required for late_leaderboard",
#             )

#         first_day_str, last_day_str = _month_range(month)

#         match_query = {
#             "date": {
#                 "$gte": first_day_str,
#                 "$lte": last_day_str,
#             },
#             "late_minutes": {"$gt": 0},
#         }

#         pipeline = [
#             {"$match": match_query}
#         ]

#         explain_result = _explain_aggregate(
#             db.attendance_logs,
#             pipeline,
#         )

#         return {
#             "endpoint": endpoint,
#             "collection": "attendance_logs",
#             "explain": explain_result,
#         }

#     # 5. Department trend
#     elif endpoint == "department_trend":
#         if not department:
#             raise HTTPException(
#                 status_code=422,
#                 detail="department is required for department_trend",
#             )

#         if not from_date:
#             raise HTTPException(
#                 status_code=422,
#                 detail="from is required for department_trend",
#             )

#         if not to_date:
#             raise HTTPException(
#                 status_code=422,
#                 detail="to is required for department_trend",
#             )

#         if from_date > to_date:
#             raise HTTPException(
#                 status_code=422,
#                 detail="'from' must not be after 'to'",
#             )

#         match_query = {
#             "date": {
#                 "$gte": from_date,
#                 "$lte": to_date,
#             }
#         }

#         pipeline = [
#             {"$match": match_query}
#         ]

#         explain_result = _explain_aggregate(
#             db.attendance_logs,
#             pipeline,
#         )

#         return {
#             "endpoint": endpoint,
#             "collection": "attendance_logs",
#             "explain": explain_result,
#         }





























































"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is fine)
"""
import os
import re
from datetime import datetime, timedelta, timezone, date
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, field_validator, model_validator
from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError

load_dotenv()

# ---------------------------------------------------------------------------
# DB setup
# ---------------------------------------------------------------------------
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client = MongoClient(MONGO_URI)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")

# ---------------------------------------------------------------------------
# Startup: create indexes idempotently
# ---------------------------------------------------------------------------
@app.on_event("startup")
def create_indexes():
    # employees: unique on emp_code (enforces concurrent-safe duplicate creates)
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="emp_code_unique")

    # attendance_logs: unique compound (emp_code, date) for concurrency-safe punch-in
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)],
        unique=True,
        name="emp_date_unique",
    )
    # Support list queries: sort by date desc + emp_code asc, with all filter fields
    db.attendance_logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)],
        name="date_desc_emp_asc",
    )
    # Fast lookup by emp_code alone (used by analytics)
    db.attendance_logs.create_index([("emp_code", ASCENDING)], name="emp_code_idx")


# ---------------------------------------------------------------------------
# Constants & helpers
# ---------------------------------------------------------------------------
PRESENCE_STATUSES = {"PRESENT", "WFH", "ON_DUTY"}
ALL_STATUSES = {"PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"}

EPOCH_MIN = 100_000_000_000   # 1973-03-03 – values below this look like seconds
EPOCH_MAX = 4_102_444_800_000  # 2100-01-01


def half_up(value: float, decimals: int) -> float:
    """Round half-up to `decimals` places (not banker's rounding)."""
    q = Decimal(10) ** -decimals
    return float(Decimal(str(value)).quantize(q, rounding=ROUND_HALF_UP))


def ms_to_dt(ms: int) -> datetime:
    """Epoch milliseconds → aware UTC datetime truncated to seconds."""
    return datetime.fromtimestamp(ms // 1000, tz=timezone.utc).replace(microsecond=0)


def dt_to_ms(dt: datetime) -> int:
    """Aware or naive datetime → epoch milliseconds."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def dt_to_ist(dt: datetime) -> datetime:
    """Convert a UTC datetime (naive or aware) to an IST-aware datetime."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(IST)


def attendance_date_for(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    """
    R1: Return the IST calendar date string for the attendance record.
    For overnight shifts (shift_end <= shift_start), a punch-in whose IST
    time is earlier than shift_end belongs to the shift that started the
    previous day.
    """
    ist = dt_to_ist(punch_in)
    # Overnight shift check
    s_h, s_m = map(int, shift_start.split(":"))
    e_h, e_m = map(int, shift_end.split(":"))
    is_overnight = (e_h * 60 + e_m) <= (s_h * 60 + s_m)
    if is_overnight:
        punch_ist_minutes = ist.hour * 60 + ist.minute
        shift_end_minutes = e_h * 60 + e_m
        if punch_ist_minutes < shift_end_minutes:
            # This punch is before shift_end → belongs to previous calendar day
            return (ist.date() - timedelta(days=1)).isoformat()
    return ist.date().isoformat()


def compute_late_minutes(punch_in: datetime, shift_start: str, attendance_date: str) -> int:
    """
    R2: Grace period 10 minutes. Late only if strictly more than 10:00 after shift_start.
    `late_minutes` = floor of whole minutes since shift_start (not since end of grace).
    punch_in is a UTC datetime (naive); attendance_date is the IST attendance date string.
    """
    h, m = map(int, shift_start.split(":"))
    # Build shift_start as an IST-aware datetime on the attendance day
    base_date = date.fromisoformat(attendance_date)
    shift_start_ist = datetime(
        base_date.year, base_date.month, base_date.day, h, m, 0, tzinfo=IST
    )
    # Convert punch_in to IST-aware
    if punch_in.tzinfo is None:
        punch_in_aware = punch_in.replace(tzinfo=timezone.utc)
    else:
        punch_in_aware = punch_in
    delta_secs = (punch_in_aware - shift_start_ist).total_seconds()
    # Grace: strictly more than 10 minutes (600 seconds) late
    if delta_secs <= 600:
        return 0
    return int(delta_secs // 60)


def compute_overtime_minutes(punch_out: datetime, shift_end: str, attendance_date: str, shift_start: str) -> int:
    """
    R3: overtime = floor minutes between shift_end and punch_out, only if >= 30; else 0.
    For overnight shifts, shift_end is on the day after attendance_date.
    """
    s_h, s_m = map(int, shift_start.split(":"))
    e_h, e_m = map(int, shift_end.split(":"))
    is_overnight = (e_h * 60 + e_m) <= (s_h * 60 + s_m)
    base_date = date.fromisoformat(attendance_date)
    if is_overnight:
        end_date = base_date + timedelta(days=1)
    else:
        end_date = base_date
    shift_end_ist = datetime(
        end_date.year, end_date.month, end_date.day, e_h, e_m, 0, tzinfo=IST
    )
    if punch_out.tzinfo is None:
        punch_out_aware = punch_out.replace(tzinfo=timezone.utc)
    else:
        punch_out_aware = punch_out
    delta_secs = (punch_out_aware - shift_end_ist).total_seconds()
    minutes = int(delta_secs // 60)
    return minutes if minutes >= 30 else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: (punch_out - punch_in) / 3600, rounded half-up to 2 decimals."""
    if punch_in.tzinfo is None:
        punch_in = punch_in.replace(tzinfo=timezone.utc)
    if punch_out.tzinfo is None:
        punch_out = punch_out.replace(tzinfo=timezone.utc)
    secs = (punch_out - punch_in).total_seconds()
    return half_up(secs / 3600, 2)


def compute_derived(punch_in: Optional[datetime], punch_out: Optional[datetime],
                    shift_start: str, shift_end: str, attendance_date: str, status: str):
    """Compute all four derived fields given the current state of the record."""
    if status not in PRESENCE_STATUSES:
        return {"late_minutes": 0, "overtime_minutes": 0, "work_hours": None, "half_day": False}
    if punch_in is None:
        return {"late_minutes": 0, "overtime_minutes": 0, "work_hours": None, "half_day": False}

    late_minutes = compute_late_minutes(punch_in, shift_start, attendance_date)
    if punch_out is None:
        return {
            "late_minutes": late_minutes,
            "overtime_minutes": 0,
            "work_hours": None,
            "half_day": False,
        }

    work_hours = compute_work_hours(punch_in, punch_out)
    overtime_minutes = compute_overtime_minutes(punch_out, shift_end, attendance_date, shift_start)
    half_day = work_hours < 4.50  # R5: based on the rounded work_hours
    return {
        "late_minutes": late_minutes,
        "overtime_minutes": overtime_minutes,
        "work_hours": work_hours,
        "half_day": half_day,
    }


def serialize_record(doc: dict) -> dict:
    """Convert a MongoDB attendance document to the API response shape."""
    def _maybe_ms(v):
        if v is None:
            return None
        if isinstance(v, datetime):
            return dt_to_ms(v)
        return v

    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": _maybe_ms(doc.get("punch_in")),
        "punch_out": _maybe_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": [serialize_history_entry(h) for h in doc.get("history", [])],
    }


def serialize_history_entry(entry: dict) -> dict:
    def _convert_change_value(v):
        if isinstance(v, datetime):
            return dt_to_ms(v)
        return v

    changes = {}
    for field, chg in entry.get("changes", {}).items():
        changes[field] = {
            "from": _convert_change_value(chg.get("from")),
            "to": _convert_change_value(chg.get("to")),
        }

    at_val = entry.get("at")
    at_ms = dt_to_ms(at_val) if isinstance(at_val, datetime) else at_val

    return {
        "at": at_ms,
        "by": entry["by"],
        "reason": entry["reason"],
        "changes": changes,
    }


def serialize_employee(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": dt_to_ms(doc["created_at"]) if isinstance(doc.get("created_at"), datetime) else doc.get("created_at"),
    }


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------
class EmployeeCreate(BaseModel):
    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str

    @field_validator("emp_code")
    @classmethod
    def validate_emp_code(cls, v):
        if not re.match(r'^EMP\d{4,6}$', v):
            raise ValueError("emp_code must match EMP followed by 4-6 digits")
        return v

    @field_validator("name")
    @classmethod
    def validate_name(cls, v):
        if not (1 <= len(v) <= 100):
            raise ValueError("name must be 1-100 characters")
        return v

    @field_validator("email")
    @classmethod
    def validate_email(cls, v):
        if not re.match(r'^[^@\s]+@[^@\s]+\.[^@\s]+$', v):
            raise ValueError("invalid email format")
        if len(v) > 120:
            raise ValueError("email too long")
        return v

    @field_validator("department")
    @classmethod
    def validate_department(cls, v):
        if not (1 <= len(v) <= 50):
            raise ValueError("department must be 1-50 characters")
        return v

    @field_validator("shift_start", "shift_end")
    @classmethod
    def validate_shift_time(cls, v):
        if not re.match(r'^([01]\d|2[0-3]):[0-5]\d$', v):
            raise ValueError("shift time must be HH:MM (24-hour)")
        return v

    @field_validator("joined_on")
    @classmethod
    def validate_joined_on(cls, v):
        try:
            date.fromisoformat(v)
        except ValueError:
            raise ValueError("joined_on must be YYYY-MM-DD")
        return v

    @model_validator(mode="after")
    def shift_start_ne_end(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None
    status: str = "PRESENT"

    @field_validator("punched_at")
    @classmethod
    def validate_punched_at(cls, v):
        if v is not None:
            if not isinstance(v, int):
                raise ValueError("punched_at must be an integer epoch milliseconds")
            if v < EPOCH_MIN or v > EPOCH_MAX:
                raise ValueError("punched_at out of valid range (must be epoch milliseconds)")
        return v

    @field_validator("status")
    @classmethod
    def validate_status(cls, v):
        if v not in PRESENCE_STATUSES:
            raise ValueError(f"status must be one of {sorted(PRESENCE_STATUSES)}")
        return v


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None

    @field_validator("punched_at")
    @classmethod
    def validate_punched_at(cls, v):
        if v is not None:
            if not isinstance(v, int):
                raise ValueError("punched_at must be an integer epoch milliseconds")
            if v < EPOCH_MIN or v > EPOCH_MAX:
                raise ValueError("punched_at out of valid range (must be epoch milliseconds)")
        return v


class RegularizeRequest(BaseModel):
    status: Optional[str] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    reason: str
    regularized_by: str

    @field_validator("punch_in", "punch_out")
    @classmethod
    def validate_ts(cls, v):
        if v is not None:
            if not isinstance(v, int):
                raise ValueError("must be integer epoch milliseconds")
            if v < EPOCH_MIN or v > EPOCH_MAX:
                raise ValueError("out of valid epoch milliseconds range")
        return v

    @field_validator("status")
    @classmethod
    def validate_status(cls, v):
        if v is not None and v not in ALL_STATUSES:
            raise ValueError(f"status must be one of {sorted(ALL_STATUSES)}")
        return v

    @field_validator("reason")
    @classmethod
    def validate_reason(cls, v):
        if not (5 <= len(v) <= 200):
            raise ValueError("reason must be 5-200 characters")
        return v

    @field_validator("regularized_by")
    @classmethod
    def validate_regularized_by(cls, v):
        if not (1 <= len(v) <= 50):
            raise ValueError("regularized_by must be 1-50 characters")
        return v


# ---------------------------------------------------------------------------
# Endpoints: system
# ---------------------------------------------------------------------------
@app.get("/health")
def health():
    try:
        db.command("ping")
        return {"status": "ok"}
    except Exception:
        return JSONResponse(status_code=503, content={"detail": "MongoDB not available"})


# ---------------------------------------------------------------------------
# Endpoints: employees
# ---------------------------------------------------------------------------
@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate):
    doc = body.model_dump()
    doc["created_at"] = datetime.now(tz=timezone.utc)
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")
    return serialize_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    q = {}
    if department:
        q["department"] = department
    skip = (page - 1) * page_size
    total = db.employees.count_documents(q)
    items = list(db.employees.find(q, {"_id": 0}).sort("emp_code", ASCENDING).skip(skip).limit(page_size))
    return {
        "items": [serialize_employee(e) for e in items],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# ---------------------------------------------------------------------------
# Endpoints: attendance
# ---------------------------------------------------------------------------
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    # Determine punch-in time (truncate to seconds per R1)
    if body.punched_at is not None:
        ts = ms_to_dt(body.punched_at)
    else:
        ts = datetime.now(tz=timezone.utc).replace(microsecond=0)

    attendance_date = attendance_date_for(ts, emp["shift_start"], emp["shift_end"])
    late_minutes = compute_late_minutes(ts, emp["shift_start"], attendance_date)

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
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="Already punched in for this attendance date")

    return serialize_record(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is not None:
        ts = ms_to_dt(body.punched_at)
    else:
        ts = datetime.now(tz=timezone.utc).replace(microsecond=0)

    # Find the most recent punch-in record at or before punched_at (for overnight support)
    record = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "punch_in": {"$lte": ts},
            "punch_out": None,
            "status": {"$in": list(PRESENCE_STATUSES)},
        },
        sort=[("punch_in", DESCENDING)],
    )
    if not record:
        raise HTTPException(status_code=404, detail="No open punch-in record found for this employee")

    punch_in_dt = record["punch_in"]
    if punch_in_dt.tzinfo is None:
        punch_in_dt = punch_in_dt.replace(tzinfo=timezone.utc)

    # Validate: punch_out > punch_in and within 24 hours
    if ts <= punch_in_dt:
        raise HTTPException(status_code=422, detail="punched_at must be after punch_in")
    if (ts - punch_in_dt).total_seconds() > 86400:
        raise HTTPException(status_code=422, detail="punched_at is more than 24 hours after punch_in")

    derived = compute_derived(
        punch_in_dt, ts,
        emp["shift_start"], emp["shift_end"],
        record["date"], record["status"]
    )

    # Atomic update: only if punch_out is still None (concurrency safety)
    result = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": ts,
                "work_hours": derived["work_hours"],
                "overtime_minutes": derived["overtime_minutes"],
                "half_day": derived["half_day"],
                "late_minutes": derived["late_minutes"],
            }
        },
        return_document=True,
    )
    if result is None:
        raise HTTPException(status_code=409, detail="Record already punched out (concurrent request)")

    return serialize_record(result)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    # Validate date_from <= date_to
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from must not be after date_to")
    if status and status not in ALL_STATUSES:
        raise HTTPException(status_code=422, detail=f"Invalid status: {status}")

    q: dict = {}
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

    skip = (page - 1) * page_size
    total = db.attendance_logs.count_documents(q)
    docs = list(
        db.attendance_logs.find(q, {"_id": 0})
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip(skip)
        .limit(page_size)
    )
    return {
        "items": [serialize_record(d) for d in docs],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeRequest):
    # Validate date format
    try:
        date_obj = datetime.strptime(date, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=422, detail="date must be YYYY-MM-DD")

    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(status_code=404, detail="Attendance record not found for that date")

    # Business-rule validations
    new_status = body.status if body.status is not None else record["status"]
    new_pi_ms = body.punch_in
    new_po_ms = body.punch_out

    # Setting ABSENT or LEAVE: must not provide punch times
    if new_status in ("ABSENT", "LEAVE"):
        if new_pi_ms is not None or new_po_ms is not None:
            raise HTTPException(
                status_code=422,
                detail="Cannot supply punch_in or punch_out when setting status to ABSENT or LEAVE",
            )

    # Convert stored punch times to aware UTC datetimes
    stored_pi = record.get("punch_in")
    if stored_pi and stored_pi.tzinfo is None:
        stored_pi = stored_pi.replace(tzinfo=timezone.utc)
    stored_po = record.get("punch_out")
    if stored_po and stored_po.tzinfo is None:
        stored_po = stored_po.replace(tzinfo=timezone.utc)

    # Determine final punch times
    if new_status in ("ABSENT", "LEAVE"):
        final_pi = None
        final_po = None
    else:
        final_pi = ms_to_dt(new_pi_ms) if new_pi_ms is not None else stored_pi
        final_po = ms_to_dt(new_po_ms) if new_po_ms is not None else stored_po

    # Presence status requires punch_in
    if new_status in PRESENCE_STATUSES and final_pi is None:
        raise HTTPException(status_code=422, detail="A presence status requires punch_in")

    # punch_in must be on the attendance date (R1)
    if final_pi is not None:
        pi_ist_date = dt_to_ist(final_pi).date().isoformat()
        # For overnight: also check the previous day
        pi_attendance_date = attendance_date_for(final_pi, emp["shift_start"], emp["shift_end"])
        if pi_attendance_date != date:
            raise HTTPException(
                status_code=422,
                detail="punch_in must belong to the attendance date (R1)",
            )

    # punch_out must be after punch_in and within 24 hours
    if final_po is not None:
        if final_pi is None:
            raise HTTPException(status_code=422, detail="punch_out requires a punch_in")
        if final_po <= final_pi:
            raise HTTPException(status_code=422, detail="punch_out must be after punch_in")
        if (final_po - final_pi).total_seconds() > 86400:
            raise HTTPException(status_code=422, detail="punch_out more than 24 hours after punch_in")

    # Compute derived fields
    derived = compute_derived(final_pi, final_po, emp["shift_start"], emp["shift_end"], date, new_status)

    # Build the set of changes (only fields that actually changed)
    field_mapping = {
        "status": (record.get("status"), new_status),
        "punch_in": (stored_pi, final_pi),
        "punch_out": (stored_po, final_po),
        "late_minutes": (record.get("late_minutes", 0), derived["late_minutes"]),
        "overtime_minutes": (record.get("overtime_minutes", 0), derived["overtime_minutes"]),
        "work_hours": (record.get("work_hours"), derived["work_hours"]),
        "half_day": (record.get("half_day", False), derived["half_day"]),
    }

    changes = {}
    for field, (old_val, new_val) in field_mapping.items():
        # Compare with type-aware equality for datetimes
        if field in ("punch_in", "punch_out"):
            old_ms = dt_to_ms(old_val) if old_val is not None else None
            new_ms = dt_to_ms(new_val) if new_val is not None else None
            if old_ms != new_ms:
                changes[field] = {"from": old_ms, "to": new_ms}
        else:
            if old_val != new_val:
                changes[field] = {"from": old_val, "to": new_val}

    if not changes:
        raise HTTPException(status_code=422, detail="Request would change nothing")

    history_entry = {
        "at": datetime.now(tz=timezone.utc),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": {
            k: {
                "from": datetime.fromtimestamp(v["from"] / 1000, tz=timezone.utc) if k in ("punch_in", "punch_out") and v["from"] is not None else v["from"],
                "to": datetime.fromtimestamp(v["to"] / 1000, tz=timezone.utc) if k in ("punch_in", "punch_out") and v["to"] is not None else v["to"],
            }
            for k, v in changes.items()
        },
    }

    update_doc = {
        "$set": {
            "status": new_status,
            "punch_in": final_pi,
            "punch_out": final_po,
            "work_hours": derived["work_hours"],
            "late_minutes": derived["late_minutes"],
            "overtime_minutes": derived["overtime_minutes"],
            "half_day": derived["half_day"],
        },
        "$push": {"history": history_entry},
    }

    # Optimistic concurrency: check current history length
    current_history_len = len(record.get("history", []))
    result = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "$expr": {"$eq": [{"$size": {"$ifNull": ["$history", []]}}, current_history_len]}},
        update_doc,
        return_document=True,
    )
    if result is None:
        raise HTTPException(status_code=409, detail="Concurrent modification detected; please retry")

    return serialize_record(result)


# ---------------------------------------------------------------------------
# Endpoints: analytics
# ---------------------------------------------------------------------------
def _month_range(month: str) -> tuple[str, str]:
    """Return (first_day, last_day) as YYYY-MM-DD strings for a YYYY-MM month."""
    year, mon = map(int, month.split("-"))
    first = date(year, mon, 1)
    if mon == 12:
        last = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        last = date(year, mon + 1, 1) - timedelta(days=1)
    return first.isoformat(), last.isoformat()


def _validate_month(month: str):
    if not re.match(r'^\d{4}-(0[1-9]|1[0-2])$', month):
        raise HTTPException(status_code=422, detail="month must be YYYY-MM")


def _count_working_days(start_date: date, end_date: date) -> int:
    """Count Mon-Fri days between start_date and end_date inclusive."""
    count = 0
    d = start_date
    while d <= end_date:
        if d.weekday() < 5:  # Mon=0 … Fri=4
            count += 1
        d += timedelta(days=1)
    return count


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=r'^\d{4}-(0[1-9]|1[0-2])$')):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    first_day_str, last_day_str = _month_range(month)
    first_day = date.fromisoformat(first_day_str)
    last_day = date.fromisoformat(last_day_str)

    # R7: working days from max(first_day, joined_on) to last_day
    joined = date.fromisoformat(emp["joined_on"])
    effective_start = max(first_day, joined)
    if effective_start > last_day:
        working_days = 0
    else:
        working_days = _count_working_days(effective_start, last_day)

    # Aggregation pipeline for attendance logs
    pipeline = [
        {"$match": {
            "emp_code": emp_code,
            "date": {"$gte": first_day_str, "$lte": last_day_str},
        }},
        {"$addFields": {
            "date_obj": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}},
            "is_working_day": {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}}}, [2, 3, 4, 5, 6]]},
            "is_present": {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
            "is_leave": {"$eq": ["$status", "LEAVE"]},
            "is_half_day": {"$ifNull": ["$half_day", False]},
        }},
        {"$addFields": {
            # present_days: Mon-Fri only; half_day = 0.5
            "present_contribution": {
                "$cond": {
                    "if": {"$and": ["$is_present", "$is_working_day"]},
                    "then": {"$cond": {"if": "$is_half_day", "then": 0.5, "else": 1.0}},
                    "else": 0.0,
                }
            }
        }},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": "$present_contribution"},
            "leave_days": {"$sum": {"$cond": ["$is_leave", 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ]

    results = list(db.attendance_logs.aggregate(pipeline))
    if results:
        r = results[0]
        present_days = half_up(r["present_days"], 2)
        leave_days = int(r["leave_days"])
        late_count = int(r["late_count"])
        total_late = int(r["total_late_minutes"])
        total_ot = int(r["total_overtime_minutes"])
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late = 0
        total_ot = 0

    if working_days == 0:
        attendance_pct = None
    else:
        attendance_pct = half_up(present_days / working_days * 100, 2)

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late,
        "total_overtime_minutes": total_ot,
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r'^\d{4}-(0[1-9]|1[0-2])$'),
    department: Optional[str] = None,
):
    first_day_str, last_day_str = _month_range(month)

    # Step 1: get employees headcount per department (R9: joined_on <= last_day_str)
    emp_match: dict = {"joined_on": {"$lte": last_day_str}}
    if department:
        emp_match["department"] = department

    # Aggregation: join employees with their logs
    pipeline = [
        # Start from employees collection (headcount anchored here)
        {"$match": emp_match},
        {"$group": {
            "_id": "$department",
            "headcount": {"$sum": 1},
            "emp_codes": {"$push": "$emp_code"},
        }},
        # Lookup attendance logs for these employees in the month
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"emps": "$emp_codes"},
            "pipeline": [
                {"$match": {"$expr": {
                    "$and": [
                        {"$in": ["$emp_code", "$$emps"]},
                        {"$gte": ["$date", first_day_str]},
                        {"$lte": ["$date", last_day_str]},
                    ]
                }}},
                {"$addFields": {
                    "is_working_day": {"$in": [
                        {"$dayOfWeek": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}}},
                        [2, 3, 4, 5, 6]
                    ]},
                    "is_present": {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                    "is_on_duty": {"$eq": ["$status", "ON_DUTY"]},
                    "is_leave": {"$eq": ["$status", "LEAVE"]},
                    "is_half_day": {"$ifNull": ["$half_day", False]},
                }},
                {"$addFields": {
                    "present_contribution": {
                        "$cond": {
                            "if": {"$and": ["$is_present", "$is_working_day"]},
                            "then": {"$cond": {"if": "$is_half_day", "then": 0.5, "else": 1.0}},
                            "else": 0.0,
                        }
                    }
                }},
            ],
            "as": "logs",
        }},
        {"$addFields": {
            "present_days": {"$sum": "$logs.present_contribution"},
            # Filter logs to presence-status records with non-null work_hours, then extract values
            "wh_logs": {
                "$filter": {
                    "input": "$logs",
                    "as": "l",
                    "cond": {
                        "$and": [
                            {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$ne": ["$$l.work_hours", None]},
                        ]
                    }
                }
            },
            "late_count": {"$sum": {"$map": {
                "input": "$logs",
                "as": "l",
                "in": {"$cond": [{"$gt": [{"$ifNull": ["$$l.late_minutes", 0]}, 0]}, 1, 0]}
            }}},
            "total_late_minutes": {"$sum": {"$map": {
                "input": "$logs",
                "as": "l",
                "in": {"$ifNull": ["$$l.late_minutes", 0]}
            }}},
            "leave_count": {"$sum": {"$map": {
                "input": "$logs",
                "as": "l",
                "in": {"$cond": [{"$eq": ["$$l.status", "LEAVE"]}, 1, 0]}
            }}},
            "on_duty_count": {"$sum": {"$map": {
                "input": "$logs",
                "as": "l",
                "in": {"$cond": [{"$eq": ["$$l.status", "ON_DUTY"]}, 1, 0]}
            }}},
        }},
        {"$addFields": {
            # $avg of the extracted work_hours; null when wh_logs is empty
            "avg_work_hours_raw": {"$avg": "$wh_logs.work_hours"},
            "wh_count": {"$size": "$wh_logs"},
        }},
        {"$sort": {"_id": ASCENDING}},
        {"$project": {
            "department": "$_id",
            "headcount": 1,
            "present_days": 1,
            "avg_work_hours_raw": 1,
            "wh_count": 1,
            "late_count": 1,
            "total_late_minutes": 1,
            "leave_count": 1,
            "on_duty_count": 1,
            "_id": 0,
        }},
    ]

    raw_items = list(db.employees.aggregate(pipeline))

    # Apply Python-side half-up rounding to avg_work_hours (MongoDB $avg uses float arithmetic)
    items = []
    for r in raw_items:
        wh_count = r.get("wh_count", 0)
        avg_wh_raw = r.get("avg_work_hours_raw")
        if wh_count and wh_count > 0 and avg_wh_raw is not None:
            avg_wh = half_up(float(avg_wh_raw), 2)
        else:
            avg_wh = None

        items.append({
            "department": r["department"],
            "headcount": int(r["headcount"]),
            "present_days": half_up(r.get("present_days", 0), 2),
            "avg_work_hours": avg_wh,
            "late_count": int(r.get("late_count", 0)),
            "total_late_minutes": int(r.get("total_late_minutes", 0)),
            "leave_count": int(r.get("leave_count", 0)),
            "on_duty_count": int(r.get("on_duty_count", 0)),
        })

    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r'^\d{4}-(0[1-9]|1[0-2])$'),
    limit: int = Query(default=10, ge=1, le=50),
    department: Optional[str] = None,
):
    first_day_str, last_day_str = _month_range(month)

    # Build employee filter
    emp_match: dict = {}
    if department:
        emp_match["department"] = department

    pipeline = [
        # Aggregate late minutes per employee
        {"$match": {
            "date": {"$gte": first_day_str, "$lte": last_day_str},
            "late_minutes": {"$gt": 0},
        }},
        {"$group": {
            "_id": "$emp_code",
            "total_late_minutes": {"$sum": "$late_minutes"},
            "late_count": {"$sum": 1},
        }},
        # Join with employees
        {"$lookup": {
            "from": "employees",
            "localField": "_id",
            "foreignField": "emp_code",
            "as": "emp",
        }},
        {"$unwind": "$emp"},
        # Filter by department if provided
        *(
            [{"$match": {"emp.department": department}}]
            if department else []
        ),
        {"$sort": {"total_late_minutes": DESCENDING, "_id": ASCENDING}},
        {"$project": {
            "emp_code": "$_id",
            "name": "$emp.name",
            "department": "$emp.department",
            "total_late_minutes": 1,
            "late_count": 1,
            "_id": 0,
        }},
    ]

    rows = list(db.attendance_logs.aggregate(pipeline))

    # Assign standard competition ranks
    ranked = []
    current_rank = 1
    i = 0
    while i < len(rows):
        rank = i + 1
        # Find all tied at same total_late_minutes
        j = i
        while j < len(rows) and rows[j]["total_late_minutes"] == rows[i]["total_late_minutes"]:
            j += 1
        for k in range(i, j):
            ranked.append({**rows[k], "rank": rank})
        i = j

    # Apply limit: include all rows where rank <= limit
    result_rows = [r for r in ranked if r["rank"] <= limit]

    return {"month": month, "items": result_rows}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: str = Query(..., alias="from"),
    to_date: str = Query(..., alias="to"),
):
    # Validate dates
    try:
        from_d = date.fromisoformat(from_date)
        to_d = date.fromisoformat(to_date)
    except ValueError:
        raise HTTPException(status_code=422, detail="from/to must be YYYY-MM-DD")

    if to_d < from_d:
        raise HTTPException(status_code=422, detail="to must not be before from")

    if (to_d - from_d).days > 91:
        raise HTTPException(status_code=422, detail="date range exceeds 92 days")

    # Check department exists
    dept_exists = db.employees.find_one({"department": department})
    if not dept_exists:
        raise HTTPException(status_code=404, detail="Department not found")

    from_str = from_date
    to_str = to_date

    # Use MongoDB $densify to fill gaps
    pipeline = [
        # Get all attendance logs for this department's employees in the range
        {"$match": {
            "date": {"$gte": from_str, "$lte": to_str},
        }},
        # Join with employees to filter by department
        {"$lookup": {
            "from": "employees",
            "localField": "emp_code",
            "foreignField": "emp_code",
            "as": "emp",
        }},
        {"$unwind": "$emp"},
        {"$match": {"emp.department": department}},
        {"$addFields": {
            "date_obj": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}},
            "is_present": {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
            "is_half_day": {"$ifNull": ["$half_day", False]},
        }},
        {"$addFields": {
            "present_contribution": {
                "$cond": {
                    "if": "$is_present",
                    "then": {"$cond": {"if": "$is_half_day", "then": 0.5, "else": 1.0}},
                    "else": 0.0,
                }
            }
        }},
        {"$group": {
            "_id": "$date_obj",
            "date_str": {"$first": "$date"},
            "present_count": {"$sum": "$present_contribution"},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
        }},
        # Densify to fill missing dates
        {"$densify": {
            "field": "_id",
            "range": {
                "step": 1,
                "unit": "day",
                "bounds": [
                    datetime.combine(from_d, datetime.min.time(), tzinfo=timezone.utc),
                    datetime.combine(to_d + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc),
                ],
            },
        }},
        {"$addFields": {
            "date_str": {
                "$cond": {
                    "if": {"$ifNull": ["$date_str", False]},
                    "then": "$date_str",
                    "else": {"$dateToString": {"format": "%Y-%m-%d", "date": "$_id"}},
                }
            },
            "present_count": {"$ifNull": ["$present_count", 0]},
            "late_count": {"$ifNull": ["$late_count", 0]},
        }},
        {"$sort": {"_id": ASCENDING}},
    ]

    raw = list(db.attendance_logs.aggregate(pipeline))

    # Build headcount per day: employees with joined_on <= that day
    all_dept_emps = list(db.employees.find({"department": department}, {"emp_code": 1, "joined_on": 1, "_id": 0}))

    # Compute attendance_rate and moving_avg_7d in Python
    items = []
    non_null_rates = []  # rolling window for 7d moving average

    for row in raw:
        d_str = row["date_str"]
        d_obj = date.fromisoformat(d_str)
        is_working_day = d_obj.weekday() < 5

        # Headcount: employees with joined_on <= d_str
        headcount = sum(1 for e in all_dept_emps if e["joined_on"] <= d_str)

        present_count = row.get("present_count", 0)
        late_count = int(row.get("late_count", 0))

        # attendance_rate: only on working days with headcount > 0
        if is_working_day and headcount > 0:
            attendance_rate = half_up(present_count / headcount, 4)
        else:
            attendance_rate = None

        # 7-day moving average: only non-null rates in the current window
        non_null_rates.append(attendance_rate)
        window = non_null_rates[-7:]
        valid_window = [r for r in window if r is not None]
        if valid_window:
            moving_avg_7d = half_up(sum(valid_window) / len(valid_window), 4)
        else:
            moving_avg_7d = None

        items.append({
            "date": d_str,
            "is_working_day": is_working_day,
            "headcount": headcount,
            "present_count": half_up(present_count, 2),
            "late_count": late_count,
            "attendance_rate": attendance_rate,
            "moving_avg_7d": moving_avg_7d,
        })

    return {"department": department, "items": items}


# ---------------------------------------------------------------------------
# Endpoints: admin/explain
# ---------------------------------------------------------------------------
def _explain_aggregate(collection, pipeline):
    """Return MongoDB execution statistics for an aggregation pipeline.

    PyMongo does not support aggregate(..., explain=True). Use the MongoDB
    explain command instead so the response includes queryPlanner and
    executionStats.
    """
    return db.command(
        "explain",
        {
            "aggregate": collection.name,
            "pipeline": pipeline,
            "cursor": {},
        },
        verbosity="executionStats",
    )


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(default=10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    from_date: Optional[str] = Query(default=None, alias="from"),
    to_date: Optional[str] = Query(default=None, alias="to"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    valid_endpoints = ["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"]
    if endpoint not in valid_endpoints:
        raise HTTPException(status_code=422, detail=f"endpoint must be one of {valid_endpoints}")

    if endpoint == "attendance_list":
        if date_from and date_to and date_from > date_to:
            raise HTTPException(status_code=422, detail="date_from must not be after date_to")
        q: dict = {}
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
        skip = (page - 1) * page_size
        explain_result = db.attendance_logs.find(q).sort(
            [("date", DESCENDING), ("emp_code", ASCENDING)]
        ).skip(skip).limit(page_size).explain()
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain_result}

    elif endpoint == "employee_monthly":
        if not emp_code:
            raise HTTPException(status_code=422, detail="emp_code is required for employee_monthly")
        if not month:
            raise HTTPException(status_code=422, detail="month is required for employee_monthly")
        first_day_str, last_day_str = _month_range(month)
        pipeline = [
            {"$match": {
                "emp_code": emp_code,
                "date": {"$gte": first_day_str, "$lte": last_day_str},
            }},
        ]
        explain_result = _explain_aggregate(db.attendance_logs, pipeline)
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain_result}

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(status_code=422, detail="month is required for department_summary")
        _, last_day_str = _month_range(month)
        emp_match: dict = {"joined_on": {"$lte": last_day_str}}
        if department:
            emp_match["department"] = department
        explain_result = db.employees.find(emp_match).explain()
        return {"endpoint": endpoint, "collection": "employees", "explain": explain_result}

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(status_code=422, detail="month is required for late_leaderboard")
        first_day_str, last_day_str = _month_range(month)
        match_q: dict = {
            "date": {"$gte": first_day_str, "$lte": last_day_str},
            "late_minutes": {"$gt": 0},
        }
        pipeline = [{"$match": match_q}]
        explain_result = _explain_aggregate(db.attendance_logs, pipeline)
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain_result}

    elif endpoint == "department_trend":
        if not department:
            raise HTTPException(status_code=422, detail="department is required for department_trend")
        if not from_date:
            raise HTTPException(status_code=422, detail="from is required for department_trend")
        if not to_date:
            raise HTTPException(status_code=422, detail="to is required for department_trend")
        match_q = {
            "date": {"$gte": from_date, "$lte": to_date},
        }
        pipeline = [{"$match": match_q}]
        explain_result = _explain_aggregate(db.attendance_logs, pipeline)
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain_result}
