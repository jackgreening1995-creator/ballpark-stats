"""
Ballpark Stats — anonymous aggregate stats server.

Powers the "you scored higher than X% of today's players" line on
share cards. One file, ~200 lines, FastAPI + SQLite.

Endpoints:
- POST /round — record a played round. No PII beyond a device id.
- GET /stats/{puzzle_number} — median, p25, p75, count for a puzzle.
- GET /privacy — human-readable privacy disclosure.
- DELETE /me/{device_id} — purge all rows for a device.
- GET / — health check (returns 200).

Deploy:
  uvicorn main:app --host 0.0.0.0 --port $PORT
"""

import math
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field, field_validator

# ----------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------

DB_PATH = os.environ.get("BALLPARK_DB", "ballpark.db")
MAX_RETAINED_DAYS = int(os.environ.get("BALLPARK_RETAIN_DAYS", "60"))

# ----------------------------------------------------------------------------
# DB
# ----------------------------------------------------------------------------


@contextmanager
def get_db():
    """Context-managed SQLite connection. Uses a fresh connection
    per request — SQLite's locking model is per-connection, and
    FastAPI workers are short-lived. Cheap on this scale."""
    conn = sqlite3.connect(DB_PATH, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
    finally:
        conn.close()


def init_db() -> None:
    """Create the rounds table if it doesn't exist. Called on app
    startup. Idempotent."""
    with get_db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS rounds (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id TEXT NOT NULL,
                puzzle_number INTEGER NOT NULL,
                total_score INTEGER NOT NULL,
                played_at TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE(device_id, puzzle_number)
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_rounds_puzzle ON rounds(puzzle_number)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_rounds_device ON rounds(device_id)"
        )


# ----------------------------------------------------------------------------
# Models
# ----------------------------------------------------------------------------


class RoundIn(BaseModel):
    puzzle_number: int = Field(..., gt=0, le=100_000)
    total_score: int = Field(..., ge=0, le=5000)
    device_id: str = Field(..., min_length=1, max_length=64)
    played_at: str = Field(..., min_length=10, max_length=40)

    @field_validator("device_id")
    @classmethod
    def _device_id_clean(cls, v: str) -> str:
        # Restrict to URL-safe characters. identifierForVendor is a
        # UUID, but be defensive.
        allowed = set(
            "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_."
        )
        if not all(c in allowed for c in v):
            raise ValueError("device_id contains invalid characters")
        return v

    @field_validator("played_at")
    @classmethod
    def _played_at_iso8601(cls, v: str) -> str:
        # Accept anything datetime.fromisoformat can parse. Reject
        # everything else.
        try:
            datetime.fromisoformat(v.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            raise ValueError("played_at must be ISO 8601")
        return v


class StatsOut(BaseModel):
    puzzle_number: int
    median: int
    p25: int
    p75: int
    count: int


# ----------------------------------------------------------------------------
# Percentile helpers
# ----------------------------------------------------------------------------


def percentile(sorted_values: list[int], p: float) -> int:
    """Linear-interpolation percentile. `p` is in [0, 1]. Returns an
    integer for stable API output."""
    if not sorted_values:
        return 0
    if len(sorted_values) == 1:
        return sorted_values[0]
    rank = p * (len(sorted_values) - 1)
    lo = math.floor(rank)
    hi = math.ceil(rank)
    if lo == hi:
        return sorted_values[lo]
    frac = rank - lo
    value = sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * frac
    return int(round(value))


def compute_stats(scores: list[int]) -> dict:
    """Compute the stats payload from a list of integer scores.
    Returns a dict matching StatsOut. Empty input returns count=0."""
    if not scores:
        return {"median": 0, "p25": 0, "p75": 0, "count": 0}
    s = sorted(scores)
    return {
        "median": percentile(s, 0.5),
        "p25": percentile(s, 0.25),
        "p75": percentile(s, 0.75),
        "count": len(s),
    }


# ----------------------------------------------------------------------------
# App
# ----------------------------------------------------------------------------

app = FastAPI(
    title="Ballpark Stats",
    version="0.1.0",
    description="Anonymous aggregate stats for the Close Enough iOS app.",
)


@app.on_event("startup")
def _startup() -> None:
    init_db()


@app.get("/")
def health() -> dict:
    """Liveness check for Railway / monitoring."""
    return {"ok": True, "service": "ballpark-stats"}


@app.post("/round", status_code=204)
def post_round(payload: RoundIn) -> Response:
    """Record a played round. Re-posts for the same (device_id,
    puzzle_number) overwrite the prior score — handles re-plays
    after a 'clear today' debug tap.

    No PII stored: device_id is identifierForVendor (resets on
    reinstall), no name, email, contacts, location, or ad id."""
    with get_db() as conn:
        # Upsert: insert, on conflict update.
        conn.execute(
            """
            INSERT INTO rounds (device_id, puzzle_number, total_score, played_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(device_id, puzzle_number) DO UPDATE SET
                total_score = excluded.total_score,
                played_at = excluded.played_at
            """,
            (payload.device_id, payload.puzzle_number, payload.total_score, payload.played_at),
        )
    return Response(status_code=204)


@app.get("/stats/{puzzle_number}", response_model=StatsOut)
def get_stats(puzzle_number: int) -> StatsOut:
    """Return aggregate stats for a puzzle. Returns count=0 if
    no data exists yet (the iOS app treats that as 'no line on
    the share card')."""
    if puzzle_number <= 0:
        raise HTTPException(status_code=400, detail="puzzle_number must be > 0")
    with get_db() as conn:
        rows = conn.execute(
            "SELECT total_score FROM rounds WHERE puzzle_number = ?",
            (puzzle_number,),
        ).fetchall()
    scores = [row["total_score"] for row in rows]
    payload = compute_stats(scores)
    return StatsOut(puzzle_number=puzzle_number, **payload)


@app.delete("/me/{device_id}", status_code=204)
def delete_device(device_id: str) -> Response:
    """Purge all rows for a device. Wired up from a 'Delete my data'
    button in the iOS app's settings screen."""
    if not (1 <= len(device_id) <= 64):
        raise HTTPException(status_code=400, detail="invalid device_id")
    allowed = set(
        "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_."
    )
    if not all(c in allowed for c in device_id):
        raise HTTPException(status_code=400, detail="invalid device_id characters")
    with get_db() as conn:
        conn.execute("DELETE FROM rounds WHERE device_id = ?", (device_id,))
    return Response(status_code=204)


@app.get("/privacy", response_class=HTMLResponse)
def privacy_page() -> str:
    """Public-facing privacy disclosure. Required for App Store
    review. Plain HTML, no external assets, safe to be cached."""
    return """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Ballpark Stats — Privacy</title>
<style>
  body { font-family: -apple-system, BlinkMacSystemFont, sans-serif;
         max-width: 640px; margin: 48px auto; padding: 0 24px;
         color: #1a1a1a; line-height: 1.6; }
  h1 { font-size: 28px; margin-bottom: 0.5em; }
  h2 { font-size: 18px; margin-top: 1.5em; }
  ul { padding-left: 1.4em; }
  code { background: #f4f4f4; padding: 2px 6px; border-radius: 3px;
         font-size: 14px; }
</style>
</head>
<body>
<h1>Ballpark Stats — Privacy</h1>

<p>Close Enough collects anonymous puzzle scores to power a
&ldquo;you scored higher than X% of today&rsquo;s players&rdquo; line
on share cards.</p>

<h2>What we collect</h2>
<ul>
  <li>The puzzle number you played.</li>
  <li>Your final score.</li>
  <li>The date you played.</li>
  <li>An anonymous device identifier (Apple&rsquo;s <code>identifierForVendor</code>,
      which resets if you reinstall the app).</li>
</ul>

<h2>What we do NOT collect</h2>
<ul>
  <li>Your name, email, phone number.</li>
  <li>Your contacts, location, or photos.</li>
  <li>Your advertising identifier.</li>
  <li>Any other personally identifying information.</li>
</ul>

<h2>How to opt out</h2>
<p>In the Close Enough app, go to <strong>Settings &rarr; Privacy</strong>
and toggle &ldquo;Share anonymous stats&rdquo; off. The app will stop
sending data immediately.</p>

<h2>How to delete your data</h2>
<p>The same Settings &rarr; Privacy screen has a
&ldquo;Delete my data&rdquo; button. Tapping it removes every row
we&rsquo;ve stored for your device identifier.</p>

<h2>Retention</h2>
<p>Data is retained for {retain} days, then deleted automatically.
The exact retention period is configurable in the server&rsquo;s
environment.</p>

<p>Questions? Open an issue on the iOS app&rsquo;s GitHub repository.</p>
</body>
</html>""".replace("{retain}", str(MAX_RETAINED_DAYS))


# ----------------------------------------------------------------------------
# Retention cleanup (called on each request, cheap)
# ----------------------------------------------------------------------------


@app.middleware("http")
async def maybe_cleanup_old_data(request: Request, call_next):
    """Best-effort retention sweep. Runs at most once per ~1000
    requests to keep the cost negligible. Railway free tier
    doesn't have a cron, so this is the cleanest way to do it."""
    import random

    if random.random() < 0.001:  # ~0.1% of requests
        try:
            with get_db() as conn:
                conn.execute(
                    """
                    DELETE FROM rounds
                    WHERE created_at < datetime('now', ?)
                    """,
                    (f"-{MAX_RETAINED_DAYS} days",),
                )
        except Exception:
            # Retention cleanup is best-effort. Don't break the
            # request if it fails.
            pass
    return await call_next(request)
