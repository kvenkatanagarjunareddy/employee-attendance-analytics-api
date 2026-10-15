# Employee Attendance & Analytics API

FastAPI + MongoDB backend for employee attendance, punch-in/punch-out, manual attendance corrections with audit history, and MongoDB-based analytics.

## Tech Stack
- Python 3.11+
- FastAPI
- PyMongo
- MongoDB 6.0+

## Local Setup

Create and activate a virtual environment:

```bash
python -m venv .venv
```

Windows PowerShell:

```powershell
.venv\Scripts\Activate.ps1
```

Install dependencies:

```bash
pip install -r requirements.txt
```

Set the following environment variables before starting the application:

```text
MONGO_URI=<your MongoDB connection string>
MONGO_DB=<your database name>
```

Start the API from the repository root:

```bash
uvicorn app.main:app --port 8000
```

Swagger documentation is available at:

```text
http://127.0.0.1:8000/docs
```

## Implemented Areas

- Employee creation and paginated listing
- Attendance punch-in and punch-out
- Attendance listing with filtering and pagination
- Manual attendance regularization with audit history
- Employee monthly analytics
- Department summary analytics
- Late-comer leaderboard with ranking ties
- Department daily trend with gap filling and 7-day moving average
- MongoDB explain endpoint for the main query/aggregation paths
- Startup index creation for the required collections

## Configuration Notes

The application reads `MONGO_URI` and `MONGO_DB` from environment variables. No database credentials or secrets are committed to the repository.

The application uses epoch milliseconds at the API boundary and BSON UTC datetimes in MongoDB.
