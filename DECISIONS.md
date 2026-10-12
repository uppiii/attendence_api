# DECISIONS.md

1. **Indexes.** Unique `emp_code` on employees; unique `(emp_code, date)` on logs, which is the punch-in race guard and join key. `(date -1, emp_code)` serves list sort and the leaderboard. `(emp_code, punch_in -1)` serves punch-out. `(department, joined_on)` serves filtered headcount and trend; `joined_on` alone supports an unfiltered summary. Rejected: an index on `late_minutes`, because the date range already narrows enough.
2. **Punch-in race.** Both requests compute the same date and insert. The unique index lets exactly one insert succeed. The other gets `DuplicateKeyError`, which I turn into 409. There is no check-then-insert.
3. **Ties.** I rank with `$rank` (1, 2, 2, 4) and keep every row with `rank <= limit`. Tied employees at the cutoff all appear, so there can be more than `limit` rows. Cutting by array position would drop one tied person arbitrarily.
4. **Headcount.** The summary starts from `employees` and `$lookup`s their logs. Someone with no logs still produces one document with an empty log array, so they are counted. Only people who joined after month end are filtered out.
5. **At 100x.** I would stop deriving analytics at read time and keep pre-aggregated monthly totals per employee, updated on punch and regularize.
