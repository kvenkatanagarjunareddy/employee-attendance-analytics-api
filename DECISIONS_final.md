# DECISIONS.md

## 1. Indexes

I created a unique `employees.emp_code` index for employee identity and duplicate protection. Employee department/join-date indexes support filtered employee queries and department analytics. Attendance uses a unique `(emp_code, date)` index for one record per employee/day, plus indexes for date/employee ordering, employee/date lookups, and status/date filtering. I considered a separate index with the same leading fields as an existing attendance index and rejected it because it would add write and storage cost without serving a different access pattern.

## 2. Punch-in race

Punch-in does not use a check-then-insert flow. It calculates the record and attempts the insert directly. The unique `(emp_code, date)` index makes concurrent inserts atomic at the database level. One request inserts successfully; competing requests receive `DuplicateKeyError`, which the API converts to HTTP 409.

## 3. Ties

The late leaderboard uses MongoDB `$rank`, ordered by total late minutes. Equal totals receive the same rank and the next rank is skipped. The `rank <= limit` filter is applied after ranking, so all employees tied at the cutoff are included. `emp_code` is then used only for deterministic output ordering.

## 4. Headcount

Department summary starts from employees who joined by the end of the requested month, then performs a `$lookup` into attendance. The lookup is unwound with `preserveNullAndEmptyArrays`, so an employee with no attendance records still contributes one person to headcount.

## 5. One change for 100x data

For a much larger dataset, I would introduce pre-aggregated daily/monthly attendance summaries for analytics. This would reduce repeated scans and joins over raw attendance records while keeping the current raw collection available for detailed queries.
