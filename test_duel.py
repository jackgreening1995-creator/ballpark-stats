"""End-to-end tests for the duel flow (Phase D1).

Run: `python -m pytest test_duel.py` (or `python test_duel.py`).

Uses FastAPI's TestClient with a temp SQLite DB so it doesn't clobber
the live database. Covers the full duel lifecycle: create → join →
result → completed, plus edge cases (self-join, double-join, third-
device claim, non-participant result, /me purge, replay resubmit).
"""

import json
import os
import tempfile

from fastapi.testclient import TestClient

DEV_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
DEV_B = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
DEV_C = "cccccccc-cccc-cccc-cccc-cccccccccccc"


def _make_client():
    """Return a TestClient backed by a fresh temp DB."""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    os.environ["BALLPARK_DB"] = tmp.name
    from main import app  # import after env is set

    return TestClient(app), tmp.name


def test_full_duel_lifecycle():
    client, db_path = _make_client()
    try:
        with client:
            # Create
            r = client.post("/duel", json={
                "puzzle_number": 525, "theme_name": "SPACE",
                "initiator_device_id": DEV_A, "initiator_name": "Golden Moose",
                "initiator_score": 3420,
                "initiator_grid": json.dumps([0,1,2,3,0,1,2,3,0,1]),
                "initiator_played_at": "2026-06-18T12:00:00Z",
            })
            assert r.status_code == 201
            token = r.json()["duel_token"]
            assert r.json()["status"] == "open"

            # Self-join blocked
            assert client.post(
                f"/duel/{token}/join", json={"device_id": DEV_A}
            ).status_code == 400

            # B joins
            r = client.post(f"/duel/{token}/join", json={"device_id": DEV_B})
            assert r.status_code == 200
            assert r.json()["joiner_device_id"] == DEV_B

            # Idempotent re-join
            assert client.post(
                f"/duel/{token}/join", json={"device_id": DEV_B}
            ).status_code == 200

            # Third device blocked
            assert client.post(
                f"/duel/{token}/join", json={"device_id": DEV_C}
            ).status_code == 409

            # B submits result -> completed
            r = client.post(f"/duel/{token}/result", json={
                "device_id": DEV_B, "score": 2180, "grid": "[]",
                "played_at": "2026-06-18T12:30:00Z",
            })
            assert r.status_code == 200
            assert r.json()["status"] == "completed"
            assert r.json()["joiner_score"] == 2180

            # Non-participant blocked
            assert client.post(f"/duel/{token}/result", json={
                "device_id": DEV_C, "score": 9999, "grid": "[]",
                "played_at": "2026-06-18T13:00:00Z",
            }).status_code == 403
    finally:
        os.unlink(db_path)


def test_open_duels_list():
    client, db_path = _make_client()
    try:
        with client:
            # Completed duel should not appear in open list
            r = client.post("/duel", json={
                "puzzle_number": 525, "initiator_device_id": DEV_A,
                "initiator_score": 3420, "initiator_grid": "[]",
                "initiator_played_at": "2026-06-18T12:00:00Z",
            })
            token1 = r.json()["duel_token"]
            client.post(f"/duel/{token1}/join", json={"device_id": DEV_B})
            client.post(f"/duel/{token1}/result", json={
                "device_id": DEV_B, "score": 1000, "grid": "[]",
                "played_at": "2026-06-18T12:30:00Z",
            })
            assert client.get(f"/duels/open/{DEV_A}").json() == []

            # Open duel shows up
            r = client.post("/duel", json={
                "puzzle_number": 526, "initiator_device_id": DEV_A,
                "initiator_score": 1000, "initiator_grid": "[]",
                "initiator_played_at": "2026-06-18T14:00:00Z",
            })
            token2 = r.json()["duel_token"]
            open_duels = client.get(f"/duels/open/{DEV_A}").json()
            assert len(open_duels) == 1
            assert open_duels[0]["duel_token"] == token2
    finally:
        os.unlink(db_path)


def test_token_validation():
    client, db_path = _make_client()
    try:
        with client:
            assert client.get("/duel/short").status_code == 400
            assert client.get("/duel/AAAAAAAAAAAA").status_code == 404
            assert client.get("/duel/has!bad!chars").status_code == 400
    finally:
        os.unlink(db_path)


def test_bad_device_id_rejected():
    client, db_path = _make_client()
    try:
        with client:
            r = client.post("/duel", json={
                "puzzle_number": 1, "initiator_device_id": "has spaces!",
                "initiator_score": 0, "initiator_grid": "[]",
                "initiator_played_at": "2026-06-18T14:00:00Z",
            })
            assert r.status_code == 422
    finally:
        os.unlink(db_path)


def test_me_purge_expires_open_initiator_duel():
    """When the initiator of an open duel deletes their data, the
    duel flips to 'expired' so the joiner sees a clean message
    instead of a 404."""
    client, db_path = _make_client()
    try:
        with client:
            r = client.post("/duel", json={
                "puzzle_number": 526, "initiator_device_id": DEV_A,
                "initiator_score": 1000, "initiator_grid": "[]",
                "initiator_played_at": "2026-06-18T14:00:00Z",
            })
            token = r.json()["duel_token"]
            assert client.get(f"/duel/{token}").json()["status"] == "open"
            assert client.delete(f"/me/{DEV_A}").status_code == 204
            assert client.get(f"/duel/{token}").json()["status"] == "expired"
    finally:
        os.unlink(db_path)


def test_me_purge_blanks_completed_duel():
    """When a player deletes their data from a completed duel, their
    identity is blanked but the duel row stays so the other side can
    still see their own result."""
    client, db_path = _make_client()
    try:
        with client:
            r = client.post("/duel", json={
                "puzzle_number": 527, "initiator_device_id": DEV_A,
                "initiator_score": 1000, "initiator_grid": "[]",
                "initiator_played_at": "2026-06-18T15:00:00Z",
            })
            token = r.json()["duel_token"]
            client.post(f"/duel/{token}/join", json={"device_id": DEV_B})
            client.post(f"/duel/{token}/result", json={
                "device_id": DEV_B, "score": 500, "grid": "[]",
                "played_at": "2026-06-18T15:30:00Z",
            })
            assert client.delete(f"/me/{DEV_A}").status_code == 204
            d = client.get(f"/duel/{token}").json()
            assert d["initiator_device_id"] is None
            assert d["initiator_score"] is None
            # Joiner's data is intact
            assert d["joiner_device_id"] == DEV_B
            assert d["joiner_score"] == 500
    finally:
        os.unlink(db_path)


def test_initiator_resubmit_replay():
    """Initiator can resubmit their score (e.g. after replaying the
    round). The duel stays completed, score updates."""
    client, db_path = _make_client()
    try:
        with client:
            r = client.post("/duel", json={
                "puzzle_number": 528, "initiator_device_id": DEV_A,
                "initiator_score": 1000, "initiator_grid": "[]",
                "initiator_played_at": "2026-06-18T15:00:00Z",
            })
            token = r.json()["duel_token"]
            client.post(f"/duel/{token}/join", json={"device_id": DEV_B})
            client.post(f"/duel/{token}/result", json={
                "device_id": DEV_B, "score": 500, "grid": "[]",
                "played_at": "2026-06-18T15:30:00Z",
            })
            r = client.post(f"/duel/{token}/result", json={
                "device_id": DEV_A, "score": 1500, "grid": "[]",
                "played_at": "2026-06-18T15:00:00Z",
            })
            assert r.status_code == 200
            assert r.json()["initiator_score"] == 1500
            assert r.json()["status"] == "completed"
    finally:
        os.unlink(db_path)


if __name__ == "__main__":
    # Manual run without pytest
    import inspect
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and inspect.isfunction(fn):
            print(f"running {name}...", end=" ")
            fn()
            print("OK")
    print("\nALL TESTS PASSED")
