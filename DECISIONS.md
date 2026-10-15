# DECISIONS.md

**1. Indexes.**
On `employees`: a **unique** index on `emp_code` (enforces the identifier's
uniqueness and backs the `emp_code`-sorted listing), and `(department,
joined_on)` which serves the department summary's opening match (`joined_on <=
month end`, optionally filtered by department) as an index scan. On
`attendance_logs`: a **unique** `(emp_code, date)` (the natural key — it also
makes punch-in races safe), `(date, emp_code)` which backs the attendance
list's `date desc, emp_code asc` sort and the month-range scans in the
leaderboard and trend, and `(emp_code, punch_in)` for punch-out's "most recent
open record" lookup. I considered a `status` index and rejected it:
`status` has five low-cardinality values, so a scan filtered by date already
narrows the set far more, and the extra index would only add write cost.

**2. Punch-in race.**
I never check-then-insert. Both requests compute the same attendance `date`,
build the document, and call `insert_one`. The unique `(emp_code, date)` index
lets exactly one commit; the other raises `DuplicateKeyError`, which I translate
to **409**. The winner gets **201**. No read happens between the decision and
the write, so there is no window to lose.

**3. Ties.**
The leaderboard uses standard competition ranking, so a tie shares a rank and
the next rank is skipped (1, 2, 2, 4). `limit` is applied **by rank**, not by
row position: I return every row whose `rank <= limit`. So two employees tied
at the `limit` cutoff are **both** returned, and the response can hold more than
`limit` rows. Rows are ordered `total_late_minutes` desc, then `emp_code` asc,
so the ranking is deterministic.

**4. Headcount.**
The department summary aggregation **starts from `employees`** (matching
`joined_on <= end of month`), then `$lookup`s each employee's attendance for the
month. Because the pipeline is driven by the employee list, someone with zero
logs still produces exactly one row and is counted once in `headcount`; the
`$lookup` simply yields an empty `logs` array for them. Employees who join after
the month are excluded by the opening match.

**5. One thing I'd change at 100x the data.**
Replace `skip`/`limit` on the attendance list with **keyset (range)
pagination** on `(date, emp_code)` — deep `skip` still walks every skipped index
entry, which gets expensive on a 10M-row collection — and pre-compute the
monthly analytics into a rolled-up `monthly_summary` collection (refreshed on
write or on a schedule) so the dashboards read one small document per
employee-month instead of re-aggregating the raw logs on every request.
