# Ballpark Stats

Anonymous aggregate stats server for the [Close Enough](https://github.com/jackgreening1995-creator/close-enough) iOS app.

Powers the *"You scored higher than X% of today's players"* line on share cards. One file (`main.py`), FastAPI + SQLite, ~200 lines.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/round` | Record a played round. Body: `{puzzle_number, total_score, device_id, played_at}`. 204 on success. |
| `GET`  | `/stats/{n}` | `{median, p25, p75, count}` for puzzle n. 0-count when no data. |
| `GET`  | `/privacy` | Public HTML privacy disclosure. |
| `DELETE` | `/me/{device_id}` | Purge all rows for a device. 204 on success. |
| `GET`  | `/` | Health check. Returns 200 with `{ok: true}`. |

## Local dev

```bash
pip install -r requirements.txt
uvicorn main:app --reload
```

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

## Deploy to Railway

1. Create a new GitHub repo for this server (suggested: `ballpark-stats`).
2. Push this directory to it: `git init && git add . && git commit -m "init" && git remote add origin <url> && git push -u origin main`
3. Sign up at [railway.app](https://railway.app) (free tier, no card).
4. New Project → Deploy from GitHub → select the repo.
5. Railway auto-detects the `Procfile` and runs `uvicorn`. Free tier will be $0-5/mo depending on usage.
6. Once deployed, copy the assigned URL (e.g. `https://ballpark-stats-production.up.railway.app`). That's the `STATS_BASE_URL` for the iOS app.

## Privacy

See `/privacy` and the inline docstrings. The server is **anonymous by design**:
- No auth, no cookies, no IP logging beyond what Railway does at the load balancer.
- `device_id` is `identifierForVendor`, which resets on app reinstall.
- No PII, no name, no email, no contacts, no location, no advertising id.
- Retention is 60 days by default (configurable via `BALLPARK_RETAIN_DAYS` env var).
- `DELETE /me/{device_id}` purges all rows for a device on demand.

If/when the app actually catches on, **add rate limiting and device-id signing.** Not before — that's premature.

## What this server is NOT

- Not authenticated. Anyone can `POST /round` with a fake `deviceId` and pollute the stats. For the current scale (low thousands of daily plays) this doesn't matter. If it ever does, add a rate limit + a per-device HMAC. That's a one-day change at most.
- Not multi-region. Single Railway instance. Latency from the iOS app to the server is single-digit ms from Australia (Railway's Sydney edge).
- Not a real database. SQLite. Fine for thousands of rows per day. Migrate to Postgres when you outgrow it.
- Not a real admin dashboard. Use `sqlite3 ballpark.db` over SSH.

## License

Same as the iOS app.
