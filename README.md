# Employee Attendance & Analytics API

A FastAPI + MongoDB service implementing the HROne assignment contract
(`openapi.yaml`) and storage model (`DATA_MODEL.md`). All application code is in
`app/main.py`.

## Status: complete

Every endpoint in the contract is implemented: `/health`, employee create/list,
punch-in, punch-out, regularize (PATCH with audit trail), the four analytics
endpoints (employee monthly, department summary, late leaderboard, department
trend), and `/admin/explain/{endpoint}`. Nothing is stubbed.

## Requirements

Python 3.11+, MongoDB 6.0+ (the grader uses 7.0). Atlas M0 works.

## Local setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # set MONGO_URI / MONGO_DB
                                # local docker: docker run -p 27017:27017 mongo:7
python sample_seed.py           # loads the 9 sample docs (optional)
uvicorn app.main:app --port 8000 --reload
```

`MONGO_URI` and `MONGO_DB` are read from the environment; a local `.env` is
loaded for convenience, but real environment variables take precedence. All
indexes are created idempotently at startup. Open `http://localhost:8000/docs`
for interactive docs.

## Design highlights

- **Instants** cross the HTTP boundary as integer epoch **milliseconds** and are
  stored as BSON datetimes. The Mongo client is `tz_aware=True`, so everything
  read back is timezone-aware UTC — no naive/aware mixing. Every instant is
  truncated to whole seconds before it is stored or returned (R1).
- **Business time is IST (UTC+05:30).** The attendance `date` and rules R2–R5
  are computed against IST wall-clock instants built from the stored shift
  strings, including the overnight-shift date rule.
- **Rounding is HALF-UP via `Decimal`** (R4/R8), not Python's banker's `round`.
- **Concurrency** uses unique indexes + atomic writes, never check-then-insert:
  punch-in relies on the unique `(emp_code, date)` index; punch-out uses
  `find_one_and_update` on `{punch_out: null}`; regularize uses optimistic
  concurrency on the `history` length so no correction silently overwrites
  another (loser gets 409).
- **Analytics are MongoDB aggregation pipelines** that read the **stored**
  derived values (never recomputed from punch times). The department summary
  starts from `employees` so zero-log staff are still counted (R9); the trend
  uses `$densify` to emit one row per day and `$setWindowFields` for the 7-day
  moving average — no Python day loops.
- **`/admin/explain`** rebuilds each endpoint's exact query/pipeline and returns
  `executionStats`. Every analytics pipeline opens with an index-backed `$match`
  and the list sort is index-backed, so plans use `IXSCAN`, not `COLLSCAN`.

## Notes / interpretations

- `/attendance/{emp_code}/{date}` business-rule violations (e.g. `punch_out <=
  punch_in`, a no-op correction) return **422** with the FastAPI `{"detail":
  "..."}` body; input-schema failures return the standard 422 validation array.
- Department trend: I treat "range exceeds 92 days" as `(to - from).days > 92`.
- `late_count` / overtime / late totals include weekend records (R7); only
  `present_days` and `working_days` are restricted to Mon–Fri.

## Tests

`app/main.py`'s pure business logic (R1–R5 time math, half-up rounding,
overnight attendance dates, competition ranking, working-day counting) was
validated against the documented sample values in `DATA_MODEL.md`. See
`REVIEW.md` for the defects found in the starter and `DECISIONS.md` for the
design-decision answers.
