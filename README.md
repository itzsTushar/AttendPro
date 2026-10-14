# HR Attendance System — Backend

A REST API backend for a small HR attendance system built with **Python 3.12.3, FastAPI, and MongoDB**. Employees can punch in and out, managers can correct attendance records, and businesses can analyze late arrivals, department attendance, and attendance trends.

## Tech Stack
- Python 3.12.3
- FastAPI
- MongoDB with PyMongo
- Uvicorn

## Local Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

export MONGO_URI="your_mongodb_uri"
export MONGO_DB="your_database_name"

python3 sample_seed.py
uvicorn app.main:app --port 8000 --reload
```

## API Documentation

Once the server starts, visit:

- Swagger UI: http://127.0.0.1:8000/docs

