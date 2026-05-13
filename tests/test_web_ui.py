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


def test_ui_dashboard_shows_pxe_state_tile(client: TestClient) -> None:
    """The 4th dashboard tile reflects PXE proxy-DHCP state and links
    to /ui/settings. Three flavours: inactive (default in test env),
    active+healthy, active+nic-gone."""
    from unittest.mock import patch

    from bty.web._sysconfig import Interface, PxeConfig

    _login(client)

    # Inactive: no config file means the "inactive" flavour.
    r = client.get("/ui/dashboard")
    assert r.status_code == 200
    assert "PXE inactive" in r.text
    assert 'href="/ui/settings"' in r.text

    # Active + NIC present: green tile, interface + subnet visible.
    with (
        patch(
            "bty.web._sysconfig.pxe_active",
            return_value=PxeConfig(interface="eth0", subnet="192.168.1.0"),
        ),
        patch(
            "bty.web._sysconfig.list_interfaces",
            return_value=[Interface(name="eth0", operstate="up", ipv4="192.168.1.5", prefix=24)],
        ),
    ):
        r = client.get("/ui/dashboard")
    assert r.status_code == 200
    assert "PXE active" in r.text
    assert "<code>eth0</code>" in r.text
    assert "192.168.1.0" in r.text

    # Active + NIC gone: yellow warning tile.
    with (
        patch(
            "bty.web._sysconfig.pxe_active",
            return_value=PxeConfig(interface="usb0", subnet="192.168.50.0"),
        ),
        patch(
            "bty.web._sysconfig.list_interfaces",
            return_value=[Interface(name="eth0", operstate="up")],
        ),
    ):
        r = client.get("/ui/dashboard")
    assert r.status_code == 200
    assert "PXE: NIC gone" in r.text
    assert "<code>usb0</code>" in r.text


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
    _login(client)
    r = client.post(
        "/ui/machines/aa:bb:cc:dd:ee:ff",
        data={
            "bty_image_ref": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
            "hostname": "",
            "boot_policy": "flash",
        },
    )
    assert r.status_code == 303
    api = client.get(
        "/machines/aa:bb:cc:dd:ee:ff",
        cookies=AUTH,
    )
    assert api.json()["boot_policy"] == "flash"


def test_ui_machine_upsert_rejects_unknown_boot_policy(client: TestClient) -> None:
    """v0.7.32 routed form upsert through Pydantic ``MachineUpsert``;
    invalid ``boot_policy`` now produces a 303 with an error flash
    instead of a 400 page. The previous 400-from-explicit-set-check
    contract was a worse UX (lost form context, no flash); the new
    bounce-back-with-flash matches the catalog-form pattern."""
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
    # v0.7.24 swapped the synchronous form-post for an
    # HTMX-style background trigger; the page now wires a
    # button that POSTs to /boot/releases (the trackable
    # release-fetch endpoint) and polls /boot/releases for
    # progress. Pin both shapes.
    assert 'id="enqueue-fetch-btn"' in body
    assert "/boot/releases" in body
    # v0.7.25: the boot-page polling JS used to (a) reference a
    # never-set ``_just_completed_marker`` field and (b) wrap
    # ``refresh`` to do a second ``fetch /boot/releases`` per
    # poll cycle, doubling load. Both were folded out -- pin the
    # cleaned-up shape so a future copy-paste doesn't reintroduce
    # them.
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


def test_ui_settings_page_renders(client: TestClient) -> None:
    _login(client)
    r = client.get("/ui/settings")
    assert r.status_code == 200
    body = r.text
    # Settings page advertises both the auth panel + the PXE panel.
    assert "Authentication" in body
    assert "passwd" in body  # the "rotate the OS password" hint
    assert "PXE proxy-DHCP" in body
    # Only one form on the Settings page now (PXE activate); the
    # SessionMiddleware swap removed the Revoke-sessions card since
    # invalidation now happens via secret-key rotation, not a button.
    assert 'action="/ui/settings/pxe-activate"' in body


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


def test_ui_settings_pxe_activate_failure_preserves_form_input(client: TestClient) -> None:
    """On activate-failure the operator's typed values survive the
    re-render so they can fix the typo instead of starting over."""
    from unittest.mock import patch

    from bty.web._sysconfig import SysConfigError

    _login(client)
    with patch(
        "bty.web._sysconfig.activate_pxe",
        side_effect=SysConfigError("invalid subnet"),
    ):
        r = client.post(
            "/ui/settings/pxe-activate",
            data={"interface": "eth0", "subnet": "10.20.30.0/garbage"},
        )
    assert r.status_code == 200
    # Subnet input keeps the typed-but-bad value (not reset to the
    # auto-detected subnet of the first interface).
    assert 'value="10.20.30.0/garbage"' in r.text


def test_ui_settings_pxe_panel_warns_when_configured_iface_is_gone(client: TestClient) -> None:
    """When dnsmasq is bound to a NIC that no longer exists (renamed
    across reboot, USB ethernet unplugged), the Settings panel shows
    the warning state, not the success state."""
    from unittest.mock import patch

    from bty.web._sysconfig import Interface, PxeConfig

    _login(client)
    with (
        patch(
            "bty.web._sysconfig.pxe_active",
            return_value=PxeConfig(interface="usb0", subnet="192.168.50.0"),
        ),
        patch(
            "bty.web._sysconfig.list_interfaces",
            return_value=[Interface(name="eth0", operstate="up", ipv4="10.0.0.5", prefix=24)],
        ),
    ):
        r = client.get("/ui/settings")
    assert r.status_code == 200
    body = r.text
    assert "not currently present" in body
    # The configured-but-gone NIC name is shown so the operator
    # can match it against what they previously activated.
    assert "<code>usb0</code>" in body


def test_ui_settings_pxe_deactivate_invokes_helper(client: TestClient) -> None:
    from unittest.mock import patch

    _login(client)
    with patch("bty.web._sysconfig.deactivate_pxe") as mock_deactivate:
        r = client.post("/ui/settings/pxe-deactivate")
    assert r.status_code == 200
    assert "PXE proxy-DHCP deactivated" in r.text
    mock_deactivate.assert_called_once_with()


def test_ui_settings_pxe_deactivate_failure_shows_danger_flash(client: TestClient) -> None:
    from unittest.mock import patch

    from bty.web._sysconfig import SysConfigError

    _login(client)
    with patch(
        "bty.web._sysconfig.deactivate_pxe",
        side_effect=SysConfigError("helper missing"),
    ):
        r = client.post("/ui/settings/pxe-deactivate")
    assert r.status_code == 200
    assert "PXE deactivation failed" in r.text


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
