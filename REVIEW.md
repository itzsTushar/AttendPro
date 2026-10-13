# REVIEW.md

List every defect you found in the starter `app/main.py` (helpers and endpoints). For each one:

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | `app/main.py` — `health()` function | The endpoint always returned `200 OK` without checking whether MongoDB was available. This violated the readiness probe requirement. | first we noticed from `openaiapi.yaml` then fucntion shoulde behave like this then we check to Stop MongoDB or configure an invalid connection, then call `GET /health`. The original implementation still returned `200` instead of `503`. | Added `client.admin.command("ping")` to verify MongoDB connectivity. The endpoint returns `{"status": "ok"}` when the ping succeeds and raises an HTTP `503 Service Unavailable` exception when it fails. |
|---|---|---|---|---|
| 2 | `app/main.py` — `create_employee()` | Checking for an existing employee before insertion is vulnerable to concurrent requests. Duplicate-key errors were not handled, equal shift times were not rejected, and `created_at` used a potentially timezone-naive timestamp. | Send concurrent POST requests with the same `emp_code`; verify exactly one returns `201` and the others return `409`. Submit equal `shift_start` and `shift_end` values and expect `422`. | Enforce uniqueness with a MongoDB unique index, catch `DuplicateKeyError`, validate that shift times differ, and use a UTC-aware timestamp for `created_at`. |

Also note anything you looked at and decided was **not** a defect, and why.
