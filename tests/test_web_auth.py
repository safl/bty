"""Tests for bty-web's PAM-backed auth flow.

PAM never actually runs in tests - we monkeypatch
``pamela.authenticate`` per-test so login outcomes are deterministic.
The session DB is real; tests cover token issuance, header/cookie
parity, expiry, and revocation.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pamela
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


# ---------- /auth/login -----------------------------------------------------


def test_login_with_valid_password_returns_token(client: TestClient) -> None:
    with patch("pamela.authenticate", return_value=True) as mock_pam:
        r = client.post("/auth/login", json={"password": "hunter2"})
    assert r.status_code == 200
    body = r.json()
    assert "token" in body and len(body["token"]) > 20
    assert "expires_at" in body
    # PAM was called with the service user the app was built with -
    # not anything from the request body.
    mock_pam.assert_called_once()
    args, kwargs = mock_pam.call_args
    user_arg = args[0] if args else kwargs.get("username")
    assert user_arg == TEST_SERVICE_USER


def test_login_with_invalid_password_is_401(client: TestClient) -> None:
    with patch("pamela.authenticate", side_effect=pamela.PAMError("bad")):
        r = client.post("/auth/login", json={"password": "wrong"})
    assert r.status_code == 401
    assert r.headers.get("www-authenticate", "").startswith("Bearer")


def test_login_label_is_persisted_on_session(client: TestClient) -> None:
    state = client.__dict__["_bty_state_path"]
    with patch("pamela.authenticate", return_value=True):
        r = client.post(
            "/auth/login",
            json={"password": "hunter2", "label": "alice@laptop"},
        )
    assert r.status_code == 200
    with open_db(state) as conn:
        row = conn.execute("SELECT label FROM sessions").fetchone()
    assert row["label"] == "alice@laptop"


# ---------- bearer / cookie parity ------------------------------------------


def test_seeded_session_authenticates_via_bearer(client: TestClient) -> None:
    with open_db(client.__dict__["_bty_state_path"]) as conn:
        token, _ = issue_session(conn, label="pytest")
    r = client.get("/machines", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200


def test_seeded_session_authenticates_via_cookie(client: TestClient) -> None:
    with open_db(client.__dict__["_bty_state_path"]) as conn:
        token, _ = issue_session(conn, label="pytest")
    client.cookies.set(SESSION_COOKIE, token)
    r = client.get("/machines")
    assert r.status_code == 200


def test_unknown_bearer_is_401(client: TestClient) -> None:
    r = client.get("/machines", headers={"Authorization": "Bearer never-issued"})
    assert r.status_code == 401


# ---------- expiry ----------------------------------------------------------


def test_expired_session_is_rejected(client: TestClient) -> None:
    """A session whose ``expires_at`` is in the past authenticates as 401."""
    state = client.__dict__["_bty_state_path"]
    with open_db(state) as conn:
        token, _ = issue_session(conn, label="will-expire")
        # Force the row's expiry into the past. The auth dependency
        # filters on expires_at > now() so this row is invisible.
        past = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
        conn.execute(
            "UPDATE sessions SET expires_at = ? WHERE token_hash = ?",
            (past, _sha256(token)),
        )
        conn.commit()
    r = client.get("/machines", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401


# ---------- /auth/logout + revocation --------------------------------------


def test_logout_revokes_the_presenting_token(client: TestClient) -> None:
    with open_db(client.__dict__["_bty_state_path"]) as conn:
        token, _ = issue_session(conn, label="will-logout")
    auth = {"Authorization": f"Bearer {token}"}
    assert client.get("/machines", headers=auth).status_code == 200
    r = client.post("/auth/logout", headers=auth)
    assert r.status_code == 204
    # Same token now 401s.
    assert client.get("/machines", headers=auth).status_code == 401


def test_logout_without_token_is_401(client: TestClient) -> None:
    r = client.post("/auth/logout")
    assert r.status_code == 401


def test_revoke_all_sessions_kills_every_active_token(client: TestClient) -> None:
    state = client.__dict__["_bty_state_path"]
    with open_db(state) as conn:
        token_a, _ = issue_session(conn, label="a")
        token_b, _ = issue_session(conn, label="b")
    auth_a = {"Authorization": f"Bearer {token_a}"}
    auth_b = {"Authorization": f"Bearer {token_b}"}
    assert client.get("/machines", headers=auth_a).status_code == 200
    assert client.get("/machines", headers=auth_b).status_code == 200
    with open_db(state) as conn:
        count = revoke_all_sessions(conn)
    assert count == 2
    assert client.get("/machines", headers=auth_a).status_code == 401
    assert client.get("/machines", headers=auth_b).status_code == 401


def _sha256(token: str) -> str:
    import hashlib

    return hashlib.sha256(token.encode("utf-8")).hexdigest()
