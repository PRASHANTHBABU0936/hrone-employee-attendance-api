# Employee Attendance & Analytics API

A FastAPI + MongoDB API for employee attendance tracking with punch-in/out, manual corrections with audit trail, and analytics.

## Setup

```bash
python -m venv .venv
# Windows:
.venv\Scripts\activate
# macOS/Linux:
source .venv/bin/activate

pip install -r requirements.txt

# Copy and edit environment variables
cp .env.example .env
# Set MONGO_URI and MONGO_DB in .env
# Local MongoDB: mongodb://localhost:27017
# Atlas: mongodb+srv://...

# (Optional) Seed sample data
python sample_seed.py

# Start the server
uvicorn app.main:app --port 8000 --reload
```

MongoDB **6.0 or newer** is required (tested on 7.0). A free Atlas M0 cluster works.

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `MONGO_URI` | `mongodb://localhost:27017` | MongoDB connection string |
| `MONGO_DB` | `attendance_db` | Database name |

## API Endpoints

| Method | Path | Description |
|---|---|---|
| GET | `/health` | Readiness probe (pings MongoDB) |
| POST | `/employees` | Create an employee |
| GET | `/employees` | List employees (paginated, filterable by department) |
| POST | `/attendance/punch-in` | Record a punch-in |
| POST | `/attendance/punch-out` | Record a punch-out |
| GET | `/attendance` | List attendance records (paginated, filterable) |
| PATCH | `/attendance/{emp_code}/{date}` | Regularize (manually correct) a record |
| GET | `/analytics/employees/{emp_code}/monthly` | Monthly summary for one employee |
| GET | `/analytics/departments/summary` | Department summary for a month |
| GET | `/analytics/leaderboard/late` | Late-comers leaderboard |
| GET | `/analytics/departments/{department}/trend` | Daily attendance trend |
| GET | `/admin/explain/{endpoint}` | MongoDB explain for a named endpoint's query |

Full API contract: see `openapi.yaml`.

## Design Highlights

- **All code in `app/main.py`** as required.
- **Indexes created at startup** idempotently: unique compound index on `(emp_code, date)` for concurrency-safe punch-in; unique index on `employees.emp_code`; composite sort index for list queries.
- **Concurrency safety**: duplicate punch-ins and duplicate employee creates are handled by catching `DuplicateKeyError` from the unique index — no check-then-insert races.
- **IST-aware date logic (R1)**: punch-in datetimes are converted to IST before computing the attendance date; overnight shifts are handled correctly.
- **Half-up rounding (R4, R8)**: uses Python's `Decimal` with `ROUND_HALF_UP` — not `round()` which uses banker's rounding.
- **Analytics via MongoDB aggregation pipelines**: no loading collections into memory; uses `$densify` for gap-filling in the trend endpoint.
- **Audit trail**: manual corrections append exactly one `history` entry per `PATCH`; concurrent edits use optimistic concurrency (history-length check) to prevent silent loss.

## What is complete

All endpoints in `openapi.yaml` are implemented:
- ✅ `POST /employees`, `GET /employees`
- ✅ `POST /attendance/punch-in`, `POST /attendance/punch-out`
- ✅ `GET /attendance`, `PATCH /attendance/{emp_code}/{date}`
- ✅ `GET /analytics/employees/{emp_code}/monthly`
- ✅ `GET /analytics/departments/summary`
- ✅ `GET /analytics/leaderboard/late`
- ✅ `GET /analytics/departments/{department}/trend` (uses `$densify`)
- ✅ `GET /admin/explain/{endpoint}`

