# Ballpark Stats

Anonymous aggregate stats server for the Ballpark iOS app.

Powers the share percentile, question medians, duel flow, and soft-launch event counters. One file (`main.py`), FastAPI, SQLite for local dev, Postgres in production.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/round` | Record a played round. Body: `{puzzle_number, total_score, device_id, played_at}`. 204 on success. |
| `POST` | `/event` | Record anonymous event pings. Body: `{device_id, event_name}`. |
| `GET`  | `/stats/{n}` | `{median, p25, p75, count}` for puzzle n. 0-count when no data. |
| `GET`  | `/question-aggregate/{n}/{i}` | Median guess + count for one question slot. |
| `POST` | `/duel` | Create a friend duel and return a token. |
| `GET`  | `/duel/{token}` | Fetch a duel's current state. |
| `POST` | `/duel/{token}/join` | Claim the joiner slot. |
| `POST` | `/duel/{token}/result` | Submit a duel result. |
| `GET`  | `/duels/open/{device_id}` | Poll open duels for one device. |
| `GET`  | `/admin/summary` | Token-protected launch funnel summary. |
| `GET`  | `/privacy` | Public HTML privacy disclosure. |
| `DELETE` | `/me/{device_id}` | Purge all rows for a device. 204 on success. |
| `GET`  | `/` | Health check. Returns 200 with `{ok: true}`. |

## Local dev

```bash
pip install -r requirements.txt
uvicorn main:app --reload
```

SQLite is the default local store. To test the production path, set
`DATABASE_URL` to a Postgres connection string first.

Then:
```bash
curl -X POST -H 'Content-Type: application/json' \
  -d '{"puzzle_number":525,"total_score":3420,"device_id":"abc","played_at":"2026-06-10T08:00:00Z"}' \
  http://localhost:8000/round
# → 204

curl http://localhost:8000/stats/525
# → {"puzzle_number":525,"median":3420,"p25":3420,"p75":3420,"count":1}

curl http://localhost:8000/privacy
# → HTML page
```

## Deploy

Set these env vars in production:

- `DATABASE_URL` — Neon/Postgres connection string
- `BALLPARK_ADMIN_TOKEN` — token for `/admin/summary`
- `BALLPARK_RETAIN_DAYS` — optional retention override
- `BALLPARK_DUEL_EXPIRY_DAYS` — optional duel expiry override

## Privacy

See `/privacy` and the inline docstrings. The server is **anonymous by design**:
- No auth, no cookies, no IP logging beyond what the host does at the load balancer.
- `device_id` is `identifierForVendor`, which resets on app reinstall.
- No PII, no name, no email, no contacts, no location, no advertising id.
- Retention is 60 days by default (configurable via `BALLPARK_RETAIN_DAYS` env var).
- `DELETE /me/{device_id}` purges all rows for a device on demand.
- `POST /event` stores only anonymous event names (`app_open`, `share_tap`,
  `duel_created`, `duel_completed`) against the anonymous device id.

## What this server is NOT

- Not authenticated. Admin access is header-token protected only.
- Not multi-region. Keep it simple until launch data says otherwise.
- Not a dashboard UI. `/admin/summary` is the dashboard.

## License

Same as the iOS app.
