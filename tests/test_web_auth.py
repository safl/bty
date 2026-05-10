"""Tests for bty-web's session-cookie auth.

The cookie is a Starlette ``SessionMiddleware``-signed payload; we
don't try to decode it. Tests exercise the visible behaviour: the
``/ui/login`` form gates mutation routes on PAM, the cookie carries
authed state across requests, and missing/wrong cookies 401.

PAM never actually runs in tests - we monkeypatch
``pamela.authenticate`` per-test.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from bty.web._app import create_app

TEST_SERVICE_USER = "auth-test-user"
TEST_SECRET_KEY = "test-secret-not-for-prod-use"


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
        secret_key=TEST_SECRET_KEY,
        image_root=image_root,
        boot_root=boot_root,
    )
    with TestClient(app) as c:
        yield c


# ---------- /ui/login ------------------------------------------------------


def test_login_with_valid_password_sets_session_cookie(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A valid password POST to ``/ui/login`` PAM-checks, flips the
    session, and sets the ``bty-token`` cookie on the redirect."""
    import pamela

    monkeypatch.setattr(pamela, "authenticate", lambda *a, **kw: True)

    r = client.post(
        "/ui/login",
        data={"password": "right-one"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers.get("location") == "/ui/dashboard"
    assert r.cookies.get("bty-token") is not None


def test_login_with_invalid_password_returns_form_with_error(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failing PAM check re-renders the login form with an error;
    no session cookie is set."""
    import pamela

    def _reject(*_a: object, **_kw: object) -> None:
        raise pamela.PAMError("bad password")

    monkeypatch.setattr(pamela, "authenticate", _reject)

    r = client.post(
        "/ui/login",
        data={"password": "wrong"},
        follow_redirects=False,
    )
    assert r.status_code == 200  # form re-rendered, not a redirect
    assert "Invalid password" in r.text
    # No session was set; the cookie response is empty / unset.
    assert r.cookies.get("bty-token") is None


def test_login_success_records_audit_event(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each successful login lands an ``auth.login.succeeded`` row
    in the audit log so the operator can see session boundaries
    in /ui/events."""
    import pamela

    monkeypatch.setattr(pamela, "authenticate", lambda *a, **kw: True)
    r = client.post("/ui/login", data={"password": "ok"}, follow_redirects=False)
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
    assert row["subject_id"] == TEST_SERVICE_USER
    assert row["actor"] == TEST_SERVICE_USER


def test_login_failure_records_audit_event(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each failed PAM check lands an ``auth.login.failed`` row.
    Operators want to see brute-force attempts in /ui/events."""
    import pamela

    def _reject(*_a: object, **_kw: object) -> None:
        raise pamela.PAMError("bad password")

    monkeypatch.setattr(pamela, "authenticate", _reject)
    client.post("/ui/login", data={"password": "wrong"}, follow_redirects=False)

    # Now log in successfully to read /events.
    monkeypatch.setattr(pamela, "authenticate", lambda *a, **kw: True)
    r = client.post("/ui/login", data={"password": "ok"}, follow_redirects=False)
    cookie = r.cookies.get("bty-token")
    r = client.get(
        "/events",
        params={"kind": "auth.login.failed"},
        cookies={"bty-token": cookie} if cookie else None,
    )
    events = r.json()["events"]
    assert len(events) == 1
    assert events[0]["subject_kind"] == "auth"
    assert events[0]["subject_id"] == TEST_SERVICE_USER


# ---------- session cookie auth --------------------------------------------


def test_authed_session_can_call_protected_routes(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After ``/ui/login`` the cookie sticks on the TestClient and
    protected routes return 200."""
    import pamela

    monkeypatch.setattr(pamela, "authenticate", lambda *a, **kw: True)

    client.post("/ui/login", data={"password": "x"}, follow_redirects=False)
    r = client.get("/machines")
    assert r.status_code == 200


def test_missing_cookie_is_401(client: TestClient) -> None:
    r = client.get("/machines")
    assert r.status_code == 401


def test_unsigned_cookie_value_is_401(client: TestClient) -> None:
    """A garbage cookie value fails the SessionMiddleware signature
    check; SessionMiddleware drops it, request.session is empty, and
    the auth dep 401s. Same DB-less lookup path as a missing cookie."""
    client.cookies.set("bty-token", "definitely-not-a-signed-payload")
    r = client.get("/machines")
    assert r.status_code == 401


# ---------- /ui/logout -----------------------------------------------------


def test_logout_clears_the_session(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """``POST /ui/logout`` empties the session; subsequent requests
    that previously authed now 401."""
    import pamela

    monkeypatch.setattr(pamela, "authenticate", lambda *a, **kw: True)

    client.post("/ui/login", data={"password": "x"}, follow_redirects=False)
    assert client.get("/machines").status_code == 200

    r = client.post("/ui/logout", follow_redirects=False)
    # SessionMiddleware deletes the cookie via Set-Cookie on the redirect.
    assert r.status_code == 303
    assert client.get("/machines").status_code == 401
