# DECISIONS.md

## 1. Indexes

**Created:**

| Index | Collection | Fields | Purpose |
|---|---|---|---|
| `emp_code_unique` | `employees` | `emp_code` ASC, unique | Enforces uniqueness at the database level. Concurrent duplicate `POST /employees` requests are resolved by the DB engine — exactly one gets 201 and all others get a `DuplicateKeyError` that the app maps to 409. |
| `emp_date_unique` | `attendance_logs` | `(emp_code, date)` ASC+ASC, unique | Enforces the "at most one record per (employee, date)" invariant. The same atomic guarantee that solves the employee-create race also solves the punch-in race. |
| `date_desc_emp_asc` | `attendance_logs` | `date` DESC, `emp_code` ASC | Covers the sort order required by `GET /attendance` (`date` descending then `emp_code` ascending) and its filter predicates, turning what would be a full collection scan into an index scan on large datasets. |
| `emp_code_idx` | `attendance_logs` | `emp_code` ASC | Supports analytics pipelines that filter by a single employee (e.g., `employee_monthly`) without scanning the whole collection. |

**Considered and rejected:**

A compound `(emp_code, date, status)` index was considered to cover the analytics `$match` stages fully. It was rejected because the same queries are already served by the two existing `attendance_logs` indexes (the composite unique index covers `emp_code + date` lookups; the date-descending index covers range scans). Adding a third index would increase write amplification on every insert and update for marginal read gain at this data size.

---

## 2. Punch-in race condition

Two identical `POST /attendance/punch-in` requests arrive at exactly the same moment for `EMP0001` on `2026-07-06`.

1. Both requests pass Pydantic validation and look up the employee in `employees` (read-only, no race here).
2. Both compute the same `attendance_date` (`"2026-07-06"`) and build the same document.
3. Both call `db.attendance_logs.insert_one(doc)`.
4. MongoDB processes these two inserts serially (writes are serialized at the collection level). The first insert succeeds and creates the document. The second insert attempts to insert a document with the same `(emp_code, date)` pair, which violates the unique compound index, and MongoDB raises a `DuplicateKeyError`.
5. The application catches `DuplicateKeyError` and returns HTTP 409 to the second request.

There is no `find_one` guard before the insert, so there is no window between the check and the insert where both requests can see "no record" and both proceed. The uniqueness is enforced atomically by the database.

---

## 3. Ties in the late leaderboard

Say `limit=5` and employees A (300 min), B (200 min), C (200 min), D (200 min), E (100 min) are the top latecomers.

The pipeline sorts by `total_late_minutes DESC, emp_code ASC` and returns all rows. The application then assigns **standard competition ranks** in Python:

- A → rank 1
- B, C, D (tied at 200) → rank 2 (all three get rank 2; next would be rank 5)
- E → rank 5

Because `limit=5` means "return everyone whose rank ≤ 5", all four employees with rank ≤ 5 are returned — that is A, B, C, D, and E — a total of **5 rows**. If E also tied with someone else at rank 5, both would be included.

The spec is explicit: `limit` applies after ranking and the response can contain more than `limit` rows when there are ties at the boundary rank.

---

## 4. Headcount for employees with zero logs

The `department_summary` aggregation starts from the **`employees` collection**, not `attendance_logs`. The pipeline is:

1. `$match` on `employees` where `joined_on <= last_day_of_month` (and optionally `department`).
2. `$group` by `department` to compute `headcount` as `$sum: 1` — this counts every matching employee, regardless of whether they have any attendance record.
3. `$lookup` from `attendance_logs` for those employees in the month — employees with zero logs simply produce an empty `logs` array.
4. All derived counters (`present_days`, `late_count`, etc.) use `$sum` over the `logs` array, which naturally returns 0 for an empty array.

This means employees who joined before or during the month but never swiped in are counted in `headcount` but contribute 0 to all attendance metrics — exactly the behaviour described in R9.

---

## 5. One thing I would change for 100× the data

I would move the `department_summary` computation from a single aggregation that fans out to a Python-side loop (used for `avg_work_hours`) into a **fully server-side MongoDB aggregation** with a pre-aggregated `$facet` or a materialized intermediate collection updated incrementally via change streams.

At current scale, the Python loop issuing one extra `aggregate` per department is fine. At 100× the data (millions of logs, hundreds of departments), the N+1 aggregation queries would cause unacceptable latency. The right fix is a single pipeline that groups by `(department, status, has_work_hours)` in one pass, computes `avg_work_hours` in the same pipeline using `$avg` after a `$match` on presence statuses, and returns everything in one round-trip to MongoDB.
