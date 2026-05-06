"""Tests for the bty-web browser UI.

Cookie-based auth flow, server-rendered pages via TestClient. The
fixture seeds an active session row directly (skipping PAM); tests
that exercise the login form mock ``pamela.authenticate`` per-test.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from bty.web._app import create_app
from bty.web._auth import SESSION_COOKIE
from bty.web._db import issue_session, open_db

TEST_SERVICE_USER = "ui-test-user"

# Mutated by the fixture so tests calling the API with
# ``headers=AUTH`` get the freshly-seeded session token.
AUTH: dict[str, str] = {}


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    image_root = tmp_path / "images"
    image_root.mkdir()
    (image_root / "demo.qcow2").write_bytes(b"\0" * 16)
    state = tmp_path / "state.db"
    app = create_app(
        state_path=state,
        service_user=TEST_SERVICE_USER,
        image_root=image_root,
    )
    with open_db(state) as conn:
        token, _ = issue_session(conn, label="ui-pytest")
    AUTH.clear()
    AUTH["Authorization"] = f"Bearer {token}"
    # ``follow_redirects=False`` so we can assert on 303 hops.
    try:
        with TestClient(app, follow_redirects=False) as c:
            # Stash the seeded token on the client so ``_login`` can
            # set the cookie without hitting /ui/login (which would
            # call PAM against the test runner's user).
            c.__dict__["_bty_session_token"] = token
            yield c
    finally:
        AUTH.clear()


def _login(client: TestClient) -> None:
    """Set the session cookie so authed UI pages render.

    We skip the real ``/ui/login`` POST so PAM never runs in tests;
    tests that need to exercise the login flow itself mock
    ``pamela.authenticate`` and call /ui/login explicitly.
    """
    token = client.__dict__["_bty_session_token"]
    client.cookies.set(SESSION_COOKIE, token)


# ---------- entry / redirects ----------------------------------------------


def test_ui_root_redirects_to_dashboard(client: TestClient) -> None:
    r = client.get("/ui")
    assert r.status_code == 303
    assert r.headers["location"] == "/ui/dashboard"


def test_ui_dashboard_without_cookie_redirects_to_login(client: TestClient) -> None:
    r = client.get("/ui/dashboard")
    assert r.status_code == 303
    assert r.headers["location"] == "/ui/login"


def test_ui_machines_without_cookie_redirects_to_login(client: TestClient) -> None:
    r = client.get("/ui/machines")
    assert r.status_code == 303
    assert r.headers["location"] == "/ui/login"


# ---------- login flow ------------------------------------------------------


def test_ui_login_form_renders(client: TestClient) -> None:
    r = client.get("/ui/login")
    assert r.status_code == 200
    assert "Log in" in r.text
    # Form prompts for the OS password of the service user; the
    # username is fixed at server-startup so it isn't a form field.
    assert 'name="password"' in r.text
    assert TEST_SERVICE_USER in r.text


def test_ui_login_invalid_password_re_renders_with_error(client: TestClient) -> None:
    from unittest.mock import patch

    import pamela

    with patch("pamela.authenticate", side_effect=pamela.PAMError("bad password")):
        r = client.post("/ui/login", data={"password": "wrong"})
    assert r.status_code == 200
    assert "Invalid password" in r.text
    assert SESSION_COOKIE not in client.cookies


def test_ui_login_valid_password_sets_cookie_and_redirects(client: TestClient) -> None:
    from unittest.mock import patch

    with patch("pamela.authenticate", return_value=True):
        r = client.post("/ui/login", data={"password": "hunter2"})
    assert r.status_code == 303
    assert r.headers["location"] == "/ui/dashboard"
    assert SESSION_COOKIE in client.cookies


def test_ui_logout_clears_cookie(client: TestClient) -> None:
    _login(client)
    r = client.post("/ui/logout")
    assert r.status_code == 303
    assert r.headers["location"] == "/ui/login"
    # The Set-Cookie header carries an empty value + Max-Age=0.
    set_cookie = r.headers.get("set-cookie", "")
    assert SESSION_COOKIE in set_cookie


# ---------- pages (auth'd) --------------------------------------------------


def test_ui_dashboard_renders_after_login(client: TestClient) -> None:
    _login(client)
    r = client.get("/ui/dashboard")
    assert r.status_code == 200
    assert "Dashboard" in r.text
    assert "Machines" in r.text


def test_ui_dashboard_subscribes_to_sse_for_live_counts(client: TestClient) -> None:
    """The counter cards need a ``sse-connect``/``sse-swap`` wrapper
    so the htmx-ext-sse client routes ``dashboard-counts`` events to
    them - that's what makes the dashboard a *dashboard* and not a
    snapshot."""
    _login(client)
    r = client.get("/ui/dashboard")
    assert r.status_code == 200
    body = r.text
    assert 'id="dashboard-counts"' in body
    assert 'sse-connect="/events/machines"' in body
    assert 'sse-swap="dashboard-counts"' in body


def test_ui_machines_lists_known_records(client: TestClient) -> None:
    _login(client)
    # Seed via the API.
    client.put(
        "/machines/aa:bb:cc:dd:ee:ff",
        json={"image": "demo.qcow2", "provisioning_mode": "none"},
        headers=AUTH,
    )
    r = client.get("/ui/machines")
    assert r.status_code == 200
    assert "aa:bb:cc:dd:ee:ff" in r.text
    assert "demo.qcow2" in r.text


def test_ui_machines_table_shows_discovered_badge(client: TestClient) -> None:
    _login(client)
    # Hitting /pxe/{mac} for an unknown MAC auto-discovers it.
    client.get("/pxe/11:22:33:44:55:66")
    r = client.get("/ui/machines")
    assert r.status_code == 200
    assert "11:22:33:44:55:66" in r.text
    assert "discovered" in r.text  # badge text


def test_ui_machine_detail_renders(client: TestClient) -> None:
    _login(client)
    client.put(
        "/machines/aa:bb:cc:dd:ee:ff",
        json={"image": "demo.qcow2", "provisioning_mode": "cloud-init"},
        headers=AUTH,
    )
    r = client.get("/ui/machines/aa:bb:cc:dd:ee:ff")
    assert r.status_code == 200
    assert "aa:bb:cc:dd:ee:ff" in r.text
    assert 'value="cloud-init" selected' in r.text or "cloud-init</option>" in r.text


def test_ui_machine_detail_404(client: TestClient) -> None:
    _login(client)
    r = client.get("/ui/machines/aa:bb:cc:dd:ee:00")
    assert r.status_code == 404


def test_ui_machine_upsert_via_form(client: TestClient) -> None:
    _login(client)
    r = client.post(
        "/ui/machines/aa:bb:cc:dd:ee:ff",
        data={
            "image": "demo.qcow2",
            "provisioning_mode": "none",
            "hostname": "bty-ui-test",
            "cijoe_workflow_ref": "",
        },
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/ui/machines"
    # The record landed.
    api = client.get(
        "/machines/aa:bb:cc:dd:ee:ff",
        headers=AUTH,
    )
    assert api.status_code == 200
    assert api.json()["image"] == "demo.qcow2"
    assert api.json()["hostname"] == "bty-ui-test"
    # Form omits boot_policy -> dependency default applies (local).
    assert api.json()["boot_policy"] == "local"


def test_ui_machine_upsert_persists_boot_policy_flash(client: TestClient) -> None:
    _login(client)
    r = client.post(
        "/ui/machines/aa:bb:cc:dd:ee:ff",
        data={
            "image": "demo.qcow2",
            "provisioning_mode": "none",
            "hostname": "",
            "cijoe_workflow_ref": "",
            "boot_policy": "flash",
        },
    )
    assert r.status_code == 303
    api = client.get(
        "/machines/aa:bb:cc:dd:ee:ff",
        headers=AUTH,
    )
    assert api.json()["boot_policy"] == "flash"


def test_ui_machine_upsert_rejects_unknown_boot_policy(client: TestClient) -> None:
    _login(client)
    r = client.post(
        "/ui/machines/aa:bb:cc:dd:ee:ff",
        data={
            "image": "demo.qcow2",
            "provisioning_mode": "none",
            "boot_policy": "yolo",
        },
    )
    assert r.status_code == 400
    assert "boot_policy" in r.text


def test_ui_machine_detail_renders_boot_policy_dropdown(client: TestClient) -> None:
    _login(client)
    client.put(
        "/machines/aa:bb:cc:dd:ee:ff",
        json={"image": "demo.qcow2", "boot_policy": "flash"},
        headers=AUTH,
    )
    r = client.get("/ui/machines/aa:bb:cc:dd:ee:ff")
    assert r.status_code == 200
    body = r.text
    assert 'name="boot_policy"' in body
    # Both options present, current value selected.
    assert ">local</option>" in body
    assert ">flash</option>" in body
    assert 'value="flash" selected' in body or 'flash" selected' in body


def test_ui_boot_page_renders_with_artifact_state(client: TestClient) -> None:
    """The /ui/boot page must show the configured boot dir and one
    row per expected artifact (vmlinuz/initrd/squashfs/sha256)."""
    _login(client)
    r = client.get("/ui/boot")
    assert r.status_code == 200
    body = r.text
    for name in (
        "bty-live-x86_64.vmlinuz",
        "bty-live-x86_64.initrd",
        "bty-live-x86_64.squashfs",
        "bty-live-x86_64.sha256",
    ):
        assert name in body, name
    # Empty boot dir => four "missing" badges (warning kind).
    assert body.count("missing</span>") == 4
    assert body.count('class="badge bg-warning text-dark"') >= 4
    # The fetch form must reach our route.
    assert 'action="/ui/boot/fetch-release"' in body


# ---------- Phase E: settings page ----------------------------------------


def test_ui_settings_requires_auth(client: TestClient) -> None:
    r = client.get("/ui/settings")
    assert r.status_code == 303
    assert r.headers["location"] == "/ui/login"


def test_ui_settings_page_renders(client: TestClient) -> None:
    _login(client)
    r = client.get("/ui/settings")
    assert r.status_code == 200
    body = r.text
    # Settings page advertises both the auth panel + the PXE panel.
    assert "Authentication" in body
    assert "passwd" in body  # the "rotate the OS password" hint
    assert "PXE proxy-DHCP" in body
    # Forms post to the right routes.
    assert 'action="/ui/settings/revoke-sessions"' in body
    assert 'action="/ui/settings/pxe-activate"' in body


def test_ui_settings_revoke_all_sessions_kills_active_logins(client: TestClient) -> None:
    """``Revoke all sessions`` empties the sessions table; the cookie
    presented next becomes a 401 / redirect to /ui/login."""
    _login(client)
    # Sanity: cookie works.
    assert client.get("/machines").status_code == 200
    r = client.post("/ui/settings/revoke-sessions")
    assert r.status_code == 200
    assert "Revoked" in r.text
    # The cookie's session row is gone now: API + bearer header both
    # 401 (the cookie's plaintext is no longer in the DB).
    assert client.get("/machines").status_code == 401
    assert client.get("/machines", headers=AUTH).status_code == 401


def test_ui_settings_pxe_activate_invokes_helper(client: TestClient) -> None:
    from unittest.mock import patch

    _login(client)
    with patch("bty.web._sysconfig.activate_pxe") as mock_activate:
        r = client.post(
            "/ui/settings/pxe-activate",
            data={"interface": "eth0", "subnet": "192.168.1.0/24"},
        )
    assert r.status_code == 200
    assert "PXE activated" in r.text
    mock_activate.assert_called_once_with("eth0", "192.168.1.0/24")


def test_ui_settings_pxe_activate_failure_shows_danger_flash(client: TestClient) -> None:
    from unittest.mock import patch

    from bty.web._sysconfig import SysConfigError

    _login(client)
    with patch(
        "bty.web._sysconfig.activate_pxe",
        side_effect=SysConfigError("invalid subnet"),
    ):
        r = client.post(
            "/ui/settings/pxe-activate",
            data={"interface": "eth0", "subnet": "garbage"},
        )
    assert r.status_code == 200
    assert "PXE activation failed" in r.text


def test_ui_boot_requires_auth(client: TestClient) -> None:
    """Without the cookie, /ui/boot redirects to login like the rest
    of the UI."""
    r = client.get("/ui/boot")
    assert r.status_code == 303
    assert r.headers["location"] == "/ui/login"


def test_ui_boot_fetch_requires_auth(client: TestClient) -> None:
    r = client.post("/ui/boot/fetch-release", data={"tag": "latest"})
    assert r.status_code == 303
    assert r.headers["location"] == "/ui/login"


def test_ui_machines_list_shows_boot_policy_badge(client: TestClient) -> None:
    _login(client)
    client.put(
        "/machines/aa:bb:cc:dd:ee:ff",
        json={"image": "demo.qcow2", "boot_policy": "flash"},
        headers=AUTH,
    )
    client.put(
        "/machines/11:22:33:44:55:66",
        json={"image": "demo.qcow2", "boot_policy": "local"},
        headers=AUTH,
    )
    r = client.get("/ui/machines")
    assert r.status_code == 200
    body = r.text
    # Both badges should appear in the table.
    assert "bg-danger" in body and ">flash<" in body
    assert "bg-secondary" in body and ">local<" in body
    # Table header now has Boot column and Last flashed column.
    assert "<th>Boot</th>" in body
    assert "<th>Last flashed</th>" in body


def test_ui_machine_delete_via_form(client: TestClient) -> None:
    _login(client)
    client.put(
        "/machines/aa:bb:cc:dd:ee:ff",
        json={"image": "demo.qcow2", "provisioning_mode": "none"},
        headers=AUTH,
    )
    r = client.post("/ui/machines/aa:bb:cc:dd:ee:ff/delete")
    assert r.status_code == 303
    api = client.get(
        "/machines/aa:bb:cc:dd:ee:ff",
        headers=AUTH,
    )
    assert api.status_code == 404


def test_ui_images_renders(client: TestClient) -> None:
    _login(client)
    r = client.get("/ui/images")
    assert r.status_code == 200
    assert "demo.qcow2" in r.text


# ---------- cross-cutting: cookie also authenticates the API ----------------


def test_cookie_auth_works_on_api_routes_too(client: TestClient) -> None:
    """The bearer dep accepts the cookie, so a logged-in browser can hit
    /machines (the API) without an Authorization header."""
    _login(client)
    r = client.get("/machines")
    assert r.status_code == 200
    assert r.json() == []


# ---------- vendored static assets (no CDN at runtime) ---------------------


def test_static_assets_served_locally(client: TestClient) -> None:
    """The wheel ships Bootstrap CSS, HTMX, and the SSE extension under
    /static so the appliance has no runtime CDN dependency."""
    for path, sniff in [
        ("/static/bootstrap.min.css", b".container"),
        ("/static/htmx.min.js", b"htmx"),
        ("/static/sse.js", b"sse"),
    ]:
        r = client.get(path)
        assert r.status_code == 200, f"{path}: {r.status_code}"
        assert sniff in r.content, f"{path} missing expected marker {sniff!r}"


def test_layout_has_no_external_origins(client: TestClient) -> None:
    """The login page (and by extension the layout) must not reference
    any third-party origin - the appliance runs offline."""
    r = client.get("/ui/login")
    assert r.status_code == 200
    assert "cdn.jsdelivr.net" not in r.text
    assert "/static/bootstrap.min.css" in r.text
    assert "/static/htmx.min.js" in r.text


# ---------- SSE live updates -----------------------------------------------


def test_sse_endpoint_requires_auth(client: TestClient) -> None:
    """The events stream must reject unauthenticated subscribers (same
    bearer/cookie check as the API).

    We don't exercise the body here - TestClient's sync httpx hangs on
    open-ended event streams. The streaming contract itself is covered
    by the unit tests in ``tests/test_web_events.py``.
    """
    r = client.get("/events/machines")
    assert r.status_code == 401


def test_machines_page_subscribes_via_sse(client: TestClient) -> None:
    """The machines table must declare its SSE subscription so the
    browser actually hooks up live updates."""
    _login(client)
    r = client.get("/ui/machines")
    assert r.status_code == 200
    assert 'sse-connect="/events/machines"' in r.text
    assert 'sse-swap="machines-update"' in r.text
