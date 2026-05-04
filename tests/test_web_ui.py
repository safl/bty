"""Tests for the bty-web browser UI (milestone 12 phase 1).

Cookie-based auth flow, server-rendered pages via TestClient.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from bty.web._app import create_app
from bty.web._auth import SESSION_COOKIE

TEST_TOKEN = "ui-test-token"


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    image_root = tmp_path / "images"
    image_root.mkdir()
    (image_root / "demo.qcow2").write_bytes(b"\0" * 16)
    app = create_app(
        state_path=tmp_path / "state.db",
        bearer_token=TEST_TOKEN,
        image_root=image_root,
    )
    # ``follow_redirects=False`` so we can assert on 303 hops.
    with TestClient(app, follow_redirects=False) as c:
        yield c


def _login(client: TestClient) -> None:
    r = client.post("/ui/login", data={"token": TEST_TOKEN})
    assert r.status_code == 303
    assert r.headers["location"] == "/ui/dashboard"
    assert SESSION_COOKIE in client.cookies


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
    assert 'name="token"' in r.text


def test_ui_login_invalid_token_re_renders_with_error(client: TestClient) -> None:
    r = client.post("/ui/login", data={"token": "wrong"})
    assert r.status_code == 200
    assert "Invalid token" in r.text
    assert SESSION_COOKIE not in client.cookies


def test_ui_login_valid_token_sets_cookie_and_redirects(client: TestClient) -> None:
    r = client.post("/ui/login", data={"token": TEST_TOKEN})
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


def test_ui_machines_lists_known_records(client: TestClient) -> None:
    _login(client)
    # Seed via the API.
    client.put(
        "/machines/aa:bb:cc:dd:ee:ff",
        json={"image": "demo.qcow2", "provisioning_mode": "none"},
        headers={"Authorization": f"Bearer {TEST_TOKEN}"},
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
        headers={"Authorization": f"Bearer {TEST_TOKEN}"},
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
        headers={"Authorization": f"Bearer {TEST_TOKEN}"},
    )
    assert api.status_code == 200
    assert api.json()["image"] == "demo.qcow2"
    assert api.json()["hostname"] == "bty-ui-test"


def test_ui_machine_delete_via_form(client: TestClient) -> None:
    _login(client)
    client.put(
        "/machines/aa:bb:cc:dd:ee:ff",
        json={"image": "demo.qcow2", "provisioning_mode": "none"},
        headers={"Authorization": f"Bearer {TEST_TOKEN}"},
    )
    r = client.post("/ui/machines/aa:bb:cc:dd:ee:ff/delete")
    assert r.status_code == 303
    api = client.get(
        "/machines/aa:bb:cc:dd:ee:ff",
        headers={"Authorization": f"Bearer {TEST_TOKEN}"},
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
