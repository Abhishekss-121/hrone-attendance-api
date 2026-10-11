# DECISIONS.md

1. **Indexes.** `employees`: unique `emp_code` (lookup, no duplicates), `(department, emp_code)` (list by department), `joined_on` and `(department, joined_on)` (headcount). `attendance_logs`: unique `(emp_code, date)` (one record per day, monthly lookup), `(date desc, emp_code)` (list, leaderboard), `(status, date desc, emp_code)` (status filter), `(emp_code, punch_in desc)` (open record at punch-out). I rejected an index on `late_minutes`: the leaderboard sums per employee first, so it would not help and would slow writes.

2. **Punch-in race.** Both requests reach `insert_one` with the same `(emp_code, date)`. The unique index lets MongoDB accept only one; the other raises `DuplicateKeyError`, which I return as 409. One 201, one 409. I never check first.

3. **Ties.** I use `$rank` (1, 2, 2, 4) and apply `limit` after ranking, keeping every row with `rank <= limit`. All tied employees at the cutoff are returned, even if that is more than `limit` rows, as the contract asks.

4. **Headcount.** The summary starts from `employees`, not the logs, and attaches each person's logs with `$lookup`. Someone with no logs still gives a row of zeros, so they are counted.

5. **100x the data.** I would store pre-calculated monthly totals per employee, updated at each punch, instead of recalculating from raw records on every request.