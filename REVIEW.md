# REVIEW.md

Defects found in the starter `app/main.py`, all fixed in this version.

| # | Where | What is wrong | How you'd notice it | How I fixed it |
|---|---|---|---|---|
| 1 | `MongoClient(...)` | No `tz_aware=True`, so PyMongo returns naive datetimes. Naive minus aware raises `TypeError`. | `compute_overtime` crashes (500) on any stored punch time. | `tz_aware=True`; all instants handled as aware UTC. |
| 2 | `compute_late_minutes` | Uses the punch's own (UTC/naive) date and hour, not IST, so shift start is on the wrong day/hour. | Punch 10:05 IST gives a nonsense value; overnight shifts break. | Shift start built from the attendance date in IST (R1). |
| 3 | `compute_late_minutes` | `int()` before `> 10` makes 10:01-10:59 count as 10, so it is not late; early punches go negative; truncation is toward zero. | 09:40:59 should be 10 late, starter gives 0. | Work in seconds: late only if `secs > 600`, then `secs // 60`. |
| 4 | `compute_work_hours` | `round()` on a float: banker's rounding plus float error. No second truncation. | 18 s = 0.005 h gives 0.0, should be 0.01. | `Decimal` with `ROUND_HALF_UP` (R4). |
| 5 | `compute_overtime` | No 30-minute minimum. Shift end always on the same day, so overnight shifts are wrong. Mixes naive and aware. | 5 min after shift end is counted. Overnight 22:00-06:00 gives huge or negative values. | Shift end moved to next day for overnight shifts; minimum of 30 (R3). |
| 6 | `create_employee` | Check-then-insert race; no unique index. `created_at` is local naive `datetime.now()`. No validation (code format, email, shift times, `joined_on`). Returns a datetime, not epoch ms. | Parallel creates give two 201s. `emp_code="x"` accepted. | Unique index, catch `DuplicateKeyError` (409), Pydantic constraints, `created_at` UTC and returned as epoch ms. |
| 7 | `list_employees` | `skip = page * page_size` skips page 1. `total` ignores the filter. No sort. `page`/`page_size` unbounded. | `page=1` misses the first rows. `total` wrong with `department`. | `(page-1)*size`, `count_documents(q)`, sort by `emp_code`, `ge/le` validation. |
| 8 | `punch_in` | Unknown employee: `emp` is `None`, so `TypeError` (500, should be 404). | Post an unknown `emp_code`. | 404. |
| 9 | `punch_in` | `datetime.fromtimestamp` / `now()` use the server's local zone and naive values. Date is not IST (R1). Not truncated to seconds. | Punch at 23:00 IST on a UTC server lands on the wrong date. | Epoch ms to aware UTC, truncated; date via `attendance_date` (IST, overnight aware). |
| 10 | `punch_in` | Check-then-insert race; no unique index. | Two parallel requests both return 201. | Unique `(emp_code, date)` index; the loser's `DuplicateKeyError` becomes 409. |
| 11 | `punch_in` | Any `status` accepted (ABSENT, junk). Any `punched_at` accepted (seconds, floats, negatives). | `status="ABSENT"` or `punched_at=1783312500`. | `Literal` presence statuses; `StrictInt` bounded to epoch-ms range, so 422. |
| 12 | `punch_in` response | Leaks `id` (`_id`) and returns raw datetimes. | Response contains `id`. | `record_out` builds the contract shape, epoch ms. |
| 13 | `list_attendance` | Loads the whole collection into memory and sorts in Python. | Slow, memory heavy at 100k records. | Server-side `find().sort().skip().limit()`, backed by indexes. |
| 14 | `list_attendance` | Sort is by `date` only (needs `emp_code` asc as tie-break); `total` = `len(docs)`; no validation of dates, `status`, `page`, `page_size`; leaks `_id` and datetimes. | `date_from > date_to`, `page_size=100000`, `status=FOO` all accepted. | Strict date parsing and 422s, `Literal` status, bounded paging, projection, `record_out`. |
| 15 | `/health` | Always `ok` even when MongoDB is down. | Stop MongoDB, call `/health`. | `ping`; 503 on failure. |
| 16 | startup | No indexes at all, so every query is a COLLSCAN. | `explain` shows COLLSCAN. | Idempotent `ensure_indexes()` in lifespan (and retried from `/health`). |

## Looked at and judged fine
- `load_dotenv()` defaults to `override=False`, so real environment variables already win.
- Pydantic ignores unknown keys, so client-sent derived fields (`work_hours` etc.) are ignored as the contract asks.
- `punch_in` stores `history: []` on creation. That is correct.
