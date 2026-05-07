"""Tests for bty-web's session-cookie auth.

PAM never actually runs in tests - we monkeypatch
``pamela.authenticate`` per-test where the login endpoint is
exercised. The session DB is real; tests cover token issuance,
cookie auth, expiry, and revocation.

The browser-flow ``POST /ui/login`` lives in ``tests/test_web_ui.py``;
this file focuses on the auth dependency itself: cookie present /
absent / wrong / expired, plus the ``revoke_all_sessions`` server
lever.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from bty.web._app import create_app
from bty.web._auth import SESSION_COOKIE
from bty.web._db import (
    issue_session,
    open_db,
    revoke_all_sessions,
)

TEST_SERVICE_USER = "auth-test-user"


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    state = tmp_path / "state.db"
    image_root = tmp_path / "images"
    image_root.mkdir()
    boot_root = tmp_path / "boot"
    boot_root.mkdir()
    app = create_app(
        state_path=state,
        service_user=TEST_SERVICE_USER,
        image_root=image_root,
        boot_root=boot_root,
    )
    with TestClient(app) as c:
        # Stash state path on the client so individual tests can poke
        # the DB (seed sessions, expire rows) without rebuilding the
        # fixture.
        c.__dict__["_bty_state_path"] = state
        yield c


# ---------- session cookie auth --------------------------------------------


def test_seeded_session_authenticates_via_cookie(client: TestClient) -> None:
    """A session row inserted directly in the DB authenticates a
    request whose ``bty-token`` cookie carries the matching token."""
    with open_db(client.__dict__["_bty_state_path"]) as conn:
        token, _ = issue_session(conn, label="pytest")
    client.cookies.set(SESSION_COOKIE, token)
    r = client.get("/machines")
    assert r.status_code == 200


def test_missing_cookie_is_401(client: TestClient) -> None:
    r = client.get("/machines")
    assert r.status_code == 401


def test_unknown_cookie_is_401(client: TestClient) -> None:
    """A cookie value that doesn't match any active session row 401s.
    Same DB lookup path as a missing cookie - no timing oracle."""
    client.cookies.set(SESSION_COOKIE, "never-issued-not-a-real-token")
    r = client.get("/machines")
    assert r.status_code == 401


# ---------- expiry ----------------------------------------------------------


def test_expired_session_is_rejected(client: TestClient) -> None:
    """A session whose ``expires_at`` is in the past 401s. The auth
    dep filters on ``expires_at > now()`` so an expired row is
    invisible to the lookup."""
    state = client.__dict__["_bty_state_path"]
    with open_db(state) as conn:
        token, _ = issue_session(conn, label="will-expire")
        past = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
        conn.execute(
            "UPDATE sessions SET expires_at = ? WHERE token_hash = ?",
            (past, _sha256(token)),
        )
        conn.commit()
    client.cookies.set(SESSION_COOKIE, token)
    r = client.get("/machines")
    assert r.status_code == 401


# ---------- revoke_all_sessions ---------------------------------------------


def test_revoke_all_sessions_kills_every_active_token(client: TestClient) -> None:
    """``revoke_all_sessions`` is the server-side "log everyone out"
    lever (used by the ``/ui/settings/revoke-sessions`` form). After
    truncation every previously-valid session cookie 401s."""
    state = client.__dict__["_bty_state_path"]
    with open_db(state) as conn:
        token_a, _ = issue_session(conn, label="a")
        token_b, _ = issue_session(conn, label="b")

    # Sanity: both authenticate before revoke.
    client.cookies.set(SESSION_COOKIE, token_a)
    assert client.get("/machines").status_code == 200
    client.cookies.set(SESSION_COOKIE, token_b)
    assert client.get("/machines").status_code == 200

    with open_db(state) as conn:
        count = revoke_all_sessions(conn)
    assert count == 2

    client.cookies.set(SESSION_COOKIE, token_a)
    assert client.get("/machines").status_code == 401
    client.cookies.set(SESSION_COOKIE, token_b)
    assert client.get("/machines").status_code == 401


def _sha256(token: str) -> str:
    import hashlib

    return hashlib.sha256(token.encode("utf-8")).hexdigest()
