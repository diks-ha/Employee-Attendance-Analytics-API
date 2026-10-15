# REVIEW.md

These are the main problems I found in the starter `app/main.py`, and how I fixed them.
For the first six I called the starter's own functions with the inputs shown and saw the wrong answer.
The other four I found by reading the code, and I checked the fixes with tests.

## 1. Late minutes used the wrong clock
**Where:** `compute_late_minutes`
**Problem:** The shift start (09:30) is in IST, but the function compared it with a UTC time.
**Example:** Punch-in at 10:05 IST, shift starts 09:30. The starter returned 0. It should be 35.
**Fix:** Convert the punch-in to IST and build the shift start in IST.

## 2. The 10-minute grace period was wrong
**Where:** `compute_late_minutes`
**Problem:** The rule is "late only if MORE than 10 minutes after the start". The starter counted whole minutes first, so someone one second late still counted as on time.
**Example:** Punch-in at 09:40:01 (shift 09:30). The starter returned 0. It should be 10.
**Fix:** Compare in seconds (more than 600), then turn it into whole minutes.

## 3. Overnight shifts were not handled
**Where:** `compute_late_minutes` and `compute_overtime`
**Problem:** An overnight shift (22:00 to 06:00) ends on the next day. The starter always put the shift end on the same day.
**Example:** Shift 22:00 to 06:00, punch-out at 06:40 the next morning. The starter returned 1480 overtime minutes. It should be 40.
**Fix:** Work out the attendance date first, then add one day to the shift end when the shift is overnight.

## 4. Overtime had no 30-minute minimum
**Where:** `compute_overtime`
**Problem:** The rule says overtime counts only if it is at least 30 minutes. The starter counted any extra time.
**Example:** Shift ends 18:30, punch-out at 18:40. The starter returned 10. It should be 0.
**Fix:** Return 0 unless the minutes are 30 or more.

## 5. Work hours were rounded the wrong way
**Where:** `compute_work_hours`
**Problem:** Python's `round()` rounds exact halves to the nearest even number. The rule says round half up.
**Example:** 9 hours 7 minutes 30 seconds is exactly 9.125 hours. The starter returned 9.12. It should be 9.13.
**Fix:** Use whole-number maths: `(seconds * 200 + 3600) // 7200`.

## 6. The attendance date depended on the server's time zone
**Where:** `punch_in`
**Problem:** The starter used `datetime.fromtimestamp(...)`, which uses the computer's own time zone. The rule says the date must be the IST date.
**Example:** A punch at 01:40 IST on 7 July. On a UTC server the starter gave the date 6 July. On an IST server it gave 7 July. The correct answer is 7 July.
**Fix:** Store times in UTC and always work out the date in IST.

## 7. Two requests at the same time could both succeed
**Where:** `create_employee` and `punch_in`
**Problem:** The starter first checked if the record exists, then inserted it. Two requests arriving together can both pass the check, so you get duplicates. There was also no unique index.
**Fix:** Added unique indexes (`emp_code`, and `emp_code` + `date`). I insert directly and turn MongoDB's duplicate error into a 409.
**Checked:** My test sends 8 requests at once and gets exactly one 201 and seven 409s.

## 8. Employee list paging was wrong
**Where:** `list_employees`
**Problem:** The skip was `page * page_size`, so page 1 skipped the first page. The `total` also counted all employees even when a department filter was used. There was no sorting and no limits on `page` and `page_size`.
**Fix:** Skip is `(page - 1) * page_size`, total uses the same filter, results are sorted by `emp_code`, and bad values give 422.

## 9. Timestamps were not checked properly
**Where:** `PunchInIn` (the request model)
**Problem:** `punched_at` accepted floats, text and true/false. A time in seconds instead of milliseconds was stored as a date in 1970. The `status` field accepted any text, even `ABSENT`.
**Fix:** `punched_at` must be a real integer in milliseconds within a sensible range, and `status` can only be PRESENT, WFH or ON_DUTY.

## 10. The attendance list loaded everything into memory
**Where:** `list_attendance`
**Problem:** It read the whole collection, sorted it in Python and cut out one page. It also sorted by date only, so rows with the same date could move between pages.
**Fix:** MongoDB now does the filter, sort, skip and limit using an index on `(date desc, emp_code)`.

## Smaller things I also fixed
- An unknown employee in `punch_in` crashed with a 500. It now returns 404.
- `/health` never checked MongoDB. It now pings it and returns 503 if it is down.
- The Mongo client returned dates without a time zone. I set `tz_aware=True`.
- The punch-in response leaked an `id` field and returned dates as text instead of milliseconds.
- The starter created no indexes. They are now created when the app starts.

## Things I checked and left alone
- `load_dotenv()` does not overwrite variables that are already set, so real environment variables already win. That is what the task asks for.
- The default `localhost` database address has no password in it, so it is fine as a local default.
