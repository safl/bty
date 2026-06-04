"""Tests for bty-web's session-cookie auth.

The cookie is a Starlette ``SessionMiddleware``-signed payload; we don't try to
decode it. Tests exercise the visible behaviour: ``/ui/login`` gates mutation
routes on the admin password (``$BTY_ADMIN_PASSWORD``), the cookie carries
authed state across requests, missing/wrong cookies 401, and an instance with
no password configured leaves the UI open.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from bty.web._app import create_app

TEST_SERVICE_USER = "auth-test-user"
TEST_SECRET_KEY = "test-secret-not-for-prod-use"
TEST_PASSWORD = "test-admin-pw"


def _make_client(tmp_path: Path) -> TestClient:
    state = tmp_path / "state.db"
    image_root = tmp_path / "images"
    image_root.mkdir()
    boot_root = tmp_path / "boot"
    boot_root.mkdir()
    app = create_app(
        state_path=state,
        service_user=TEST_SERVICE_USER,
        secret_key=TEST_SECRET_KEY,
        image_root=image_root,
        boot_root=boot_root,
    )
    return TestClient(app)


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """A gated instance: BTY_ADMIN_PASSWORD is set, so /ui requires login."""
    monkeypatch.setenv("BTY_ADMIN_PASSWORD", TEST_PASSWORD)
    with _make_client(tmp_path) as c:
        yield c


# ---------- /ui/login ------------------------------------------------------


def test_login_with_valid_password_sets_session_cookie(client: TestClient) -> None:
    """The configured password flips the session and sets ``bty-token``."""
    r = client.post("/ui/login", data={"password": TEST_PASSWORD}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers.get("location") == "/ui/dashboard"
    assert r.cookies.get("bty-token") is not None


def test_login_with_invalid_password_returns_form_with_error(client: TestClient) -> None:
    """A wrong password re-renders the login form; no session cookie is set."""
    r = client.post("/ui/login", data={"password": "wrong"}, follow_redirects=False)
    assert r.status_code == 200  # form re-rendered, not a redirect
    assert "Invalid password" in r.text
    assert r.cookies.get("bty-token") is None


def test_login_success_records_audit_event(client: TestClient) -> None:
    """Each successful login lands an ``auth.login.succeeded`` row."""
    r = client.post("/ui/login", data={"password": TEST_PASSWORD}, follow_redirects=False)
    assert r.status_code == 303
    cookie = r.cookies.get("bty-token")
    r = client.get(
        "/events",
        params={"kind": "auth.login.succeeded"},
        cookies={"bty-token": cookie} if cookie else None,
    )
    events = r.json()["events"]
    assert len(events) == 1
    row = events[0]
    assert row["subject_kind"] == "auth"
    assert row["subject_id"] == "operator"
    assert row["actor"] == "operator"


def test_login_failure_records_audit_event(client: TestClient) -> None:
    """Each failed login lands an ``auth.login.failed`` row."""
    client.post("/ui/login", data={"password": "wrong"}, follow_redirects=False)
    r = client.post("/ui/login", data={"password": TEST_PASSWORD}, follow_redirects=False)
    cookie = r.cookies.get("bty-token")
    r = client.get(
        "/events",
        params={"kind": "auth.login.failed"},
        cookies={"bty-token": cookie} if cookie else None,
    )
    events = r.json()["events"]
    assert len(events) == 1
    assert events[0]["subject_kind"] == "auth"
    assert events[0]["subject_id"] == "operator"


# ---------- session cookie auth --------------------------------------------


def test_authed_session_can_call_protected_routes(client: TestClient) -> None:
    client.post("/ui/login", data={"password": TEST_PASSWORD}, follow_redirects=False)
    assert client.get("/machines").status_code == 200


def test_missing_cookie_is_401(client: TestClient) -> None:
    assert client.get("/machines").status_code == 401


def test_unsigned_cookie_value_is_401(client: TestClient) -> None:
    """A garbage cookie fails the SessionMiddleware signature check; the auth
    dep then 401s. Same DB-less path as a missing cookie."""
    client.cookies.set("bty-token", "definitely-not-a-signed-payload")
    assert client.get("/machines").status_code == 401


# ---------- /ui/logout -----------------------------------------------------


def test_logout_clears_the_session(client: TestClient) -> None:
    client.post("/ui/login", data={"password": TEST_PASSWORD}, follow_redirects=False)
    assert client.get("/machines").status_code == 200
    r = client.post("/ui/logout", follow_redirects=False)
    assert r.status_code == 303
    assert client.get("/machines").status_code == 401


# ---------- open instance (no password configured) -------------------------


def test_open_instance_allows_protected_routes_without_login(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With BTY_ADMIN_PASSWORD unset the UI is open: protected routes answer
    without a session cookie (a startup warning is logged)."""
    monkeypatch.delenv("BTY_ADMIN_PASSWORD", raising=False)
    with _make_client(tmp_path) as c:
        assert c.get("/machines").status_code == 200
