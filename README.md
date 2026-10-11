# Employee Attendance & Analytics API

A small API for employee attendance: punch-in and punch-out with business rules, manual corrections with an audit trail, and analytics built with MongoDB aggregation pipelines.

Built with Python, FastAPI and MongoDB (Atlas free tier M0 while developing). All the code is in `app/main.py`.

## How to run

1. Create and activate a virtual environment, then install the packages:
   ```
   python -m venv .venv
   .venv\Scripts\activate          (Mac/Linux: source .venv/bin/activate)
   pip install -r requirements.txt
   ```
2. Create a `.env` file in the project folder (it is not in the repository):
   ```
   MONGO_URI=your-mongodb-connection-string
   MONGO_DB=attendance_db
   ```
3. Start the app:
   ```
   uvicorn app.main:app --port 8000
   ```
4. Open `http://localhost:8000/docs` to try the endpoints.

The indexes are created automatically when the app starts.

## What is done

Everything in the contract is implemented: `/health`, employees (create, list), punch-in, punch-out, attendance list, corrections (PATCH) with history, the three analytics endpoints (employee monthly, department summary, late leaderboard), the department trend, and `/admin/explain/{endpoint}`.

Nothing is missing.

## Things you should know

- Duplicate punch-ins and duplicate punch-outs are stopped by the database (a unique index and an atomic update), not by "check first, then write".
- Dates and times are handled in UTC and converted to IST for business rules. Times in responses are epoch milliseconds.
- I tested with the sample data and with my own small test data (different departments, mid-month joiners, ties, weekends, an overnight shift). I did not test with a dataset as large as 100,000 records, or with heavy parallel traffic.
- `REVIEW.md` lists the defects I found in the starter code. `DECISIONS.md` has my answers to the five questions.