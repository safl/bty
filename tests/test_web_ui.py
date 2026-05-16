"""Tests for the bty-web browser UI.

Cookie-based auth flow, server-rendered pages via TestClient. The
fixture monkeypatches ``pamela.authenticate`` to always succeed and
drives ``POST /ui/login`` once to mint a real session cookie; tests
opt in to the authenticated path via ``cookies=AUTH`` (or call
``_login(client)`` for the sticky form).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from bty.web._app import create_app

TEST_SERVICE_USER = "ui-test-user"
TEST_SECRET_KEY = "test-secret-not-for-prod-use"

# Mutated by the fixture so tests calling the API with
# ``cookies=AUTH`` get the cookie they need.
AUTH: dict[str, str] = {}


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    image_root = tmp_path / "images"
    image_root.mkdir()
    (image_root / "demo.qcow2").write_bytes(b"\0" * 16)
    state = tmp_path / "state.db"
    app = create_app(
        state_path=state,
        service_user=TEST_SERVICE_USER,
        secret_key=TEST_SECRET_KEY,
        image_root=image_root,
    )

    import pamela

    monkeypatch.setattr(pamela, "authenticate", lambda *a, **kw: True)

    # ``follow_redirects=False`` so we can assert on 303 hops.
    with TestClient(app, follow_redirects=False) as c:
        # Drive /ui/login once with PAM monkeypatched so we have a
        # real session cookie value tests can re-attach via
        # ``cookies=AUTH``. Don't leave it sticky on the client -
        # tests opt in by passing ``cookies=AUTH`` (matches the
        # ``_login(client)`` helper below for tests that want the
        # sticky form).
        r = c.post("/ui/login", data={"password": "x"}, follow_redirects=False)
        assert r.status_code == 303, r.text
        cookie_value = r.cookies.get("bty-token")
        assert cookie_value is not None
        AUTH.clear()
        AUTH["bty-token"] = cookie_value
        c.cookies.clear()
        try:
            yield c
        finally:
            AUTH.clear()


def _login(client: TestClient) -> None:
    """Make subsequent requests on ``client`` carry the authed
    session cookie. The fixture has already minted one via /ui/login;
    we just attach it sticky so tests don't have to repeat
    ``cookies=AUTH`` on every call."""
    client.cookies.set("bty-token", AUTH["bty-token"])


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
    assert "bty-token" not in client.cookies


def test_ui_login_valid_password_sets_cookie_and_redirects(client: TestClient) -> None:
    from unittest.mock import patch

    with patch("pamela.authenticate", return_value=True):
        r = client.post("/ui/login", data={"password": "hunter2"})
    assert r.status_code == 303
    assert r.headers["location"] == "/ui/dashboard"
    assert "bty-token" in client.cookies


def test_ui_logout_clears_cookie(client: TestClient) -> None:
    _login(client)
    r = client.post("/ui/logout")
    assert r.status_code == 303
    assert r.headers["location"] == "/ui/login"
    # The Set-Cookie header carries an empty value + Max-Age=0.
    set_cookie = r.headers.get("set-cookie", "")
    assert "bty-token" in set_cookie


# ---------- pages (auth'd) --------------------------------------------------


def test_ui_dashboard_renders_after_login(client: TestClient) -> None:
    _login(client)
    r = client.get("/ui/dashboard")
    assert r.status_code == 200
    assert "Dashboard" in r.text
    assert "Machines" in r.text


def test_ui_dashboard_shows_recent_activity_after_a_pxe_event(client: TestClient) -> None:
    """The dashboard re-uses ``_events_card.html`` to surface the
    last 10 events. Trigger a PXE check-in so there's a row, then
    assert the card title + the event kind appear in the dashboard
    body. The full timeline link should also be present."""
    _login(client)
    client.get("/pxe/aa:bb:cc:dd:ee:fa")
    r = client.get("/ui/dashboard")
    assert r.status_code == 200
    body = r.text
    assert "Recent activity" in body
    assert "machine.discovered" in body
    assert 'href="/ui/events"' in body


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


def test_ui_boot_page_shows_recent_activity_card(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The /ui/boot page reuses ``_events_card.html`` to show the
    last 10 boot.* events (release fetches + fetch failures).
    Trigger a successful sync fetch first so a row exists."""
    _login(client)

    def fake_fetch(boot_dir, *_a, **_kw):  # type: ignore[no-untyped-def]
        from bty.web._releases import FetchResult

        return FetchResult(base_url="https://test.invalid/x", artifacts=("a",), total_bytes=42)

    monkeypatch.setattr("bty.web._releases.fetch_release", fake_fetch)
    client.post("/ui/boot/fetch-release", data={"tag": "v0.0.1"})
    r = client.get("/ui/boot")
    assert r.status_code == 200
    body = r.text
    assert "Recent boot-artefact activity" in body
    assert "boot.release.fetched" in body


def test_ui_machines_filter_assigned_excludes_discovered(client: TestClient) -> None:
    """``?filter=assigned`` is the symmetric pivot for
    ``?filter=discovered``: only machines bound to an image."""
    _login(client)
    client.get("/pxe/aa:bb:cc:dd:ee:03")  # discovered (no image)
    client.put(
        "/machines/aa:bb:cc:dd:ee:04",
        json={
            "bty_image_ref": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
        },
    )
    r = client.get("/ui/machines?filter=assigned")
    assert r.status_code == 200
    body = r.text
    assert "aa:bb:cc:dd:ee:04" in body
    assert "aa:bb:cc:dd:ee:03" not in body
    assert "filter:" in body


def test_ui_machines_filter_unrecognised_value_falls_back_to_full_list(
    client: TestClient,
) -> None:
    """An unrecognised ``?filter=foo`` shows the full list and
    suppresses the active-filter banner -- defensive so a typo'd
    URL doesn't render a confusing "filter: foo" chip with no
    filtering applied."""
    _login(client)
    client.get("/pxe/aa:bb:cc:dd:ee:05")
    r = client.get("/ui/machines?filter=garbage")
    assert r.status_code == 200
    body = r.text
    assert "aa:bb:cc:dd:ee:05" in body
    assert "filter:" not in body
    # SSE wiring restored when no filter active.
    assert 'sse-connect="/events/machines"' in body


def test_ui_machines_filter_discovered_excludes_assigned(client: TestClient) -> None:
    """``?filter=discovered`` (the dashboard counter card link)
    only shows machines without an assigned image, and drops the
    SSE auto-refresh wiring so the filter isn't immediately
    overwritten by the next ``machines-update`` event."""
    _login(client)
    # Discovered (auto-discovery, no image bound).
    client.get("/pxe/aa:bb:cc:dd:ee:01")
    # Assigned (operator PUT with bty_image_ref).
    client.put(
        "/machines/aa:bb:cc:dd:ee:02",
        json={
            "bty_image_ref": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
        },
    )

    r = client.get("/ui/machines?filter=discovered")
    assert r.status_code == 200
    body = r.text
    assert "aa:bb:cc:dd:ee:01" in body
    assert "aa:bb:cc:dd:ee:02" not in body
    # Active-filter banner; SSE wiring suppressed.
    assert "filter:" in body
    assert "show all" in body
    assert 'sse-connect="/events/machines"' not in body


def test_ui_machines_lists_known_records(client: TestClient) -> None:
    _login(client)
    # Seed via the API.
    client.put(
        "/machines/aa:bb:cc:dd:ee:ff",
        json={
            "bty_image_ref": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
        },
        cookies=AUTH,
    )
    r = client.get("/ui/machines")
    assert r.status_code == 200
    assert "aa:bb:cc:dd:ee:ff" in r.text
    # SHA short-prefix (first 12 hex chars) renders into the row's
    # image cell. The full SHA is in the title= attribute.
    assert "0123456789ab" in r.text


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
        json={
            "bty_image_ref": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
        },
        cookies=AUTH,
    )
    r = client.get("/ui/machines/aa:bb:cc:dd:ee:ff")
    assert r.status_code == 200
    assert "aa:bb:cc:dd:ee:ff" in r.text


def test_ui_machine_detail_404(client: TestClient) -> None:
    _login(client)
    r = client.get("/ui/machines/aa:bb:cc:dd:ee:00")
    assert r.status_code == 404


def test_ui_catalog_entry_form_rejects_bad_url(client: TestClient) -> None:
    """The form-style endpoint at ``POST /ui/catalog/entries``
    must apply the same Pydantic ``CatalogEntryAdd`` validation
    as the JSON ``POST /catalog/entries`` endpoint -- the form
    used to skip pattern validation entirely, accepting
    ``ftp://`` and host-less URLs that the API rejects.

    On validation failure the form 303s back to /ui/images with
    a URL-encoded ``?error=`` query param; the redirect must be
    well-formed regardless of the exception text. We follow the
    redirect manually and assert the URL shape."""
    _login(client)
    r = client.post(
        "/ui/catalog/entries",
        data={"image_url": "ftp://example.invalid/foo.img.gz", "sha_url": ""},
        follow_redirects=False,
    )
    assert r.status_code == 303
    location = r.headers["location"]
    assert location.startswith("/ui/images?error="), location
    # URL-encoded payload: spaces and special chars become %xx,
    # so a raw space would be a sign of the un-quoted bug.
    assert " " not in location

    # Bare-host URL (no filename) should also bounce with a
    # ``filename component`` flash.
    r = client.post(
        "/ui/catalog/entries",
        data={"image_url": "https://example.invalid", "sha_url": ""},
        follow_redirects=False,
    )
    assert r.status_code == 303
    location = r.headers["location"]
    assert location.startswith("/ui/images?error="), location
    assert "filename%20component" in location or "filename+component" in location


def test_ui_catalog_entry_form_requires_auth(client: TestClient) -> None:
    """Unauthed POST to /ui/catalog/entries bounces to /ui/login,
    not 303 to /ui/images. Defence-in-depth: the JSON sibling at
    POST /catalog/entries is also gated, but a logged-out form
    must hit the same auth wall."""
    r = client.post(
        "/ui/catalog/entries",
        data={"image_url": "https://example.invalid/x.img.gz", "sha_url": ""},
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/ui/login"


def test_ui_catalog_entry_form_happy_path_lands_row_and_303s(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Valid image_url (no sha_url) -> 303 back to /ui/images and a
    new ``catalog_entries`` row is visible via the JSON
    ``GET /catalog/entries`` endpoint. Stubs the size-probe HEAD
    so no real network call leaves the test."""
    from bty.web import _app as _web_app

    monkeypatch.setattr(_web_app, "_head_content_length", lambda url: None)
    _login(client)
    r = client.post(
        "/ui/catalog/entries",
        data={
            "image_url": "https://example.invalid/charlie.img.gz",
            "sha_url": "",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/ui/images"
    entries = client.get("/catalog/entries", cookies=AUTH).json()
    assert any(e["src"] == "https://example.invalid/charlie.img.gz" for e in entries)


def test_ui_machine_upsert_form_rejects_non_hex_sha256(client: TestClient) -> None:
    """The form-style ``POST /ui/machines/{mac}`` must apply the
    same Pydantic ``MachineUpsert`` validation as the JSON
    ``PUT /machines/{mac}``. Previously the form accepted any
    string for ``bty_image_ref`` and silently landed garbage in
    state.db; the JSON API rejected the same value with 422.

    On validation failure the form 303s to /ui/machines/{mac}
    with a URL-encoded ``?error=`` flash, matching the catalog
    form pattern from round 6."""
    _login(client)
    # Create a machine first so the detail page exists.
    client.put(
        "/machines/aa:bb:cc:dd:ee:ff",
        json={
            "bty_image_ref": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
        },
        cookies=AUTH,
    )
    # Submit a non-hex SHA via the form.
    r = client.post(
        "/ui/machines/aa:bb:cc:dd:ee:ff",
        data={
            "bty_image_ref": "not-a-real-sha-just-garbage",
            "boot_policy": "local",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    location = r.headers["location"]
    assert location.startswith("/ui/machines/aa:bb:cc:dd:ee:ff?error="), location
    # The well-formed-URL invariant: no raw spaces.
    assert " " not in location

    # The machine record was NOT updated -- the bad SHA didn't
    # land in state.db. (The original good SHA from the seed PUT
    # is still there.)
    r = client.get("/machines/aa:bb:cc:dd:ee:ff", cookies=AUTH)
    assert r.status_code == 200
    assert (
        r.json()["bty_image_ref"]
        == "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
    )


def test_ui_machine_detail_renders_error_query_param_as_flash_banner(
    client: TestClient,
) -> None:
    """``/ui/machines/{mac}`` reads ``?error=<msg>`` so the
    upsert form's validation-failure bounce surfaces as a
    flash banner instead of a silent redirect."""
    _login(client)
    client.put(
        "/machines/aa:bb:cc:dd:ee:ff",
        json={
            "bty_image_ref": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
        },
        cookies=AUTH,
    )
    r = client.get(
        "/ui/machines/aa:bb:cc:dd:ee:ff?error=validation+failed%3A+test",
        follow_redirects=False,
    )
    assert r.status_code == 200
    body = r.text
    assert 'class="alert alert-danger"' in body
    assert "validation failed: test" in body


def test_ui_images_renders_error_query_param_as_flash_banner(
    client: TestClient,
) -> None:
    """The form-style ``POST /ui/catalog/entries`` 303s back to
    /ui/images with a ``?error=...`` query param on validation
    failure / sha-resolve failure / duplicate-409. The page
    handler must read the param into the layout's flash slot,
    otherwise the operator gets a silent bounce with no reason
    visible. Round 6 added the redirect; this test pins that
    round 7's page handler renders it."""
    _login(client)
    r = client.get(
        "/ui/images?error=validation+failed%3A+test+message",
        follow_redirects=False,
    )
    assert r.status_code == 200
    body = r.text
    # The layout renders the flash inside an alert div.
    assert 'class="alert alert-danger"' in body
    # The decoded message appears in the rendered page.
    assert "validation failed: test message" in body


def test_ui_machine_upsert_via_form(client: TestClient) -> None:
    _login(client)
    r = client.post(
        "/ui/machines/aa:bb:cc:dd:ee:ff",
        data={
            "bty_image_ref": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
            "hostname": "bty-ui-test",
        },
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/ui/machines"
    # The record landed.
    api = client.get(
        "/machines/aa:bb:cc:dd:ee:ff",
        cookies=AUTH,
    )
    assert api.status_code == 200
    assert (
        api.json()["bty_image_ref"]
        == "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
    )
    assert api.json()["hostname"] == "bty-ui-test"
    # Form omits boot_policy -> dependency default applies (local).
    assert api.json()["boot_policy"] == "local"


def test_ui_machine_upsert_persists_boot_policy_flash(client: TestClient) -> None:
    """Form upsert with boot_policy=flash also requires the operator
    to have picked a target_disk_serial (post-v0.18 safety gate).
    The dropdown is populated from machines.known_disks after
    bty-tui posts its inventory; this test sends the serial
    directly via form data."""
    _login(client)
    r = client.post(
        "/ui/machines/aa:bb:cc:dd:ee:ff",
        data={
            "bty_image_ref": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
            "hostname": "",
            "boot_policy": "flash",
            "target_disk_serial": "ATA-WDC-123456",
        },
    )
    assert r.status_code == 303
    api = client.get(
        "/machines/aa:bb:cc:dd:ee:ff",
        cookies=AUTH,
    )
    assert api.json()["boot_policy"] == "flash"
    assert api.json()["target_disk_serial"] == "ATA-WDC-123456"


def test_ui_machine_detail_renders_disk_inventory_dropdown(client: TestClient) -> None:
    """When the machine has ``known_disks`` populated (bty-tui has
    reported in), /ui/machines/{mac} shows a populated <select>
    with one <option> per disk. Each option displays the path /
    size / model / serial so the operator picks a recognisable
    line."""
    _login(client)
    # Discover the machine, then post inventory (mirrors what
    # bty-tui does on startup).
    client.get("/pxe/aa:bb:cc:dd:ee:88")
    inv = client.post(
        "/pxe/aa:bb:cc:dd:ee:88/inventory",
        json={
            "disks": [
                {
                    "path": "/dev/sda",
                    "size": "500G",
                    "model": "Samsung 870 EVO",
                    "serial": "S5RRNF0N123456",
                    "tran": "sata",
                },
                {
                    "path": "/dev/nvme0n1",
                    "size": "2T",
                    "model": "WDC PC SN810",
                    "serial": "21345A800002",
                    "tran": "nvme",
                },
            ],
        },
    )
    assert inv.status_code == 204, inv.text
    r = client.get("/ui/machines/aa:bb:cc:dd:ee:88", cookies=AUTH)
    assert r.status_code == 200
    body = r.text
    # The <select> for target_disk_serial exists.
    assert 'name="target_disk_serial"' in body
    # Both serials are options.
    assert "S5RRNF0N123456" in body
    assert "21345A800002" in body
    # Each option shows the path so the operator can map the serial.
    assert "/dev/sda" in body
    assert "/dev/nvme0n1" in body
    # The "no inventory yet" alert should NOT render.
    assert "No disk inventory yet for this machine" not in body


def test_ui_machine_detail_renders_no_inventory_warning(client: TestClient) -> None:
    """A machine that hasn't yet reported its inventory shows a
    yellow warning alert pointing at the recovery path ("set
    boot_policy=tui and power-cycle") instead of a broken empty
    dropdown."""
    _login(client)
    # Seed a machine record without ever posting inventory.
    client.put(
        "/machines/aa:bb:cc:dd:ee:89",
        json={"boot_policy": "local"},
        cookies=AUTH,
    )
    r = client.get("/ui/machines/aa:bb:cc:dd:ee:89", cookies=AUTH)
    assert r.status_code == 200
    body = r.text
    assert "No disk inventory yet for this machine" in body
    assert "alert-warning" in body
    # The dropdown <select> should NOT be rendered; the hidden
    # input form-field preserves the existing serial (empty here)
    # so a form submit doesn't clobber the value with garbage.
    assert 'id="target_disk_serial"' not in body
    assert 'type="hidden" name="target_disk_serial"' in body


def test_ui_machine_upsert_refuses_flash_without_target_disk(client: TestClient) -> None:
    """Safety gate (operator request: refuse if target_disk is
    unset). Setting boot_policy=flash without target_disk_serial
    bounces back to /ui/machines/{mac} with a flash banner
    explaining how to fix it -- and the machine row does NOT
    flip to boot_policy=flash."""
    _login(client)
    # Seed the machine first so the redirect target exists.
    client.put(
        "/machines/aa:bb:cc:dd:ee:ff",
        json={
            "bty_image_ref": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
            "boot_policy": "local",
        },
        cookies=AUTH,
    )
    r = client.post(
        "/ui/machines/aa:bb:cc:dd:ee:ff",
        data={
            "bty_image_ref": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
            "boot_policy": "flash",
            "target_disk_serial": "",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "/ui/machines/aa:bb:cc:dd:ee:ff?error=" in r.headers["location"]
    api = client.get("/machines/aa:bb:cc:dd:ee:ff", cookies=AUTH).json()
    # Safety gate: didn't flip to flash.
    assert api["boot_policy"] == "local"
    assert api["target_disk_serial"] is None


def test_ui_machine_upsert_rejects_unknown_boot_policy(client: TestClient) -> None:
    """Form upsert routes through the same Pydantic ``MachineUpsert``
    as the JSON API; an invalid ``boot_policy`` produces a 303 with
    an error flash (matches the catalog-form pattern) instead of a
    400 page that loses form context."""
    _login(client)
    r = client.post(
        "/ui/machines/aa:bb:cc:dd:ee:ff",
        data={
            "bty_image_ref": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
            "boot_policy": "yolo",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    location = r.headers["location"]
    assert location.startswith("/ui/machines/aa:bb:cc:dd:ee:ff?error="), location
    assert "boot_policy" in location


def test_ui_machine_detail_renders_boot_policy_dropdown(client: TestClient) -> None:
    _login(client)
    client.put(
        "/machines/aa:bb:cc:dd:ee:ff",
        json={
            "bty_image_ref": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
            "boot_policy": "flash",
        },
        cookies=AUTH,
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
        "bty-netboot-x86_64.vmlinuz",
        "bty-netboot-x86_64.initrd",
        "bty-netboot-x86_64.squashfs",
        "bty-netboot-x86_64.sha256",
    ):
        assert name in body, name
    # Empty boot dir => four "missing" badges (warning kind).
    assert body.count("missing</span>") == 4
    assert body.count('class="badge bg-warning text-dark"') >= 4
    # HTMX-style background trigger: the page wires a button that
    # POSTs to /boot/releases (the trackable release-fetch endpoint)
    # and polls /boot/releases for progress.
    assert 'id="enqueue-fetch-btn"' in body
    assert "/boot/releases" in body
    # The boot-page polling JS must NOT reference a never-set
    # ``_just_completed_marker`` field nor wrap ``refresh`` to
    # do a second ``fetch /boot/releases`` per poll cycle (the
    # latter doubles load). Pin the cleaned-up shape so a
    # copy-paste doesn't reintroduce them.
    assert "_just_completed_marker" not in body
    # The bare-quoted ``"/boot/releases"`` (closing quote, no
    # trailing slash) appears in exactly two places: the refresh
    # GET and the enqueue POST. The cancel DELETE uses
    # ``"/boot/releases/" + encodeURIComponent(tag)`` (trailing
    # slash, different literal). A third bare-quoted occurrence
    # means the double-fetch ``origRefresh`` wrapper crept back.
    bare_count = body.count('"/boot/releases"')
    assert bare_count == 2, (
        f"expected exactly 2 references (refresh GET + enqueue POST); got {bare_count}"
    )


# ---------- Phase E: settings page ----------------------------------------


def test_ui_settings_requires_auth(client: TestClient) -> None:
    r = client.get("/ui/settings")
    assert r.status_code == 303
    assert r.headers["location"] == "/ui/login"


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
        json={
            "bty_image_ref": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
            "boot_policy": "flash",
        },
        cookies=AUTH,
    )
    client.put(
        "/machines/11:22:33:44:55:66",
        json={
            "bty_image_ref": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
            "boot_policy": "local",
        },
        cookies=AUTH,
    )
    # Auto-discovery via /pxe lands a third row with boot_policy=tui
    # so we can exercise all three badge variants in one table.
    client.get("/pxe/aa:bb:cc:dd:ee:01")
    r = client.get("/ui/machines")
    assert r.status_code == 200
    body = r.text
    # All three boot-policy badges should appear in the table.
    assert "bg-danger" in body and ">flash<" in body
    assert "bg-secondary" in body and ">local<" in body
    assert "bg-info text-dark" in body and ">tui<" in body
    # Table header has Boot column + Last flashed column.
    assert "<th>Boot</th>" in body
    assert "<th>Last flashed</th>" in body


def test_ui_machine_delete_via_form(client: TestClient) -> None:
    _login(client)
    client.put(
        "/machines/aa:bb:cc:dd:ee:ff",
        json={
            "bty_image_ref": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
        },
        cookies=AUTH,
    )
    r = client.post("/ui/machines/aa:bb:cc:dd:ee:ff/delete")
    assert r.status_code == 303
    api = client.get(
        "/machines/aa:bb:cc:dd:ee:ff",
        cookies=AUTH,
    )
    assert api.status_code == 404


def test_ui_images_renders(client: TestClient) -> None:
    _login(client)
    r = client.get("/ui/images")
    assert r.status_code == 200
    assert "demo.qcow2" in r.text


def test_ui_images_renders_fetch_button_for_unhashed_url_entry(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Operator-visible bug fix: a catalog row added by URL without
    a sha_url used to render a 'Hash' button that 404'd when
    clicked (HashManager needs a local file). Now those entries
    render a 'Fetch' button instead, which downloads + computes
    + back-fills the sha via the DownloadManager.

    Guards the template dispatch rule: ``not u.sha256 + no local
    source -> Fetch button``."""
    # Stub the HEAD probe + sha_url resolve so the catalog-entry
    # add doesn't try to reach example.invalid.
    from bty.web import _app as _web_app

    monkeypatch.setattr(_web_app, "_head_content_length", lambda url: None)
    _login(client)
    add = client.post(
        "/ui/catalog/entries",
        data={
            "image_url": "https://example.invalid/rolling.img.gz",
            "sha_url": "",
        },
        follow_redirects=False,
    )
    assert add.status_code == 303, add.text
    r = client.get("/ui/images")
    assert r.status_code == 200
    body = r.text
    # The row exists.
    assert "rolling.img.gz" in body
    # Fetch button is rendered for this entry.
    assert "bty-fetch-btn" in body
    # The bug: this entry must NOT carry a hash button.
    # The dir-scan demo.qcow2 also lacks a sha (no sidecar in the
    # fixture), so the Hash button still appears for THAT row.
    # We assert specifically that the URL-row's neighbourhood
    # does not have a hash button by checking the per-row marker.
    # ``data-name="rolling.img.gz"`` should only appear on
    # ``bty-fetch-btn`` (not ``bty-hash-btn``) for this row.
    fetch_idx = body.find('data-name="rolling.img.gz"')
    hash_idx_before_fetch = body.rfind("bty-hash-btn", 0, fetch_idx)
    hash_idx_after_fetch = body.find("bty-hash-btn", fetch_idx)
    fetch_btn_idx = body.rfind("bty-fetch-btn", 0, fetch_idx)
    # The fetch-btn class must be on the SAME button as the data-
    # name for rolling.img.gz, so its closest preceding bty-*-btn
    # marker must be bty-fetch-btn.
    assert fetch_btn_idx != -1
    assert fetch_btn_idx > (hash_idx_before_fetch or -1) if hash_idx_before_fetch != -1 else True
    # And the nearest following bty-hash-btn (if any) is for a
    # later row, not this row.
    if hash_idx_after_fetch != -1:
        # The following hash-btn shouldn't carry rolling.img.gz's
        # data-name.
        next_data_name_idx = body.find('data-name="', hash_idx_after_fetch)
        if next_data_name_idx != -1:
            chunk = body[next_data_name_idx : next_data_name_idx + 80]
            assert "rolling.img.gz" not in chunk


# ---------- /ui/settings/tftp-control --------------------------------------


def test_ui_settings_tftp_control_requires_auth(client: TestClient) -> None:
    """Unauthed POST bounces to /ui/login like the rest of the UI;
    no TFTP daemon action is taken."""
    r = client.post("/ui/settings/tftp-control", data={"action": "restart"})
    assert r.status_code == 303
    assert r.headers["location"] == "/ui/login"


def test_ui_settings_tftp_control_success_renders_green_flash(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``control_tftp`` returning cleanly produces a 200 with a
    success flash on the settings page. The handler also records a
    ``settings.tftp.controlled`` event."""
    from bty.web import _sysconfig

    seen: list[str] = []
    monkeypatch.setattr(_sysconfig, "control_tftp", lambda action: seen.append(action))
    _login(client)
    r = client.post("/ui/settings/tftp-control", data={"action": "restart"})
    assert r.status_code == 200
    assert seen == ["restart"]
    # Green flash on the rendered settings page.
    body = r.text
    assert "alert-success" in body
    assert "Restarted TFTP" in body
    # Event recorded.
    events = client.get(
        "/events",
        params={"subject_kind": "settings", "subject_id": "tftp"},
        cookies=AUTH,
    ).json()["events"]
    assert any(e["kind"] == "settings.tftp.controlled" for e in events)


def test_ui_settings_tftp_control_failure_renders_red_flash_and_logs_event(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``SysConfigError`` from the helper bounces back to the
    settings page with a red flash AND a ``settings.tftp.control_failed``
    event so the operator sees the systemctl exit code in the
    audit log without having to ssh in."""
    from bty.web import _sysconfig

    def _raise(action: str) -> None:
        raise _sysconfig.SysConfigError("dnsmasq.service is masked")

    monkeypatch.setattr(_sysconfig, "control_tftp", _raise)
    _login(client)
    r = client.post("/ui/settings/tftp-control", data={"action": "start"})
    assert r.status_code == 200
    body = r.text
    assert "alert-danger" in body
    assert "dnsmasq.service is masked" in body
    events = client.get(
        "/events",
        params={"subject_kind": "settings", "subject_id": "tftp"},
        cookies=AUTH,
    ).json()["events"]
    failed = [e for e in events if e["kind"] == "settings.tftp.control_failed"]
    assert len(failed) == 1
    assert failed[0]["details"]["action"] == "start"


def test_ui_settings_tftp_control_unknown_action_surfaces_clear_error(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bad ``action`` value (typo from a hand-crafted form post or
    a stale page) hits the allowlist check in ``control_tftp`` and
    renders the failure on the settings page."""
    # No monkeypatch needed for this path -- ``control_tftp`` raises
    # before reaching subprocess.
    _login(client)
    r = client.post("/ui/settings/tftp-control", data={"action": "explode"})
    assert r.status_code == 200
    assert "alert-danger" in r.text
    assert "unknown action" in r.text


def test_ui_settings_tftp_control_empty_action_surfaces_clear_error(
    client: TestClient,
) -> None:
    """Form posted without an action field: the handler still
    renders cleanly and the operator sees a "no action specified"
    flash instead of a 500."""
    _login(client)
    r = client.post("/ui/settings/tftp-control", data={})
    assert r.status_code == 200
    assert "alert-danger" in r.text
    assert "no action specified" in r.text


# ---------- /ui/boot/fetch-release ------------------------------------------


def test_ui_boot_fetch_success_renders_green_flash(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Happy path: ``_releases.fetch_release`` returns a
    ``FetchResult`` -> 200 with a green flash listing the
    artifact count + total bytes. Also records the
    ``boot.release.fetched`` event."""
    from bty.web import _releases

    def _stub(boot_root_arg: Path, *, tag: str) -> _releases.FetchResult:
        return _releases.FetchResult(
            base_url=f"https://example.invalid/releases/{tag}",
            artifacts=("a.efi", "b.vmlinuz", "c.initrd"),
            total_bytes=12345,
        )

    monkeypatch.setattr(_releases, "fetch_release", _stub)
    _login(client)
    r = client.post("/ui/boot/fetch-release", data={"tag": "v0.1.2"})
    assert r.status_code == 200
    body = r.text
    assert "alert-success" in body
    assert "Fetched 3 artifacts" in body
    assert "12,345 bytes" in body
    events = client.get(
        "/events",
        params={"subject_kind": "boot", "subject_id": "v0.1.2"},
        cookies=AUTH,
    ).json()["events"]
    assert any(e["kind"] == "boot.release.fetched" for e in events)


def test_ui_boot_fetch_failure_renders_red_flash_and_logs_event(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``FetchError`` (no network / 404 release tag / sha mismatch)
    surfaces on the page with a red flash + a
    ``boot.release.fetch_failed`` event."""
    from bty.web import _releases

    def _raise(boot_root_arg: Path, *, tag: str) -> _releases.FetchResult:
        raise _releases.FetchError(f"tag {tag!r} not found")

    monkeypatch.setattr(_releases, "fetch_release", _raise)
    _login(client)
    r = client.post("/ui/boot/fetch-release", data={"tag": "v0.999.999"})
    assert r.status_code == 200
    assert "alert-danger" in r.text
    assert "Fetch failed" in r.text
    events = client.get(
        "/events",
        params={"subject_kind": "boot", "subject_id": "v0.999.999"},
        cookies=AUTH,
    ).json()["events"]
    failed = [e for e in events if e["kind"] == "boot.release.fetch_failed"]
    assert len(failed) == 1
    assert failed[0]["details"]["tag"] == "v0.999.999"


def test_ui_boot_fetch_empty_tag_falls_back_to_latest(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Submitting the form with an empty ``tag`` field is the same
    as omitting it -- the handler resolves to ``"latest"``. Guards
    against a UI change that wires an empty default to the form
    accidentally pointing the operator at a release tagged
    literally ``""``."""
    from bty.web import _releases

    seen: list[str] = []

    def _stub(boot_root_arg: Path, *, tag: str) -> _releases.FetchResult:
        seen.append(tag)
        return _releases.FetchResult(
            base_url=f"https://example.invalid/releases/{tag}",
            artifacts=(),
            total_bytes=0,
        )

    monkeypatch.setattr(_releases, "fetch_release", _stub)
    _login(client)
    r = client.post("/ui/boot/fetch-release", data={"tag": ""})
    assert r.status_code == 200
    assert seen == ["latest"]


# ---------- cross-cutting: cookie also authenticates the API ----------------


def test_cookie_auth_works_on_api_routes_too(client: TestClient) -> None:
    """The session cookie set by /ui/login also authenticates the JSON
    API, so a logged-in browser (or scripted shell) can hit /machines
    without a separate auth step."""
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
    """The login HTML (and by extension the layout) loads its
    JS / CSS from ``/static/*`` only, not from a CDN."""
    r = client.get("/ui/login")
    assert r.status_code == 200
    assert "cdn.jsdelivr.net" not in r.text
    assert "/static/bootstrap.min.css" in r.text
    assert "/static/htmx.min.js" in r.text


def test_vendored_css_has_no_runtime_external_fetches(client: TestClient) -> None:
    """Strict no-CDN guarantee: the operator's browser must not be
    able to reach out to any third-party origin while using bty-web.
    The upstream Bootswatch Sandstone CSS ships with an
    ``@import url(https://fonts.googleapis.com/...)`` for Roboto at
    the top of the file; we strip that line when vendoring so the
    browser falls back to the system sans-serif. This test guards
    against a future refresh quietly re-introducing it.

    Other URLs in the bundled CSS are all in ``/* ... */`` license
    comments (CSS parsers ignore those) or the SVG XML namespace
    identifier (``http://www.w3.org/2000/svg``, never fetched).
    """
    for path in ("/static/bootstrap.min.css", "/static/bootstrap-icons.min.css"):
        r = client.get(path)
        assert r.status_code == 200, f"{path}: {r.status_code}"
        body = r.text
        assert "@import url(http" not in body, (
            f"{path} contains an @import that would trigger a runtime "
            f"external fetch; strip it when vendoring."
        )
        assert "fonts.googleapis.com" not in body, (
            f"{path} still references fonts.googleapis.com; strip the @import line."
        )


# ---------- SSE live updates -----------------------------------------------


def test_sse_endpoint_requires_auth(client: TestClient) -> None:
    """The events stream must reject unauthenticated subscribers (same
    session-cookie check as the rest of the API).

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
