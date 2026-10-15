# Employee Attendance & Analytics API

Built with Python, FastAPI and MongoDB. All the code is in `app/main.py`.
You need Python 3.11 or newer and MongoDB 6.0 or newer.

## How to run it
```bash
python -m venv .venv
# Windows: .venv\Scripts\Activate.ps1 
pip install -r requirements.txt
cp .env.example .env          # then put your own MONGO_URI and MONGO_DB in it
python sample_seed.py        
python -m uvicorn app.main:app --port 8000
```
Open `http://127.0.0.1:8000/health`. You should see `{"status":"ok"}`. If MongoDB is not reachable it returns 503.
The indexes are created automatically when the app starts.

| Variable | What it is | Default |

| `MONGO_URI` | MongoDB connection string | `mongodb://localhost:27017` |
| `MONGO_DB` | database name | `attendance_db` |

Real environment variables always win over the `.env` file. Never commit `.env`.

## How to run the tests
Rule tests (no database needed):
```bash
python -m unittest tests.test_rules -v
```
Integration tests. **They delete all employees and attendance logs in the database**, so they only run if the database name contains "test".
Start the server in one terminal and run the tests in another. Set the same variables in both:
```bash
MONGO_URI=<your connection string>
MONGO_DB=attendance_test
python -m uvicorn app.main:app --port 8000      # terminal 1
python -m unittest tests.test_api -v            # terminal 2
SDT_SCALE=1 python -m unittest tests.test_api.TestScale -v   # optional: 100,000 records
```
On Windows, use `127.0.0.1` and not `localhost` when testing. With `localhost` every request was about 2 seconds slower on my computer.

## Test results (what I actually saw)
- **Rule tests:** 38 tests, all passed.
- **Sample data:** all 9 sample documents match the values my code calculates.
- **Integration tests** (MongoDB Atlas 8.0.34, server running locally): 29 of 30 passed on the first full run. That covered the analytics checks, the concurrency checks (duplicate create, punch-in, punch-out, corrections), validation errors, and the explain plans.
- **The one failure** was `test_punch_out_errors`. It failed because an earlier test had left an open punch-in for the same test employee, so the test itself was wrong, not the API. It passed when run alone. I then gave that test its own employee. I have not yet re-run the whole suite after that change.
- **Explain plans:** the winning plan for all five endpoints used `IXSCAN` and none used `COLLSCAN`.
- **Speed with 100,000 records:** not measured yet. A direct MongoDB query for one page took about 30 to 110 ms on Atlas. My first API timing was about 2 seconds for every endpoint, which looked like the `localhost` problem above. I have not confirmed this.

## Choices I made where the rules could be read two ways
- The 7-day moving average uses the already rounded (4 decimal) daily rates.
- For the trend, 92 days counts both the first and the last day, so 92 days is allowed and 93 is not.
- Average work hours uses every present record that has work hours, weekends included. Present days only counts Monday to Friday.
- A correction (PATCH) that does not change status, punch-in or punch-out gives 422.
- If two corrections to the same record arrive together, one succeeds and the other gets 409.

## Limits
- Only tested on MongoDB 8.0. The trend query needs 6.0 or newer.
- Not tested with more than one server running at the same time.
