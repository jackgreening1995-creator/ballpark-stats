"""
Ballpark Stats — anonymous aggregate stats server.

Powers the "you scored higher than X% of today's players" line on
share cards, and the daily friend-duel flow. One file, FastAPI + SQLite.

Endpoints:
- POST /round — record a played round. No PII beyond a device id.
  Optional `guesses` field stores per-question guesses for the
  call-your-shot mechanic (C2).
- GET /stats/{puzzle_number} — median, p25, p75, count for a puzzle.
- GET /question-aggregate/{puzzle_number}/{question_index} — median
  and count of a single question's guesses. Powers the
  call-your-shot chip on the iOS round.
- POST /duel — create a duel from a finished round. Returns a token.
- GET /duel/{duel_token} — fetch a duel's state.
- POST /duel/{duel_token}/join — claim the joiner slot.
- POST /duel/{duel_token}/result — submit a player's result; flips
  the duel to 'completed' when both sides have scores.
- GET /duels/open/{device_id} — open duels for a device (polled on
  app open + scenePhase active).
- GET /privacy — human-readable privacy disclosure.
- DELETE /me/{device_id} — purge all rows for a device.
- GET / — health check (returns 200).

Deploy:
  uvicorn main:app --host 0.0.0.0 --port $PORT
"""

import math
import os
import secrets
import sqlite3
import string
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
# Open duels older than this get expired by the retention sweep.
# 7 days matches the iOS app's expectation (a daily puzzle's duel is
# stale within ~48h, but we keep a week of headroom for slow friends).
DUEL_EXPIRY_DAYS = int(os.environ.get("BALLPARK_DUEL_EXPIRY_DAYS", "7"))
DUEL_TOKEN_ALPHABET = string.ascii_letters + string.digits
DUEL_TOKEN_LEN = 12


def new_duel_token() -> str:
    """Random 12-char alphanumeric duel token. ~62^12 ≈ 3e21 space —
    no collision check needed at this scale."""
    return "".join(secrets.choice(DUEL_TOKEN_ALPHABET) for _ in range(DUEL_TOKEN_LEN))


def _clean_device_id(v: str) -> str:
    """Shared validator for device_id fields. Restricts to URL-safe
    characters. identifierForVendor is a UUID, but be defensive —
    the duel endpoints accept the same id space as /round."""
    allowed = set(
        "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_."
    )
    if not v or len(v) > 64 or not all(c in allowed for c in v):
        raise ValueError("device_id contains invalid characters")
    return v

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
    """Create the rounds + question_guesses tables if they don't
    exist. Called on app startup. Idempotent."""
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
        # Per-question guesses, indexed by (puzzle_number, question_index)
        # for fast median lookups. question_index is 0-based position in
        # the daily round. guess is the player's slider value (a positive
        # Double, log-space). Nullable because legacy rounds (pre-C2) had
        # no per-question data.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS question_guesses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id TEXT NOT NULL,
                puzzle_number INTEGER NOT NULL,
                question_index INTEGER NOT NULL,
                guess REAL NOT NULL,
                played_at TEXT NOT NULL,
                UNIQUE(device_id, puzzle_number, question_index)
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_qg_puzzle_q ON question_guesses(puzzle_number, question_index)"
        )
        # Duels (Phase D1). One row per duel. The initiator creates a
        # duel after finishing their round; the joiner claims the slot
        # by opening the share link. Both sides submit their score +
        # grid; when both are present, status flips to 'completed'.
        # No PII: device_id is identifierForVendor, name is whatever
        # the sender typed (defaults to a random Adjective Noun on the
        # client). Grid is a JSON array of color-tier ints (0..3).
        # initiator_device_id is nullable so /me can blank a deleted
        # user out of a duel without breaking the NOT NULL constraint
        # — the joiner sees the duel as 'expired' in that case.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS duels (
                duel_token TEXT PRIMARY KEY,
                puzzle_number INTEGER NOT NULL,
                theme_name TEXT,
                initiator_device_id TEXT,
                initiator_name TEXT,
                initiator_score INTEGER,
                initiator_grid TEXT,
                initiator_played_at TEXT,
                joiner_device_id TEXT,
                joiner_score INTEGER,
                joiner_grid TEXT,
                joiner_played_at TEXT,
                status TEXT NOT NULL DEFAULT 'open',
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_duels_initiator ON duels(initiator_device_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_duels_joiner ON duels(joiner_device_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_duels_status ON duels(status, created_at)"
        )


# ----------------------------------------------------------------------------
# Models
# ----------------------------------------------------------------------------


class RoundIn(BaseModel):
    puzzle_number: int = Field(..., gt=0, le=100_000)
    total_score: int = Field(..., ge=0, le=1_500)  # 1 question (0-1000) + call-shot bonus headroom
    device_id: str = Field(..., min_length=1, max_length=64)
    played_at: str = Field(..., min_length=10, max_length=40)
    # Optional per-question guesses. When present, length should equal
    # the round's question count (currently 10). Nulls inside the list
    # represent "didn't touch the scrubber" for that question and are
    # dropped from aggregates. Used by the C2 call-your-shot mechanic.
    guesses: Optional[list[Optional[float]]] = Field(default=None, max_length=20)

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

    @field_validator("guesses")
    @classmethod
    def _guesses_finite(cls, v):
        if v is None:
            return v
        cleaned = []
        for g in v:
            if g is None:
                cleaned.append(None)
            elif isinstance(g, (int, float)) and math.isfinite(g) and g > 0:
                cleaned.append(float(g))
            else:
                # Bad entry — drop it. The iOS app should never send
                # non-positive or non-finite guesses (the slider's
                # value is always positive in log space), so a hit
                # here is a client bug. We silently skip rather than
                # 400 the whole request because losing one guess is
                # less bad than losing the whole round.
                cleaned.append(None)
        return cleaned


class StatsOut(BaseModel):
    puzzle_number: int
    median: int
    p25: int
    p75: int
    count: int


class QuestionAggregateOut(BaseModel):
    puzzle_number: int
    question_index: int
    median: float
    count: int


# --- Duel models (Phase D1) -------------------------------------------------


class DuelCreateIn(BaseModel):
    """Initiator creates a duel after finishing their round. The
    share URL the iOS app builds is `https://ballpark.app/d?k=<token>`
    where <token> is returned here."""

    puzzle_number: int = Field(..., gt=0, le=100_000)
    theme_name: Optional[str] = Field(default=None, max_length=64)
    initiator_device_id: str = Field(..., min_length=1, max_length=64)
    initiator_name: Optional[str] = Field(default=None, max_length=64)
    initiator_score: int = Field(..., ge=0, le=1_500)  # 1 question (0-1000) + call-shot bonus
    # JSON-encoded array of color-tier ints (0..3), one per question.
    # Length cap is generous — rounds are currently 10 questions but
    # we don't want to break if that changes.
    initiator_grid: str = Field(default="[]", max_length=512)
    initiator_played_at: str = Field(..., min_length=10, max_length=40)

    @field_validator("initiator_device_id")
    @classmethod
    def _init_dev(cls, v: str) -> str:
        return _clean_device_id(v)

    @field_validator("initiator_played_at")
    @classmethod
    def _init_played(cls, v: str) -> str:
        try:
            datetime.fromisoformat(v.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            raise ValueError("initiator_played_at must be ISO 8601")
        return v


class DuelJoinIn(BaseModel):
    """Joiner claims the duel. device_id must differ from the
    initiator's (you can't duel yourself)."""

    device_id: str = Field(..., min_length=1, max_length=64)

    @field_validator("device_id")
    @classmethod
    def _join_dev(cls, v: str) -> str:
        return _clean_device_id(v)


class DuelResultIn(BaseModel):
    """Either side submits their score + grid. Updates whichever side
    matches device_id. If both sides now have scores, status flips
    to 'completed'."""

    device_id: str = Field(..., min_length=1, max_length=64)
    score: int = Field(..., ge=0, le=1_500)  # 1 question (0-1000) + call-shot bonus
    grid: str = Field(default="[]", max_length=512)
    played_at: str = Field(..., min_length=10, max_length=40)

    @field_validator("device_id")
    @classmethod
    def _res_dev(cls, v: str) -> str:
        return _clean_device_id(v)

    @field_validator("played_at")
    @classmethod
    def _res_played(cls, v: str) -> str:
        try:
            datetime.fromisoformat(v.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            raise ValueError("played_at must be ISO 8601")
        return v


class DuelOut(BaseModel):
    duel_token: str
    puzzle_number: int
    theme_name: Optional[str] = None
    initiator_device_id: Optional[str] = None
    initiator_name: Optional[str] = None
    initiator_score: Optional[int] = None
    initiator_grid: Optional[str] = None
    initiator_played_at: Optional[str] = None
    joiner_device_id: Optional[str] = None
    joiner_score: Optional[int] = None
    joiner_grid: Optional[str] = None
    joiner_played_at: Optional[str] = None
    status: str
    created_at: str


def row_to_duel(row: sqlite3.Row) -> DuelOut:
    return DuelOut(
        duel_token=row["duel_token"],
        puzzle_number=row["puzzle_number"],
        theme_name=row["theme_name"],
        initiator_device_id=row["initiator_device_id"],
        initiator_name=row["initiator_name"],
        initiator_score=row["initiator_score"],
        initiator_grid=row["initiator_grid"],
        initiator_played_at=row["initiator_played_at"],
        joiner_device_id=row["joiner_device_id"],
        joiner_score=row["joiner_score"],
        joiner_grid=row["joiner_grid"],
        joiner_played_at=row["joiner_played_at"],
        status=row["status"],
        created_at=row["created_at"],
    )


# ----------------------------------------------------------------------------
# Percentile helpers
# ----------------------------------------------------------------------------


def percentile(sorted_values, p: float):
    """Linear-interpolation percentile. `p` is in [0, 1]. Returns
    int for int inputs, float for float inputs (so the same helper
    works for total-score (int) and per-question guesses (float))."""
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
    if isinstance(sorted_values[0], int):
        return int(round(value))
    return value


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


def compute_question_aggregate(guesses: list[float]) -> dict:
    """Median and count of a single question's guesses. Empty input
    returns count=0 (the iOS app treats that as 'no median, hide the
    call-your-shot chip').

    Returns floats because guesses are slider values in log space —
    the answer to a per-question 'median' is rarely an integer.
    """
    if not guesses:
        return {"median": 0.0, "count": 0}
    s = sorted(guesses)
    return {
        "median": percentile(s, 0.5),
        "count": len(s),
    }


# ----------------------------------------------------------------------------
# App
# ----------------------------------------------------------------------------

app = FastAPI(
    title="Ballpark Stats",
    version="0.2.0",
    description="Anonymous aggregate stats + daily friend duels for the Close Enough iOS app.",
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

    If `guesses` is present, also upserts one row per question
    into question_guesses. Powers the call-your-shot chip on the
    next time the player (or anyone else) plays the same puzzle.

    No PII stored: device_id is identifierForVendor (resets on
    reinstall), no name, email, contacts, location, or ad id."""
    with get_db() as conn:
        # Upsert the round summary.
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
        # Per-question guesses. Replay = overwrite (same as round summary).
        if payload.guesses:
            for q_index, guess in enumerate(payload.guesses):
                if guess is None:
                    continue
                conn.execute(
                    """
                    INSERT INTO question_guesses
                        (device_id, puzzle_number, question_index, guess, played_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(device_id, puzzle_number, question_index) DO UPDATE SET
                        guess = excluded.guess,
                        played_at = excluded.played_at
                    """,
                    (payload.device_id, payload.puzzle_number, q_index, guess, payload.played_at),
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


@app.get(
    "/question-aggregate/{puzzle_number}/{question_index}",
    response_model=QuestionAggregateOut,
)
def get_question_aggregate(puzzle_number: int, question_index: int) -> QuestionAggregateOut:
    """Median guess and count for a single (puzzle, question) pair.
    Powers the call-your-shot chip on the iOS round.

    Returns count=0 if no data exists yet (iOS app treats that as
    'no median available, hide the chip' so the round plays as
    pre-C2)."""
    if puzzle_number <= 0:
        raise HTTPException(status_code=400, detail="puzzle_number must be > 0")
    if question_index < 0 or question_index >= 20:
        raise HTTPException(status_code=400, detail="question_index out of range")
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT guess FROM question_guesses
            WHERE puzzle_number = ? AND question_index = ?
            """,
            (puzzle_number, question_index),
        ).fetchall()
    guesses = [row["guess"] for row in rows]
    payload = compute_question_aggregate(guesses)
    return QuestionAggregateOut(
        puzzle_number=puzzle_number,
        question_index=question_index,
        **payload,
    )


# ----------------------------------------------------------------------------
# Duels (Phase D1)
# ----------------------------------------------------------------------------


@app.post("/duel", response_model=DuelOut, status_code=201)
def create_duel(payload: DuelCreateIn) -> DuelOut:
    """Initiator creates a duel after finishing their round. Returns
    the full duel row (including the token the iOS app uses to build
    the share URL `https://ballpark.app/d?k=<token>`).

    No PII: device_id is identifierForVendor, name is a client-side
    random 'Adjective Noun' pair (or whatever the player typed)."""
    token = new_duel_token()
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO duels (
                duel_token, puzzle_number, theme_name,
                initiator_device_id, initiator_name, initiator_score,
                initiator_grid, initiator_played_at, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open')
            """,
            (
                token,
                payload.puzzle_number,
                payload.theme_name,
                payload.initiator_device_id,
                payload.initiator_name,
                payload.initiator_score,
                payload.initiator_grid,
                payload.initiator_played_at,
            ),
        )
        row = conn.execute(
            "SELECT * FROM duels WHERE duel_token = ?", (token,)
        ).fetchone()
    return row_to_duel(row)


@app.get("/duel/{duel_token}", response_model=DuelOut)
def get_duel(duel_token: str) -> DuelOut:
    """Fetch a duel's current state. Used by both sides to poll for
    results (since APNs is deferred, the iOS app polls this on
    scenePhase becoming active)."""
    if len(duel_token) != DUEL_TOKEN_LEN or not all(
        c in DUEL_TOKEN_ALPHABET for c in duel_token
    ):
        raise HTTPException(status_code=400, detail="invalid duel_token")
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM duels WHERE duel_token = ?", (duel_token,)
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="duel not found")
    return row_to_duel(row)


@app.post("/duel/{duel_token}/join", response_model=DuelOut)
def join_duel(duel_token: str, payload: DuelJoinIn) -> DuelOut:
    """Claim the joiner slot of an open duel. Atomic: uses a single
    UPDATE with a WHERE guard so two simultaneous joiners can't both
    win. 409 if the duel is already claimed by a different device.
    404 if the duel doesn't exist. 400 if you try to join your own
    duel (initiator_device_id == payload.device_id)."""
    if len(duel_token) != DUEL_TOKEN_LEN or not all(
        c in DUEL_TOKEN_ALPHABET for c in duel_token
    ):
        raise HTTPException(status_code=400, detail="invalid duel_token")
    with get_db() as conn:
        # BEGIN IMMEDIATE acquires a write lock immediately so the
        # read-check-write below is atomic against other joiners.
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT * FROM duels WHERE duel_token = ?", (duel_token,)
            ).fetchone()
            if row is None:
                conn.execute("ROLLBACK")
                raise HTTPException(status_code=404, detail="duel not found")
            if row["status"] != "open":
                # Already completed/expired — but allow the existing
                # joiner to re-join (idempotent, useful if they tapped
                # the link twice or the app re-handled the URL).
                if row["joiner_device_id"] != payload.device_id:
                    conn.execute("ROLLBACK")
                    raise HTTPException(
                        status_code=409, detail="duel already claimed"
                    )
            elif row["initiator_device_id"] == payload.device_id:
                conn.execute("ROLLBACK")
                raise HTTPException(
                    status_code=400, detail="can't join your own duel"
                )
            elif (
                row["joiner_device_id"] is not None
                and row["joiner_device_id"] != payload.device_id
            ):
                # Open duel but someone else already claimed the slot.
                conn.execute("ROLLBACK")
                raise HTTPException(
                    status_code=409, detail="duel already claimed"
                )
            else:
                conn.execute(
                    """
                    UPDATE duels SET joiner_device_id = ?
                    WHERE duel_token = ? AND status = 'open'
                    """,
                    (payload.device_id, duel_token),
                )
            row = conn.execute(
                "SELECT * FROM duels WHERE duel_token = ?", (duel_token,)
            ).fetchone()
            conn.execute("COMMIT")
        except HTTPException:
            raise
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return row_to_duel(row)


@app.post("/duel/{duel_token}/result", response_model=DuelOut)
def submit_duel_result(duel_token: str, payload: DuelResultIn) -> DuelOut:
    """Submit a player's result. Updates whichever side matches
    device_id. If both sides now have scores, flips status to
    'completed'.

    The initiator normally has their score set at creation time, so
    this endpoint mostly handles the joiner's submission. But it
    handles either side symmetrically for robustness (e.g. initiator
    replays the round and resubmits)."""
    if len(duel_token) != DUEL_TOKEN_LEN or not all(
        c in DUEL_TOKEN_ALPHABET for c in duel_token
    ):
        raise HTTPException(status_code=400, detail="invalid duel_token")
    with get_db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT * FROM duels WHERE duel_token = ?", (duel_token,)
            ).fetchone()
            if row is None:
                conn.execute("ROLLBACK")
                raise HTTPException(status_code=404, detail="duel not found")
            if row["status"] not in ("open", "completed"):
                conn.execute("ROLLBACK")
                raise HTTPException(
                    status_code=409, detail=f"duel is {row['status']}"
                )
            is_initiator = row["initiator_device_id"] == payload.device_id
            is_joiner = row["joiner_device_id"] == payload.device_id
            if not (is_initiator or is_joiner):
                conn.execute("ROLLBACK")
                raise HTTPException(
                    status_code=403, detail="device is not in this duel"
                )
            if is_initiator:
                conn.execute(
                    """
                    UPDATE duels SET
                        initiator_score = ?,
                        initiator_grid = ?,
                        initiator_played_at = ?
                    WHERE duel_token = ?
                    """,
                    (payload.score, payload.grid, payload.played_at, duel_token),
                )
            if is_joiner:
                conn.execute(
                    """
                    UPDATE duels SET
                        joiner_score = ?,
                        joiner_grid = ?,
                        joiner_played_at = ?
                    WHERE duel_token = ?
                    """,
                    (payload.score, payload.grid, payload.played_at, duel_token),
                )
            # Flip to completed if both sides have scores.
            row = conn.execute(
                "SELECT * FROM duels WHERE duel_token = ?", (duel_token,)
            ).fetchone()
            if (
                row["initiator_score"] is not None
                and row["joiner_score"] is not None
                and row["status"] == "open"
            ):
                conn.execute(
                    "UPDATE duels SET status = 'completed' WHERE duel_token = ?",
                    (duel_token,),
                )
                row = conn.execute(
                    "SELECT * FROM duels WHERE duel_token = ?", (duel_token,)
                ).fetchone()
            conn.execute("COMMIT")
        except HTTPException:
            raise
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return row_to_duel(row)


@app.get("/duels/open/{device_id}", response_model=list[DuelOut])
def list_open_duels(device_id: str) -> list[DuelOut]:
    """Open (not-completed, not-expired) duels where this device is
    either initiator or joiner. Polled by the iOS app on app open and
    on scenePhase becoming active — this is the polling fallback for
    the deferred-APNs world. Includes duels the device hasn't joined
    yet (initiator-side, waiting for a friend) and duels waiting for
    this device to play (joiner-side, claimed but no result yet)."""
    try:
        _clean_device_id(device_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid device_id")
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT * FROM duels
            WHERE status = 'open'
              AND (initiator_device_id = ? OR joiner_device_id = ?)
              AND created_at >= datetime('now', ?)
            ORDER BY created_at DESC
            """,
            (device_id, device_id, f"-{DUEL_EXPIRY_DAYS} days"),
        ).fetchall()
    return [row_to_duel(r) for r in rows]


@app.delete("/me/{device_id}", status_code=204)
def delete_device(device_id: str) -> Response:
    """Purge all rows for a device. Wired up from a 'Delete my data'
    button in the iOS app's settings screen. Cascades to per-question
    guesses so the call-your-shot aggregate for this device is also
    removed. Also removes the device from any duel (initiator or
    joiner) — the duel row is kept so the other side still has a
    result, but this device's identity is blanked."""
    if not (1 <= len(device_id) <= 64):
        raise HTTPException(status_code=400, detail="invalid device_id")
    allowed = set(
        "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_."
    )
    if not all(c in allowed for c in device_id):
        raise HTTPException(status_code=400, detail="invalid device_id characters")
    with get_db() as conn:
        conn.execute("DELETE FROM rounds WHERE device_id = ?", (device_id,))
        conn.execute(
            "DELETE FROM question_guesses WHERE device_id = ?", (device_id,)
        )
        # Blank this device out of any duel it's in. We don't delete
        # the row because the other side may still want their result.
        # If the device was the initiator of an open duel, the duel is
        # effectively dead — flip to 'expired'.
        conn.execute(
            """
            UPDATE duels SET
                initiator_device_id = NULL,
                initiator_name = NULL,
                initiator_score = NULL,
                initiator_grid = NULL,
                initiator_played_at = NULL
            WHERE initiator_device_id = ?
            """,
            (device_id,),
        )
        conn.execute(
            """
            UPDATE duels SET
                joiner_device_id = NULL,
                joiner_score = NULL,
                joiner_grid = NULL,
                joiner_played_at = NULL
            WHERE joiner_device_id = ?
            """,
            (device_id,),
        )
        conn.execute(
            """
            UPDATE duels SET status = 'expired'
            WHERE status = 'open' AND initiator_device_id IS NULL
            """,
        )
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
  <li>Your per-question guesses (one number per question in the
      daily round). Used anonymously to power the
      &ldquo;call your shot&rdquo; mechanic.</li>
  <li>The date you played.</li>
  <li>An anonymous device identifier (Apple&rsquo;s <code>identifierForVendor</code>,
      which resets if you reinstall the app).</li>
  <li>When you start or join a duel: the duel token, your device id,
      the name you typed (optional — defaults to a random
      &ldquo;Adjective Noun&rdquo;), your score, and your color-tier
      grid. This is the minimum needed to show a head-to-head
      scorecard to the friend you duelled.</li>
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
                # Use SQLite's own datetime arithmetic so the cutoff
                # matches the stored `datetime('now')` format. This
                # avoids any timezone mismatch between Python's
                # datetime.now() and SQLite's UTC-less "now" string.
                conn.execute(
                    """
                    DELETE FROM rounds
                    WHERE created_at < datetime('now', ?)
                    """,
                    (f"-{MAX_RETAINED_DAYS} days",),
                )
                conn.execute(
                    """
                    DELETE FROM question_guesses
                    WHERE played_at < datetime('now', ?)
                    """,
                    (f"-{MAX_RETAINED_DAYS} days",),
                )
                # Expire open duels older than DUEL_EXPIRY_DAYS. We
                # flip to 'expired' rather than DELETE so a slow
                # joiner who opens the link after the cutoff gets a
                # clean 'this duel has expired' message instead of a
                # 404 (which would look like a typo).
                conn.execute(
                    """
                    UPDATE duels SET status = 'expired'
                    WHERE status = 'open'
                      AND created_at < datetime('now', ?)
                    """,
                    (f"-{DUEL_EXPIRY_DAYS} days",),
                )
        except Exception:
            # Retention cleanup is best-effort. Don't break the
            # request if it fails.
            pass
    return await call_next(request)
