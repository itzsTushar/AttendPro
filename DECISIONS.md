# DECISIONS.md  (about 150-200 words in total - plain language, your own words)

### 1. Indexes
I created a unique index on `employees.emp_code` to prevent duplicate employee codes, and a unique compound index on `attendance_logs(emp_code, date)` to guarantee one attendance record per employee per day. I added `(date, emp_code)` for attendance listing and date-based queries, `(date, status)` for status filtering, and `(department, joined_on)` for department headcount queries. I considered separate indexes on `emp_code` and `date` but rejected them because compound indexes better support the main query patterns.

### 2. Punch-in race
If two identical punch-in requests arrive simultaneously, both may initially find no attendance record. Both attempt to insert, but MongoDB's unique compound index allows only one insert to succeed. That request returns `201 Created`; the other raises `DuplicateKeyError`, which the API catches and converts to `409 Conflict`. This avoids relying on an unsafe check-then-insert operation.

### 3. Ties
The leaderboard uses `$rank` on total late minutes. Employees tied at the cutoff receive the same rank, so all employees whose rank is within `limit` are returned. Therefore, the response may contain more than `limit` employees.

### 4. Headcount
The department summary starts from employees who joined by the reporting period's end, then uses `$lookup` with `preserveNullAndEmptyArrays` so employees without attendance logs remain in the results.

### 5. One change for 100x data
I would benchmark query plans and add targeted indexes where needed, while monitoring query latency and index overhead.
