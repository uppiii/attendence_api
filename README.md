# Employee Attendance & Analytics API

FastAPI + MongoDB (6.0+). All code is in `app/main.py`.

```bash
pip install -r requirements.txt
```

Set `MONGO_URI` and `MONGO_DB` in the environment (or in an uncommitted local `.env`), then start the API:

```bash
uvicorn app.main:app --port 8000
```

Run the rule tests with `python -m unittest discover -s tests -v`.

Indexes are created at startup. Every endpoint in `openapi.yaml` is implemented, including `/admin/explain/{endpoint}`.

## Notes
- Analytics use MongoDB aggregation pipelines (`$lookup`, `$setWindowFields`, and `$densify`).
- The trend range is inclusive (92 days allowed, 93 rejected); its moving average uses the reported 4-decimal daily rates. Concurrent regularizations return 409 to a losing request.
- The endpoints and explain plans were smoke-tested against sample and edge-case data on a local MongoDB. The hidden 100,000-record dataset was not available for validation.
- `python sample_seed.py` replaces the `employees` and `attendance_logs` collections in the configured database with the sample data.
