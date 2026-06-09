"""Tests for the bty-web browser UI.

Cookie-based auth flow, server-rendered pages via TestClient. The
fixture sets ``$BTY_ADMIN_PASSWORD`` to enable auth and drives
``POST /ui/login`` once with that password to mint a real session
cookie; tests opt in to the authenticated path via ``cookies=AUTH``
(or call ``_login(client)`` for the sticky form).
"""

from __future__ import annotations

import urllib.parse
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from bty.web._app import create_app
from bty.web._releases import ARTIFACT_NAMES, SHA256_NAME

TEST_SERVICE_USER = "ui-test-user"
TEST_SECRET_KEY = "test-secret-not-for-prod-use"
TEST_PASSWORD = "test-admin-pw"

# Mutated by the fixture so tests calling the API with
# ``cookies=AUTH`` get the cookie they need.
AUTH: dict[str, str] = {}


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("BTY_ADMIN_PASSWORD", TEST_PASSWORD)
    image_root = tmp_path / "images"
    image_root.mkdir()
    (image_root / "demo.qcow2").write_bytes(b"\0" * 16)
    state = tmp_path / "state.db"
    app = create_app(
        state_path=state,
        service_user=TEST_SERVICE_USER,
        secret_key=TEST_SECRET_KEY,
    )
    app.state.state_path = state  # let tests seed the DB directly

    # ``follow_redirects=False`` so we can assert on 303 hops.
    with TestClient(app, follow_redirects=False) as c:
        # Drive /ui/login once with the admin password so we have a
        # real session cookie value tests can re-attach via
        # ``cookies=AUTH``. Don't leave it sticky on the client -
        # tests opt in by passing ``cookies=AUTH`` (matches the
        # ``_login(client)`` helper below for tests that want the
        # sticky form).
        r = c.post("/ui/login", data={"password": TEST_PASSWORD}, follow_redirects=False)
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
    # Form prompts for the single admin password (``$BTY_ADMIN_PASSWORD``);
    # there is no username field.
    assert 'name="password"' in r.text
    assert "BTY_ADMIN_PASSWORD" in r.text


def test_ui_login_invalid_password_re_renders_with_error(client: TestClient) -> None:
    r = client.post("/ui/login", data={"password": "wrong"})
    assert r.status_code == 200
    assert "Invalid password" in r.text
    assert "bty-token" not in client.cookies


def test_ui_login_valid_password_sets_cookie_and_redirects(client: TestClient) -> None:
    r = client.post("/ui/login", data={"password": TEST_PASSWORD})
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
    """Bare ``GET /ui/dashboard`` after auth renders the three
    surfaces a logged-in operator expects to see: the counter
    tiles, the sanity-check card, and the recent-activity card.
    The "Dashboard" string previously asserted here only matched
    the ``<title>`` tag (since the dashboard nav-btn was dropped
    in favour of the brand pill); pin the actual content instead.
    """
    _login(client)
    r = client.get("/ui/dashboard")
    assert r.status_code == 200
    body = r.text
    # Live panels render (SSE-swapped Machine Summary + Images).
    assert 'sse-connect="/events/machines"' in body
    assert "Machine Summary" in body
    assert "Images" in body
    # Health Monitoring panel (renamed from "Sanity checklist").
    assert "Health Monitoring" in body
    # Recent-activity card title.
    assert "Recent Events" in body
    # Navbar still carries the Machines nav-btn.
    assert 'href="/ui/machines">' in body
    assert "Machines" in body


def test_ui_images_handles_empty_release_repo_env(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``BTY_BOOT_RELEASE_REPO=`` (empty string) used to produce a
    broken release link on /ui/images because the dict-default
    fallback only fires on absent keys, not empty values. The
    ``or DEFAULT_REPO`` pattern handles both."""
    monkeypatch.setenv("BTY_BOOT_RELEASE_REPO", "")
    _login(client)
    # The Fetch control now lives in the Catalog table header on the
    # default list view (the dropped ``?section=fetch`` page).
    r = client.get("/ui/images")
    assert r.status_code == 200
    body = r.text
    # The fallback ``safl/bty`` repo appears in the Fetch button's
    # title ("... from safl/bty ...") + the fetch-release form action.
    assert "safl/bty" in body
    assert 'action="/ui/catalog/fetch-release"' in body


def test_ui_dashboard_renders_with_zero_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fresh appliance with no machines + no images must not 500
    the dashboard. Guards the count-cards rendering against
    division-by-zero / off-by-one regressions if someone tries to
    compute a percentage from machine_count or image_count."""
    # Spin up a brand-new app with NOTHING in either root so the
    # zero-state path renders.
    state = tmp_path / "state.db"
    image_root = tmp_path / "images"
    image_root.mkdir()
    bty_state_dir = tmp_path / "bty-state"
    bty_state_dir.mkdir()
    monkeypatch.setenv("BTY_STATE_DIR", str(bty_state_dir))
    monkeypatch.setenv("BTY_ADMIN_PASSWORD", TEST_PASSWORD)
    fresh_app = create_app(
        state_path=state,
        service_user=TEST_SERVICE_USER,
        secret_key=TEST_SECRET_KEY,
    )

    with TestClient(fresh_app, follow_redirects=False) as c:
        login = c.post("/ui/login", data={"password": TEST_PASSWORD})
        assert login.status_code == 303
        r = c.get("/ui/dashboard")
        assert r.status_code == 200
        body = r.text
        # Counts should show 0 (or the literal "0" character).
        assert "Dashboard" in body
        # Empty-state rendering -- the page must still surface the
        # primary nav so the operator can act.
        assert 'href="/ui/machines"' in body
        assert 'href="/ui/images"' in body


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
    assert "Recent Events" in body
    assert "machine.discovered" in body
    assert 'href="/ui/events"' in body


def test_ui_dashboard_subscribes_to_sse_for_live_counts(client: TestClient) -> None:
    """The live panels need ``sse-connect`` on the row + a per-panel
    ``sse-swap`` so the htmx-ext-sse client routes the
    ``dashboard-machine`` / ``dashboard-images`` events to the right
    column - that's what makes the dashboard a *dashboard* and not a
    snapshot. The Machine Summary + Images panels are independent
    swap targets (not one bundled fragment) so they sit as separate,
    equally-spaced columns."""
    _login(client)
    r = client.get("/ui/dashboard")
    assert r.status_code == 200
    body = r.text
    assert 'sse-connect="/events/machines"' in body
    assert 'sse-swap="dashboard-machine"' in body
    assert 'sse-swap="dashboard-images"' in body


def test_ui_dashboard_health_monitoring_renders_with_links(client: TestClient) -> None:
    """The dashboard Health Monitoring panel (was "Sanity
    checklist") is the operator's fresh-install onboarding +
    at-a-glance status surface: one row per readiness condition,
    each linked into the remediation section when it fails. Pin the
    four pass/fail rows (Netboot artifacts / Catalog non-empty / TFTP
    daemon / No unacknowledged errors) plus the advisory dedicated-disk
    info row so a future refactor doesn't drop one.
    """
    import re

    _login(client)
    body = client.get("/ui/dashboard").text
    assert "Health Monitoring" in body
    # Each row's label.
    assert "Netboot artifacts present" in body
    assert "Catalog is non-empty" in body
    assert "TFTP daemon running" in body
    assert "No unacknowledged errors" in body
    # The dedicated-disk advisory renders as an INFO row (an "i",
    # never a red cross) -- recommended-not-required.
    assert "State on a dedicated disk" in body
    assert "bi-info-circle-fill" in body
    # The "N / 4 OK" header counts only the four pass/fail rows --
    # the info row is excluded from the OK tally. (The actual N
    # depends on the fixture's pre-seeded state; we don't pin it.)
    assert re.search(r"\d+ / 4 OK", body), (
        "health-monitoring header should render an 'N / 4 OK' summary "
        "over the four pass/fail rows (info row excluded)"
    )
    # The two always-failing rows in the bare-fixture state
    # (no netboot artifacts, no live TFTP daemon) carry fix links;
    # both point at the Netboot list view (the Fetch control + the
    # TFTP daemon panel both live there now).
    assert 'href="/ui/netboot"' in body
    # Both visual indicators render on the bare fixture: green
    # tick on the catalog row (the fixture seeds demo.qcow2 so
    # ``Catalog is non-empty`` passes), red x on the other two.
    # Pinned together so a future "text-only checklist" refactor
    # would fail CI.
    assert "bi-check-circle-fill" in body
    assert "bi-x-circle-fill" in body


def test_ui_dashboard_state_row_green_when_migrated_and_valid(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The dedicated-disk row turns into a counted green check (not a
    blue info) once the state dir is a mount that holds the live DB +
    image/netboot roots. The fixture puts all three under ``tmp_path``,
    so faking ``os.path.ismount`` for that dir flips the row to valid.
    """
    import os
    import re

    real = os.path.ismount
    monkeypatch.setattr(
        "os.path.ismount",
        lambda p: True if os.fspath(p) == os.fspath(tmp_path) else real(p),
    )
    _login(client)
    body = client.get("/ui/dashboard").text
    # Green-state detail text, and the count now includes this row
    # (5 pass/fail rows) rather than excluding it as advisory.
    assert "all live on it, so they" in body
    assert re.search(r"\d+ / 5 OK", body)


# ---------------------------------------------------------------------
# Sub-nav (v0.22.11): /ui/images, /ui/netboot, /ui/machines each have a
# sub-nav strip that splits "what's there" (list, the default
# landing) from "how to add/fetch more". The action paths are one
# click away; the default landing stays a clean read view.
# ---------------------------------------------------------------------


def test_ui_images_is_catalog_listing_only(client: TestClient) -> None:
    """``/ui/images`` is the catalog listing plus three add-paths in
    the header: Add image (URL), Upload catalog, Fetch latest catalog."""
    _login(client)
    body = client.get("/ui/images").text
    assert 'aria-label="Section sub-navigation"' in body
    assert 'href="#images-list"' in body
    assert 'href="#images-activity"' in body
    # Catalog actions in the list header: all three add-paths live here.
    assert 'action="/ui/catalog/entries"' in body
    assert 'action="/ui/catalog/fetch-release"' in body
    assert 'action="/ui/catalog/upload"' in body
    assert 'accept=".toml"' in body
    assert 'id="image_url"' in body
    # No live job tables on this page (those live on /ui/netboot for
    # release fetches and /ui/backups for backups).
    assert "bty-downloads-tbody" not in body
    assert "bty-hashes-tbody" not in body


def test_worker_pages_exist_separately(client: TestClient) -> None:
    """v0.41.2+: ``/ui/backups`` is the only standalone worker page.
    The release-fetch workload moved into ``/ui/netboot``; the merged
    ``/ui/workers`` page and the legacy ``/ui/downloads`` / ``/ui/hashing``
    / ``/ui/fetches`` / ``/ui/hashes`` routes all return 404."""
    _login(client)
    assert client.get("/ui/backups").status_code == 200
    assert client.get("/ui/netboot").status_code == 200
    # Removed routes.
    for legacy in (
        "/ui/workers",
        "/ui/downloads",
        "/ui/hashes",
        "/ui/hashing",
        "/ui/fetches",
    ):
        assert client.get(legacy).status_code == 404, legacy


def test_ui_images_list_header_has_all_three_add_paths(client: TestClient) -> None:
    """The Images table header carries the three add-paths in
    order: Add image (URL), Upload catalog, Fetch latest catalog."""
    _login(client)
    body = client.get("/ui/images").text
    # All three forms.
    assert 'action="/ui/catalog/entries"' in body
    assert 'id="image_url"' in body
    assert "Add image" in body
    assert 'action="/ui/catalog/upload"' in body
    assert "Upload catalog" in body
    assert 'accept=".toml"' in body
    assert 'action="/ui/catalog/fetch-release"' in body
    assert "Fetch latest catalog" in body
    # Ordering: Add image is first, then Upload catalog, then Fetch.
    pos_add = body.index('action="/ui/catalog/entries"')
    pos_upload = body.index('action="/ui/catalog/upload"')
    pos_fetch = body.index('action="/ui/catalog/fetch-release"')
    assert pos_add < pos_upload < pos_fetch


def test_top_level_nav_highlights_active_page(client: TestClient) -> None:
    """The top-bar nav (Machines / Images / Netboot / Events) marks
    the current page with the ``active`` class so the operator can
    see where they are. The Python side derives ``nav_active`` from
    ``request.url.path.split("/")[2]``; the template applies the
    class via a Jinja ``{% if nav_active == 'images' %}active{%
    endif %}`` pattern. Each page must light up its own entry and
    ONLY its own.

    Dashboard is NOT a separate nav-btn -- the BTY brand pill
    doubles as the dashboard link and carries the ``brand-active``
    class instead (checked below). The operator Account page sits
    behind the user-bar gear icon and is checked via the
    ``user-bar-action active`` class.

    Mirror of the v0.22.11 sub-nav bug (where ``active`` was never
    wired through) for the top-level nav -- without this test the
    same drift could happen and go unnoticed.
    """
    import re

    _login(client)
    page_to_nav_key = {
        # /ui/dashboard's highlight lives on the brand pill, not in
        # the nav-btn cluster -- tested separately below.
        "/ui/machines": "/ui/machines",
        "/ui/images": "/ui/images",
        "/ui/netboot": "/ui/netboot",
        "/ui/events": "/ui/events",
        "/ui/settings": "/ui/settings",
        # /ui/account sits behind the user-bar gear icon; its
        # highlight uses the ``user-bar-action active`` class
        # (tested below).
    }
    for path, expected_href in page_to_nav_key.items():
        body = client.get(path).text
        actives = [
            href
            for cls, href in re.findall(r'<a class="nav-btn ([^"]*)" href="([^"]+)"', body)
            if "active" in cls
        ]
        assert actives == [expected_href], (
            f"{path}: expected top-bar nav to highlight {expected_href!r} only, got {actives!r}"
        )
    # /ui/dashboard's brand pill carries the active state.
    body = client.get("/ui/dashboard").text
    assert "brand-active" in body, (
        "/ui/dashboard should mark the BTY brand pill as active "
        "(the brand doubles as the dashboard nav link)"
    )
    # /ui/account highlights the operator pill (the name doubles as the
    # account link, carrying ``user-bar-action active``).
    body = client.get("/ui/account").text
    assert "user-bar-action active" in body, (
        "/ui/account page should mark the operator pill as active"
    )
    assert 'href="/ui/account"' in body


def test_subnavs_drop_the_redundant_list_pill(client: TestClient) -> None:
    """The per-page "List" pill was dropped as redundant. The single-view
    pages keep the thin sub-nav strip as chrome but carry no section
    pills (so no ``aria-current`` markers). Settings is the one page with
    a real sub-nav: in-page section-jump links separated by rules.
    """
    import re

    _login(client)

    def _aria_current_hrefs(body: str) -> list[str]:
        """Return the ``href`` of every <a> bearing ``aria-current``."""
        out: list[str] = []
        for m in re.finditer(r'<a\b[^>]*\baria-current="page"[^>]*>', body, flags=re.DOTALL):
            href = re.search(r'\bhref="([^"]+)"', m.group(0))
            if href:
                out.append(href.group(1))
        return out

    # Every content page carries the section-jump sub-nav strip with
    # in-page anchor links (no aria-current pills -- those were the old
    # ?section= page links). Release-fetch UI moved onto /ui/netboot in
    # v0.41.2; Backups is the only standalone worker page now.
    for path in (
        "/ui/images",
        "/ui/netboot",
        "/ui/machines",
        "/ui/events",
        "/ui/backups",
        "/ui/dashboard",
    ):
        body = client.get(path).text
        assert 'aria-label="Section sub-navigation"' in body, path
        assert _aria_current_hrefs(body) == [], path

    # The Backups page lights ONLY its own navbar indicator.
    assert client.get("/ui/backups").text.count("nav-worker active") == 1

    # Settings carries its own section-jump sub-nav (anchor links + rules).
    settings = client.get("/ui/settings").text
    assert 'aria-label="Settings sections"' in settings
    assert 'href="#upstream-sources"' in settings
    assert 'href="#dhcp-pxe"' in settings


def test_ui_images_ignores_unknown_query_params(client: TestClient) -> None:
    """``?section=...`` was the old per-tab selector. With the page
    merged into the simple catalog list + three add-paths, query
    params are ignored: a bookmark / typo / scripted call must NOT
    500 the page."""
    _login(client)
    r = client.get("/ui/images?section=garbage")
    assert r.status_code == 200
    body = r.text
    # The header add-form is still rendered.
    assert 'id="image_url"' in body
    assert 'aria-label="Section sub-navigation"' in body


def test_ui_netboot_has_fetch_trigger_and_jobs_tbody(client: TestClient) -> None:
    """v0.41.2+: ``/ui/netboot`` hosts both the artifacts inventory AND
    the release-fetch workload (Fetch artifacts button + active-jobs
    tbody + the polling JS). The old ``/ui/downloads`` page is gone."""
    _login(client)
    r = client.get("/ui/netboot")
    assert r.status_code == 200
    body = r.text
    assert 'aria-label="Section sub-navigation"' in body
    assert "<th>File</th>" in body
    # Release-fetch trigger + the active-jobs tbody both live here now.
    assert 'id="bty-downloads-fetch-artifacts-btn"' in body
    assert "bty-workers-downloads-tbody" in body
    # The legacy /ui/downloads route is gone -- no link should still
    # point at it from the netboot page.
    assert 'href="/ui/downloads"' not in body


def test_ui_backups_has_back_up_now_and_activity(client: TestClient) -> None:
    """``/ui/backups`` carries the Back-up-now trigger, the active
    backups tbody, the on-disk listing card, schedule summary, and
    the recent backup events."""
    _login(client)
    body = client.get("/ui/backups").text
    assert 'id="bty-workers-backup-now"' in body
    assert "bty-workers-backup-tbody" in body
    assert 'id="backups-on-disk"' in body
    assert 'id="backups-activity"' in body
    # Schedule summary references the Settings knob.
    assert 'href="/ui/settings#backup-schedule"' in body
    # Subnav exposes a jump to the on-disk listing.
    assert 'href="#backups-on-disk"' in body
    # Empty-state copy points the operator at the trigger / schedule.
    assert "No backups on disk yet" in body


def test_ui_backups_lists_existing_bundles(client: TestClient, tmp_path: Path) -> None:
    """When ``backups_root`` contains bundles, ``/ui/backups`` renders a
    row per bundle with its backup_id + machine / catalog / image
    counts. Each row carries a Download link to the streaming-tar
    endpoint. The empty-state copy is gone.

    The ``client`` fixture's state.db is at ``tmp_path/state.db`` and
    the app derives ``backups_root`` as ``state.db.parent / "backups"``
    when ``BTY_BACKUP_DIR`` is unset -- so a bundle dropped into
    ``tmp_path/backups`` shows up without any patching."""
    import json

    backups_root = tmp_path / "backups"
    bundle = backups_root / "2026-05-23T10-00-00Z"
    bundle.mkdir(parents=True)
    (bundle / "inventory.json").write_text(
        json.dumps(
            {
                "bty_export_version": 3,
                "exported_at": "2026-05-23T10:00:00+00:00",
                "exported_by_bty_version": "0.33.2",
                "machines": [{"mac": "aa:bb:cc:dd:ee:01"}],
            }
        )
    )

    _login(client)
    body = client.get("/ui/backups").text
    assert "2026-05-23T10-00-00Z" in body
    assert "0.33.2" in body
    assert "No backups on disk yet" not in body
    # Retention number lands in the schedule summary, regardless of
    # whether the schedule itself is on/off -- it always applies on
    # successful completion.
    assert "Retention:" in body
    assert "keep last 7" in body  # default retention is 7
    # Each on-disk row carries a download link to the streaming-tar
    # endpoint for that specific bundle.
    assert 'href="/ui/backups/2026-05-23T10-00-00Z/download"' in body


def test_ui_backups_download_serves_inventory_json(client: TestClient, tmp_path: Path) -> None:
    """``GET /ui/backups/{id}/download`` serves the bundle's
    ``inventory.json`` directly as ``application/json``, with a
    ``Content-Disposition: attachment; filename="<id>.json"`` so the
    operator's browser saves it with a self-describing name. v3
    bundles are one file -- no tar wrapping."""
    import json as _json

    backups_root = tmp_path / "backups"
    bundle = backups_root / "2026-05-23T10-00-00Z"
    bundle.mkdir(parents=True)
    inventory = {"bty_export_version": 3, "machines": [{"mac": "aa:bb:cc:dd:ee:01"}]}
    (bundle / "inventory.json").write_text(_json.dumps(inventory) + "\n")

    _login(client)
    r = client.get("/ui/backups/2026-05-23T10-00-00Z/download")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json")
    assert "2026-05-23T10-00-00Z.json" in r.headers["content-disposition"]
    assert r.json() == inventory


def test_ui_backups_download_rejects_invalid_id(client: TestClient) -> None:
    """A backup_id that isn't an ISO-8601 slug returns 404 -- a
    path-traversal attempt never reaches the filesystem."""
    _login(client)
    for bad in ("..", "not-a-date", "2026"):
        r = client.get(f"/ui/backups/{bad}/download")
        assert r.status_code == 404, bad


def test_ui_backups_download_missing_bundle_404(client: TestClient) -> None:
    """A correctly-shaped backup_id whose directory doesn't exist
    returns 404 (not 500). Same 404 as the malformed-id case so the
    operator can't enumerate other operators' bundle ids."""
    _login(client)
    r = client.get("/ui/backups/2026-05-23T10-00-00Z/download")
    assert r.status_code == 404


def test_ui_backups_delete_removes_bundle_and_logs(client: TestClient, tmp_path: Path) -> None:
    """``DELETE /ui/backups/{id}`` rmtree's the bundle, returns the
    snapshotted counts, and the next page render shows the row gone."""
    import json

    backups_root = tmp_path / "backups"
    bundle = backups_root / "2026-05-23T10-00-00Z"
    bundle.mkdir(parents=True)
    (bundle / "inventory.json").write_text(
        json.dumps(
            {
                "bty_export_version": 3,
                "machines": [{"mac": "aa:bb:cc:dd:ee:01"}],
            }
        )
    )

    _login(client)
    # Pre-check: the row is there.
    body = client.get("/ui/backups").text
    assert "2026-05-23T10-00-00Z" in body
    assert 'class="btn-group btn-group-sm"' in body  # action group rendered
    assert "bty-backups-delete" in body

    r = client.delete("/ui/backups/2026-05-23T10-00-00Z")
    assert r.status_code == 200
    payload = r.json()
    assert payload["backup_id"] == "2026-05-23T10-00-00Z"
    assert payload["machines"] == 1

    # Bundle is gone from disk + the on-disk table reverts to its
    # empty state. (The backup_id will still appear in the Activity
    # card as part of the ``backup.deleted`` event summary, so we
    # assert via the empty-state copy rather than absence of the id.)
    assert not bundle.exists()
    body = client.get("/ui/backups").text
    assert "No backups on disk yet" in body


def test_ui_backups_delete_rejects_invalid_id(client: TestClient) -> None:
    """A delete against a malformed slug returns 404 -- the validator
    runs before any filesystem access. (URL ``..`` is normalised away
    by Starlette before the route sees it, so the path-traversal
    coverage uses slug-shaped strings that actually reach the
    validator.)"""
    _login(client)
    for bad in ("not-a-date", "2026", "2026-05-23T99-99-99Z", "etc"):
        r = client.delete(f"/ui/backups/{bad}")
        assert r.status_code == 404, bad


def test_ui_backups_delete_missing_bundle_404(client: TestClient) -> None:
    """A well-shaped id whose bundle doesn't exist returns 404."""
    _login(client)
    r = client.delete("/ui/backups/2026-05-23T10-00-00Z")
    assert r.status_code == 404


def test_ui_backups_page_auto_reload_on_completion(client: TestClient) -> None:
    """The Backups page polling JS reloads on the active-count
    transition (>0 -> 0), so the on-disk + Recent activity cards
    update on their own when a backup finishes. We can't drive the
    JS in this hermetic test; just assert the source carries the
    transition trigger so we won't silently regress to "operator
    must reload manually"."""
    _login(client)
    body = client.get("/ui/backups").text
    # Closure variable + the reload trigger live together in the
    # refresh() function; assert both shapes appear.
    assert "lastActiveCount" in body
    assert "window.location.reload" in body


def test_ui_settings_shows_dhcp_pxe_cheatsheet(client: TestClient) -> None:
    """The DHCP / Network-boot router-config cheatsheet on the Settings
    page renders BOTH net-boot paths: PXE-via-TFTP (60 PXEClient / 66
    next-server / 67 bootfile / 67 for user-class=iPXE) and UEFI HTTP
    Boot (60 HTTPClient / 67 full http URL to /boot/ipxe.efi)."""
    _login(client)
    r = client.get("/ui/settings")
    assert r.status_code == 200
    body = r.text
    assert "DHCP / Network boot" in body
    assert "Router-side configuration" in body
    # PXE-via-TFTP path.
    assert "option 60" in body
    assert "PXEClient" in body
    assert "option 66" in body
    assert "option 67" in body
    assert "pxe-bootstrap.ipxe" in body
    # UEFI HTTP Boot path.
    assert "HTTP Boot" in body
    assert "HTTPClient" in body
    assert "/boot/ipxe.efi" in body


def test_ui_boot_shows_tftp_daemon_status(client: TestClient) -> None:
    """The Netboot page surfaces the dnsmasq.service status as a
    pure observation -- no Start/Stop/Restart buttons (lifecycle is
    a systemd/Podman concern, not an operator click target)."""
    _login(client)
    r = client.get("/ui/netboot")
    assert r.status_code == 200
    body = r.text
    assert "TFTP daemon" in body
    assert "dnsmasq.service" in body
    # No control surface: the form + buttons are gone.
    assert 'action="/ui/settings/tftp-control"' not in body
    assert "Start</button>" not in body
    assert "Stop</button>" not in body
    assert "Restart</button>" not in body
    # The router cheatsheet is on Settings now, not here.
    assert "Router-side configuration" not in body


def test_ui_layout_renders_top_level_live_indicator(client: TestClient) -> None:
    """The navbar carries a top-level live indicator (after the worker
    cluster, behind a divider) plus the poll-driven setLive logic that
    flips it green/red and pulses it on activity."""
    _login(client)
    body = client.get("/ui/dashboard").text
    assert 'id="nav-live"' in body
    assert "nav-live-sep" in body  # divider after the backups indicator
    assert "function setLive" in body
    # The poller targets the only surviving navbar-tracked worker
    # endpoint (release-fetches now live inline on /ui/netboot).
    assert "/workers/backups" in body


def test_ui_layout_renders_version_in_navbar_outside_brand(client: TestClient) -> None:
    """The navbar carries an always-visible ``v{__version__}`` slug
    sitting OUTSIDE the brand ``<a>`` so the BTY pill stays a clean
    click target. Pin both invariants:
    * The slug renders with the ``v`` prefix on every authed page.
    * It's a sibling of the brand link (``class="navbar-version"``),
      not nested inside it.
    """
    import re

    import bty

    _login(client)
    body = client.get("/ui/dashboard").text
    # Always-visible version with the ``v`` prefix.
    assert f"v{bty.__version__}" in body
    # The version <span> carries the navbar-version class (may sit
    # alongside utility classes like ``me-2``).
    assert re.search(r'<span class="navbar-version[^"]*">', body), (
        'version slug should render as a <span class="navbar-version ...">'
    )
    # The navbar-version <span> must NOT be inside the navbar-brand
    # <a>: the brand needs to stay a single clean click target.
    brand_match = re.search(
        r'<a class="navbar-brand[^"]*"[^>]*>(.*?)</a>',
        body,
        re.DOTALL,
    )
    assert brand_match, "navbar-brand <a> should render"
    assert "navbar-version" not in brand_match.group(1), (
        "navbar-version must be a sibling of the brand <a>, not nested inside it"
    )


def test_ui_boot_section_unrecognised_falls_back_to_list(client: TestClient) -> None:
    """A bookmark / typo / scripted call with a bogus ``?section=``
    value must NOT 500 the page. The server clamps to the default
    ``list`` section. Symmetric with the /ui/images fallback test.
    """
    _login(client)
    r = client.get("/ui/netboot?section=garbage")
    assert r.status_code == 200
    body = r.text
    # Lands on list: the artifacts inventory (the Fetch trigger moved
    # to the Release fetches page).
    assert "<th>File</th>" in body
    assert ARTIFACT_NAMES[0] in body
    # Sub-nav strip still renders (just the List pill now; DHCP / PXE
    # moved to Settings and TFTP folded into this view).
    assert 'aria-label="Section sub-navigation"' in body
    assert 'href="/ui/netboot?section=dhcp-pxe"' not in body


def test_ui_machines_list_has_inline_add_form(client: TestClient) -> None:
    """Bare ``GET /ui/machines`` shows the live table with the
    minimal add-by-MAC field inline in the table header (the
    standalone ?section=add page was dropped)."""
    _login(client)
    r = client.get("/ui/machines")
    assert r.status_code == 200
    body = r.text
    # Inline add-by-MAC field is present in the list header.
    assert 'id="add-mac"' in body
    # Live machines table is present.
    assert 'id="machines-tbody"' in body
    # The standalone add sub-nav pill is gone.
    assert 'href="/ui/machines?section=add"' not in body


def test_ui_machines_inline_add_defaults_to_safe_sanboot_mode(client: TestClient) -> None:
    """The minimal inline add field stages a row by MAC only and
    submits ``boot_mode=ipxe-exit`` -- never a flash policy (which
    would need a target_disk_serial the box only reports after its
    first PXE check-in). Image binding + policy are set on the
    detail page.
    """
    _login(client)
    body = client.get("/ui/machines").text
    # The submit JS hardcodes the safe default.
    assert '"boot_mode", "ipxe-exit"' in body
    # No flash policy is offered/sent from the inline add.
    assert '"boot_mode", "bty-flash-always"' not in body


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
    # Scope to the list table (the unfiltered "Activity" card below can
    # mention any MAC's discovery event).
    list_part = r.text.split('id="machines-activity"')[0]
    assert "aa:bb:cc:dd:ee:04" in list_part
    assert "aa:bb:cc:dd:ee:03" not in list_part


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
    # Scope to the list table (the unfiltered "Activity" card below can
    # mention any MAC's create/discovery event).
    list_part = body.split('id="machines-activity"')[0]
    assert "aa:bb:cc:dd:ee:01" in list_part
    assert "aa:bb:cc:dd:ee:02" not in list_part
    # Under a server-side filter the SSE wiring is suppressed (a live
    # update would replace the filtered tbody with the full list).
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


def test_ui_machine_detail_shows_bound_image_and_hostname(client: TestClient) -> None:
    """The identity card shows the bound image human-readably (name +
    format + human size) linked to the catalog, and the hostname next to
    the MAC -- not a bare cut-off ref/sha."""
    from bty.web import _db as _bty_db

    _login(client)
    state_path = client.app.state.state_path  # type: ignore[attr-defined]
    ref = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
    with _bty_db.open_db(state_path) as conn:
        conn.execute(
            "INSERT INTO catalog_entries "
            "(bty_image_ref, src, disk_image_sha, name, sha_url, format, "
            "size_bytes, description, added_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                ref,
                "oras://ghcr.io/safl/nosi/fedora:latest",
                None,
                "nosi fedora-sysdev (x86_64, rolling)",
                None,
                "img.gz",
                2_606_593_716,
                None,
                "2026-05-22T00:00:00+00:00",
            ),
        )
        conn.commit()
    client.put(
        "/machines/aa:bb:cc:dd:ee:ff",
        json={"bty_image_ref": ref, "hostname": "lab-fedora-01"},
        cookies=AUTH,
    )
    body = client.get("/ui/machines/aa:bb:cc:dd:ee:ff").text
    assert "nosi fedora-sysdev (x86_64, rolling)" in body  # human name
    assert 'href="/ui/images"' in body  # links to the catalog
    assert "GiB" in body  # 2.6e9 bytes -> filesizeformat "2.4 GiB"
    assert "lab-fedora-01" in body  # hostname next to the MAC


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
    # The operator action is audited (debuggability) like the JSON path.
    events = client.get(
        "/events",
        params={"subject_kind": "catalog", "kind": "catalog.entry.added"},
        cookies=AUTH,
    ).json()["events"]
    assert any("charlie.img.gz" in e["summary"] for e in events)


def test_ui_catalog_entry_form_oras_branch(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The oras:// branch of /ui/catalog/entries: resolve_ref is
    called server-side; digest, name, format, size_bytes come from
    the manifest; row inserts with the resolved metadata. Mirrors
    the JSON ``POST /catalog/entries`` oras path but exercises the
    Form-based UI handler (913-975 of _ui.py was the uncovered
    block)."""
    from bty import oras as _oras

    fake_blob = _oras.ResolvedBlob(
        blob_url="https://ghcr.io/v2/safl/nosi/blobs/sha256:" + "a" * 64,
        headers={"Authorization": "Bearer fake"},
        digest="sha256:" + "a" * 64,
        size=12345,
        title="nosi-debian-sysdev.img.gz",
    )
    monkeypatch.setattr(_oras, "resolve_ref", lambda url: fake_blob)
    _login(client)

    r = client.post(
        "/ui/catalog/entries",
        data={"image_url": "oras://ghcr.io/safl/nosi/debian-sysdev:latest"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/ui/images"

    entries = client.get("/catalog/entries", cookies=AUTH).json()
    oras_row = next(
        (e for e in entries if e["src"] == "oras://ghcr.io/safl/nosi/debian-sysdev:latest"),
        None,
    )
    assert oras_row is not None
    assert oras_row["disk_image_sha"] == "a" * 64  # algorithm prefix stripped
    assert oras_row["name"] == "nosi-debian-sysdev.img.gz"
    assert oras_row["format"] == "img.gz"
    assert oras_row["size_bytes"] == 12345


def test_ui_catalog_entry_form_oras_resolve_failure(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If ``resolve_ref`` raises (registry unreachable, bad ref),
    the operator gets a 303 back to /ui/images with an ``?error=``
    query param -- NOT a 500 traceback. Pin the error-channel
    shape so a refactor can't silently regress to a wedge."""
    from bty import oras as _oras

    def _boom(url: str) -> _oras.ResolvedBlob:
        raise _oras.OrasError("simulated registry unreachable")

    monkeypatch.setattr(_oras, "resolve_ref", _boom)
    _login(client)

    r = client.post(
        "/ui/catalog/entries",
        data={"image_url": "oras://ghcr.io/bad/ref:latest"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    loc = r.headers["location"]
    assert loc.startswith("/ui/images?error=")
    assert "oras+resolve+failed" in loc or "oras%20resolve%20failed" in loc


def test_ui_catalog_entry_form_oras_duplicate_redirects_with_error(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Submitting the same oras URL twice hits the UNIQUE(src)
    constraint. The handler catches the IntegrityError and
    redirects with ``?error=already+exists`` rather than 500'ing.
    """
    from bty import oras as _oras

    fake_blob = _oras.ResolvedBlob(
        blob_url="x",
        headers={},
        digest="sha256:" + "b" * 64,
        size=10,
        title="dup.img.gz",
    )
    monkeypatch.setattr(_oras, "resolve_ref", lambda url: fake_blob)
    _login(client)

    url = "oras://ghcr.io/safl/dup:latest"
    r1 = client.post("/ui/catalog/entries", data={"image_url": url}, follow_redirects=False)
    assert r1.status_code == 303
    assert r1.headers["location"] == "/ui/images"

    r2 = client.post("/ui/catalog/entries", data={"image_url": url}, follow_redirects=False)
    assert r2.status_code == 303
    assert "already+exists" in r2.headers["location"]


def test_ui_machine_save_is_audited(client: TestClient) -> None:
    """Changing a machine via the browser UI records machine.created /
    machine.upserted, same as the JSON PUT path -- so operator config
    changes are debuggable from /ui/events (they previously weren't)."""
    _login(client)
    mac = "aa:bb:cc:dd:ee:99"
    assert (
        client.post(
            f"/ui/machines/{mac}",
            data={"boot_mode": "bty-inventory"},
            cookies=AUTH,
            follow_redirects=False,
        ).status_code
        == 303
    )
    created = client.get(
        "/events",
        params={"subject_kind": "machine", "subject_id": mac, "kind": "machine.created"},
        cookies=AUTH,
    ).json()["events"]
    assert len(created) == 1
    # A second save is an update -> machine.upserted.
    client.post(
        f"/ui/machines/{mac}",
        data={"boot_mode": "ipxe-exit"},
        cookies=AUTH,
        follow_redirects=False,
    )
    upserted = client.get(
        "/events",
        params={"subject_kind": "machine", "subject_id": mac, "kind": "machine.upserted"},
        cookies=AUTH,
    ).json()["events"]
    assert len(upserted) == 1


def test_ui_events_summary_linkifies_mac(client: TestClient) -> None:
    """The /ui/events Summary column linkifies MACs so an operator can
    jump to the machine a row mentions. The ``{mac} created`` summary
    yields ``...<code>{mac}</code></a> created`` -- the trailing word
    proves the link is inside the summary text, not just the structured
    Subject column (which has no trailing word)."""
    _login(client)
    mac = "aa:bb:cc:dd:ee:42"
    client.post(
        f"/ui/machines/{mac}",
        data={"boot_mode": "ipxe-exit"},
        cookies=AUTH,
        follow_redirects=False,
    )
    body = client.get("/ui/events", cookies=AUTH).text
    assert f'href="/ui/machines/{mac}"' in body
    assert "</code></a> created" in body


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
            "boot_mode": "ipxe-exit",
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
    # Form omits boot_mode -> dependency default applies (sanboot).
    assert api.json()["boot_mode"] == "ipxe-exit"


def test_ui_machine_upsert_invalid_field_gives_concise_banner(client: TestClient) -> None:
    """A bad field (non-hex bty_image_ref) 303s back with a concise
    ``field: message`` banner, not Pydantic's multi-line str() dump
    (which ends in a pydantic.dev URL and reads terribly in an alert)."""
    _login(client)
    r = client.post(
        "/ui/machines/aa:bb:cc:dd:ee:ff",
        data={"bty_image_ref": "not-a-hex-digest!", "boot_mode": "ipxe-exit"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    loc = r.headers["location"]
    assert "?error=" in loc
    decoded = urllib.parse.unquote(loc.split("?error=", 1)[1])
    assert decoded.startswith("validation failed:")
    assert "bty_image_ref" in decoded  # names the offending field
    # Concise: single line, no Pydantic boilerplate / docs URL.
    assert "\n" not in decoded
    assert "pydantic.dev" not in decoded


def test_ui_machine_upsert_flash_without_target_names_the_policy(client: TestClient) -> None:
    """The flash-without-target gate banner names the actual policy the
    operator picked (was hardcoded 'bty-flash-always') and points at the
    Target disk dropdown / bty-inventory auto-report (not the stale
    'set bty-tui mode' guidance)."""
    _login(client)
    r = client.post(
        "/ui/machines/aa:bb:cc:dd:ee:ff",
        data={"boot_mode": "bty-flash-once"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    decoded = urllib.parse.unquote(r.headers["location"].split("?error=", 1)[1])
    assert "boot_mode=bty-flash-once requires a target disk" in decoded
    assert "bty-inventory" in decoded
    assert "bty-tui" not in decoded


def test_ui_machine_upsert_persists_boot_mode_flash(client: TestClient) -> None:
    """Form upsert with boot_mode=flash also requires the operator
    to have picked a target_disk_serial (post-v0.18 safety gate).
    The dropdown is populated from machines.known_disks after
    ``bty`` posts its inventory; this test sends the serial
    directly via form data."""
    _login(client)
    r = client.post(
        "/ui/machines/aa:bb:cc:dd:ee:ff",
        data={
            "bty_image_ref": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
            "hostname": "",
            "boot_mode": "bty-flash-always",
            "target_disk_serial": "ATA-WDC-123456",
        },
    )
    assert r.status_code == 303
    api = client.get(
        "/machines/aa:bb:cc:dd:ee:ff",
        cookies=AUTH,
    )
    assert api.json()["boot_mode"] == "bty-flash-always"
    assert api.json()["target_disk_serial"] == "ATA-WDC-123456"


def test_ui_machine_detail_renders_disk_inventory_dropdown(client: TestClient) -> None:
    """When the machine has ``known_disks`` populated (``bty`` has
    reported in), /ui/machines/{mac} shows a populated <select>
    with one <option> per disk. Each option displays the path /
    size / model / serial so the operator picks a recognisable
    line."""
    _login(client)
    # Discover the machine, then post inventory (mirrors what
    # ``bty`` does on startup).
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
    boot_mode=bty-tui and power-cycle") instead of a broken empty
    dropdown."""
    _login(client)
    # Seed a machine record without ever posting inventory.
    client.put(
        "/machines/aa:bb:cc:dd:ee:89",
        json={"boot_mode": "ipxe-exit"},
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
    unset). Setting boot_mode=flash without target_disk_serial
    bounces back to /ui/machines/{mac} with a flash banner
    explaining how to fix it -- and the machine row does NOT
    flip to boot_mode=flash."""
    _login(client)
    # Seed the machine first so the redirect target exists.
    client.put(
        "/machines/aa:bb:cc:dd:ee:ff",
        json={
            "bty_image_ref": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
            "boot_mode": "ipxe-exit",
        },
        cookies=AUTH,
    )
    r = client.post(
        "/ui/machines/aa:bb:cc:dd:ee:ff",
        data={
            "bty_image_ref": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
            "boot_mode": "bty-flash-always",
            "target_disk_serial": "",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "/ui/machines/aa:bb:cc:dd:ee:ff?error=" in r.headers["location"]
    api = client.get("/machines/aa:bb:cc:dd:ee:ff", cookies=AUTH).json()
    # Safety gate: didn't flip to flash.
    assert api["boot_mode"] == "ipxe-exit"
    assert api["target_disk_serial"] is None


def test_ui_machine_upsert_rejects_unknown_boot_mode(client: TestClient) -> None:
    """Form upsert routes through the same Pydantic ``MachineUpsert``
    as the JSON API; an invalid ``boot_mode`` produces a 303 with
    an error flash (matches the catalog-form pattern) instead of a
    400 page that loses form context."""
    _login(client)
    r = client.post(
        "/ui/machines/aa:bb:cc:dd:ee:ff",
        data={
            "bty_image_ref": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
            "boot_mode": "yolo",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    location = r.headers["location"]
    assert location.startswith("/ui/machines/aa:bb:cc:dd:ee:ff?error="), location
    assert "boot_mode" in location


def test_ui_machine_detail_renders_boot_mode_dropdown(client: TestClient) -> None:
    _login(client)
    client.put(
        "/machines/aa:bb:cc:dd:ee:ff",
        json={
            "bty_image_ref": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
            "boot_mode": "bty-flash-always",
        },
        cookies=AUTH,
    )
    r = client.get("/ui/machines/aa:bb:cc:dd:ee:ff")
    assert r.status_code == 200
    body = r.text
    assert 'name="boot_mode"' in body
    # Both options present, current value selected.
    assert ">ipxe-exit</option>" in body
    assert ">bty-flash-always</option>" in body
    assert 'value="bty-flash-always" selected' in body


_LSHW_TREE = {
    "id": "sys",
    "class": "system",
    "product": "Test Box",
    "children": [
        {
            "id": "cpu",
            "class": "processor",
            "product": "Test CPU @ 3.0GHz",
            "configuration": {"cores": "4", "threads": "8"},
        },
        {
            "id": "memory",
            "class": "memory",
            "size": 17179869184,
            "children": [
                {"id": "bank:0", "class": "memory", "size": 8589934592},
                {"id": "bank:1", "class": "memory", "size": 8589934592},
            ],
        },
        {
            "id": "net",
            "class": "network",
            "logicalname": "eth0",
            "serial": "aa:bb:cc:dd:ee:ff",
            "product": "Test NIC",
        },
    ],
}


def test_lshw_highlights_parses_cpu_ram_nics() -> None:
    """The Machine-view highlight parser pulls CPU / RAM / NIC MACs out
    of an lshw tree and degrades to None on missing / bad input."""
    import json

    from bty.web._ui import lshw_highlights

    hw = lshw_highlights(json.dumps(_LSHW_TREE))
    assert hw is not None
    assert hw["cpu"] == "Test CPU @ 3.0GHz"
    assert hw["cpu_cores"] == 4
    assert hw["memory"] == "16.0 GiB"
    assert hw["mem_modules"] == 2  # two populated banks
    assert hw["nics"][0]["mac"] == "aa:bb:cc:dd:ee:ff"
    assert hw["nics"][0]["name"] == "eth0"
    assert lshw_highlights(None) is None
    assert lshw_highlights("not json{") is None


def test_lshw_highlights_sums_banks_when_container_has_no_size() -> None:
    """When lshw puts the size on the bank:* children but not on the
    'memory' container (a real hardware shape), the total comes from
    summing the banks rather than reading blank."""
    import json

    from bty.web._ui import lshw_highlights

    tree = {
        "id": "sys",
        "class": "system",
        "children": [
            {
                "id": "memory",
                "class": "memory",
                "children": [
                    {"id": "bank:0", "class": "memory", "size": 4294967296},
                    {"id": "bank:1", "class": "memory", "size": 4294967296},
                ],
            }
        ],
    }
    hw = lshw_highlights(json.dumps(tree))
    assert hw is not None
    assert hw["memory"] == "8.0 GiB"
    assert hw["mem_modules"] == 2


def test_ui_machine_detail_renders_inventory_card(client: TestClient) -> None:
    """Once a box posts an inventory, the Machine view shows a "Machine
    Inventory" card with the hardware highlights (CPU + cores, RAM +
    modules, NICs), the drive list, and both raw-download links."""
    _login(client)
    mac = "aa:bb:cc:dd:ee:d0"
    client.get(f"/pxe/{mac}")  # auto-discover so a row exists
    # Inventory POST is the open /pxe endpoint -- no auth.
    r = client.post(
        f"/pxe/{mac}/inventory",
        json={
            "disks": [{"path": "/dev/sda", "size": "238.5G", "model": "SK hynix", "serial": "S1"}],
            "lshw": _LSHW_TREE,
        },
    )
    assert r.status_code == 204, r.text
    body = client.get(f"/ui/machines/{mac}").text
    assert 'id="inventory"' in body
    assert "Machine Inventory" in body
    # Both downloads.
    assert f"/machines/{mac}/lshw.json" in body
    assert f"/machines/{mac}/disks.json" in body
    # Hardware highlights incl. the new core / module counts.
    assert "Test CPU @ 3.0GHz" in body
    assert "4 cores" in body
    assert "2 modules" in body
    assert "aa:bb:cc:dd:ee:ff" in body
    # Drives listed.
    assert "/dev/sda" in body
    assert "SK hynix" in body


def test_ui_boot_page_renders_with_artifact_state(client: TestClient) -> None:
    """The /ui/netboot page must show the configured boot dir and one
    row per expected artifact (vmlinuz/initrd/squashfs/sha256)."""
    _login(client)
    # Default landing (list section): shows the four artifacts +
    # the polling JS for the active-fetches table.
    r = client.get("/ui/netboot")
    assert r.status_code == 200
    body = r.text
    for name in (
        ARTIFACT_NAMES[0],
        ARTIFACT_NAMES[1],
        ARTIFACT_NAMES[2],
        SHA256_NAME,
    ):
        assert name in body, name
    # Empty boot dir => four "missing" badges (warning kind).
    assert body.count("missing</span>") == 4
    assert body.count('class="badge bg-warning text-dark"') >= 4
    # v0.41.2+: the Fetch-artifacts trigger + the active-jobs tbody both
    # live on /ui/netboot directly (the old /ui/downloads page is gone).
    assert 'id="bty-downloads-fetch-artifacts-btn"' in body
    assert "/boot/releases" in body
    assert "bty-workers-downloads-tbody" in body


# ---------- Phase E: settings page ----------------------------------------


def test_ui_settings_renders_when_authed(client: TestClient) -> None:
    """The /ui/settings page (its own top-nav entry) shows the editable
    Upstream sources card (repo + catalog URL + release tag), and the
    read-only config-value groups, including the Identity group with the
    bty version, service user, and project URL as magic values. The
    Authentication card lives on /ui/account, not here.
    """
    import bty

    _login(client)
    r = client.get("/ui/settings")
    assert r.status_code == 200
    body = r.text
    # Editable upstream card: release repo + the two tag fields +
    # the save form. (v0.41.3+: catalog URL became a derived view of
    # repo + catalog_tag; the form-field id is gone.)
    assert "Upstream sources" in body
    assert 'action="/ui/settings/upstream"' in body
    assert 'id="release_repo"' in body
    assert 'id="catalog_tag"' in body
    assert 'id="netboot_tag"' in body
    # Read-only config groups: storage + the Identity magic values.
    assert "Storage paths" in body
    assert "BTY_STATE_DIR" in body
    assert "Service user" in body
    assert "github.com/safl/bty" in body  # project URL listed as a magic value
    assert f"{bty.__version__}" in body
    # Authentication is an operator concern -> not on Settings.
    assert "Authentication" not in body
    assert "passwd" not in body
    # Cross-links to the Account + Netboot pages.
    assert 'href="/ui/account"' in body
    assert 'href="/ui/netboot"' in body


def test_ui_account_holds_authentication(client: TestClient) -> None:
    """The operator Account page (user-bar gear icon) carries the
    Authentication card moved off the bty Settings page."""
    _login(client)
    r = client.get("/ui/account")
    assert r.status_code == 200
    body = r.text
    assert "Authentication" in body
    assert "BTY_ADMIN_PASSWORD" in body
    # bty config is elsewhere.
    assert "Upstream sources" not in body
    assert 'href="/ui/settings"' in body


def test_ui_settings_upstream_override_round_trips(client: TestClient) -> None:
    """Saving an upstream override persists it; the Settings page then
    shows it as the override value, and clearing the field reverts to
    the default."""
    _login(client)
    # Save repo + catalog tag overrides.
    r = client.post(
        "/ui/settings/upstream",
        data={
            "release_repo": "acme/bty-fork",
            "catalog_tag": "v1.2.3",
            "netboot_tag": "v1.2.3",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/ui/settings?saved=upstream"
    body = client.get("/ui/settings").text
    assert "acme/bty-fork" in body
    assert "v1.2.3" in body
    # The derived catalog URL uses the explicit-tag form.
    assert "releases/download/v1.2.3/catalog.toml" in body
    # Clearing every field reverts to defaults.
    client.post(
        "/ui/settings/upstream",
        data={"release_repo": "", "catalog_tag": "", "netboot_tag": ""},
    )
    body2 = client.get("/ui/settings").text
    assert "acme/bty-fork" not in body2
    assert "safl/bty" in body2


def test_ui_settings_upstream_audit_event_captures_old_and_new(
    client: TestClient,
) -> None:
    """v0.33.28+: settings.upstream.updated event details carries
    both ``old`` and ``new`` for each of the three knobs. v0.41.3+
    the third knob is ``netboot_tag`` (the old ``catalog_url``
    standalone override is gone -- it's derived from repo + catalog
    tag now)."""
    _login(client)
    # Initial save: olds are all None (defaults in effect).
    r1 = client.post(
        "/ui/settings/upstream",
        data={
            "release_repo": "acme/bty-fork",
            "catalog_tag": "v1.0.0",
            "netboot_tag": "v1.0.0",
        },
        follow_redirects=False,
    )
    assert r1.status_code == 303
    # Change two of three values + clear one to test all three shapes.
    r2 = client.post(
        "/ui/settings/upstream",
        data={
            "release_repo": "acme/bty-fork",  # unchanged
            "catalog_tag": "v1.1.0",  # changed
            "netboot_tag": "",  # cleared
        },
        follow_redirects=False,
    )
    assert r2.status_code == 303
    events = client.get(
        "/events",
        params={"kind": "settings.upstream.updated"},
    ).json()["events"]
    assert len(events) >= 2
    # The newest event captures the second save -- the BEFORE state
    # of which was the first save's values.
    newest = events[0]
    d = newest["details"]
    assert d["release_repo"] == {"old": "acme/bty-fork", "new": "acme/bty-fork"}
    assert d["catalog_tag"] == {"old": "v1.0.0", "new": "v1.1.0"}
    assert d["netboot_tag"] == {"old": "v1.0.0", "new": None}


def test_ui_settings_renders_backup_schedule_card(client: TestClient) -> None:
    """The Settings page carries a Backup schedule card with the
    three knobs (enabled / cadence / retention) and the read-only
    destination row, anchored at ``#backup-schedule`` and reachable
    from the subnav. The always-relevant block (Retention + Destination
    + Last run) sits above an ``<hr>`` separator, with the optional
    schedule knobs (Enable + Cadence) below it -- so an operator who
    never enables the scheduler still sees what matters."""
    _login(client)
    body = client.get("/ui/settings").text
    assert 'id="backup-schedule"' in body
    assert 'href="#backup-schedule"' in body
    assert 'action="/ui/settings/backup"' in body
    assert 'name="backup_enabled"' in body
    # All three cadence radios.
    assert 'value="daily"' in body
    assert 'value="weekly"' in body
    assert 'value="manual"' in body
    assert 'name="backup_retention_count"' in body
    # Layout: Retention + Destination + Last-run come BEFORE the <hr>
    # which comes BEFORE the Enable/Cadence schedule knobs. Slice the
    # backup-schedule card out and check the order.
    card_start = body.index('id="backup-schedule"')
    card_end = body.index("Save backup schedule", card_start)
    card = body[card_start:card_end]
    retention_at = card.index('name="backup_retention_count"')
    hr_at = card.index("<hr")
    enable_at = card.index('name="backup_enabled"')
    assert retention_at < hr_at < enable_at, (
        f"backup card order wrong: retention={retention_at} hr={hr_at} enable={enable_at}"
    )


def test_ui_settings_backup_round_trip(client: TestClient) -> None:
    """Saving the backup form persists enabled / cadence / retention
    and the Settings page reflects the new state on the next render."""
    _login(client)
    r = client.post(
        "/ui/settings/backup",
        data={
            "backup_enabled": "on",
            "backup_cadence": "weekly",
            "backup_retention_count": "14",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"].startswith("/ui/settings?saved=backup")
    body = client.get("/ui/settings").text
    # Saved state lights up: enabled+weekly+14. The HTML wraps long
    # attribute lists across lines so we test for the values rather
    # than exact substring shapes.
    assert "checked" in body
    assert 'value="weekly"' in body
    assert 'value="14"' in body


def test_ui_settings_backup_invalid_cadence_rejects(client: TestClient) -> None:
    """Hand-crafted form payloads with an unknown cadence return 422
    rather than silently coercing to the default. Pre-1.0 keeps form
    validation strict so a typo is loud rather than rolling the
    operator's input forward as something else."""
    _login(client)
    r = client.post(
        "/ui/settings/backup",
        data={
            "backup_enabled": "on",
            "backup_cadence": "fortnightly",
            "backup_retention_count": "14",
        },
        follow_redirects=False,
    )
    assert r.status_code == 422
    assert "fortnightly" in r.text


def test_ui_settings_backup_invalid_retention_rejects(client: TestClient) -> None:
    """Non-numeric / sub-1 ``backup_retention_count`` returns 422.
    Same strict-form rationale as the cadence test above."""
    _login(client)
    for bad in ("abc", "0", "-3"):
        r = client.post(
            "/ui/settings/backup",
            data={
                "backup_enabled": "on",
                "backup_cadence": "weekly",
                "backup_retention_count": bad,
            },
            follow_redirects=False,
        )
        assert r.status_code == 422, bad


def test_settings_upstream_override_drives_catalog_fetch_url(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The catalog-fetch handler resolves its URL from the override at
    request time. v0.41.3+: a saved repo + catalog tag together build
    the URL (there's no separate URL override any more)."""
    import urllib.error
    import urllib.request

    _login(client)
    client.post(
        "/ui/settings/upstream",
        data={
            "release_repo": "acme/widgets",
            "catalog_tag": "v9.9.9",
            "netboot_tag": "",
        },
    )
    seen: list[str] = []

    def _fake_urlopen(url, *a, **kw):  # type: ignore[no-untyped-def]
        seen.append(url)
        raise urllib.error.URLError("blocked in test")

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)
    client.post("/ui/catalog/fetch-release", follow_redirects=False)
    assert seen == ["https://github.com/acme/widgets/releases/download/v9.9.9/catalog.toml"]


def test_ui_settings_requires_auth(client: TestClient) -> None:
    r = client.get("/ui/settings")
    assert r.status_code == 303
    assert r.headers["location"] == "/ui/login"


def test_ui_boot_requires_auth(client: TestClient) -> None:
    """Without the cookie, /ui/netboot redirects to login like the rest
    of the UI."""
    r = client.get("/ui/netboot")
    assert r.status_code == 303
    assert r.headers["location"] == "/ui/login"


def test_ui_boot_fetch_requires_auth(client: TestClient) -> None:
    r = client.post("/ui/netboot/fetch-release", data={"tag": "latest"})
    assert r.status_code == 303
    assert r.headers["location"] == "/ui/login"


def test_ui_machines_list_shows_boot_mode_badge(client: TestClient) -> None:
    _login(client)
    client.put(
        "/machines/aa:bb:cc:dd:ee:ff",
        json={
            "bty_image_ref": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
            "boot_mode": "bty-flash-always",
        },
        cookies=AUTH,
    )
    client.put(
        "/machines/11:22:33:44:55:66",
        json={
            "bty_image_ref": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
            "boot_mode": "ipxe-exit",
        },
        cookies=AUTH,
    )
    client.put(
        "/machines/22:33:44:55:66:77",
        json={
            "bty_image_ref": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
            "boot_mode": "bty-flash-once",
        },
        cookies=AUTH,
    )
    client.put(
        "/machines/33:44:55:66:77:88",
        json={"boot_mode": "bty-tui"},
        cookies=AUTH,
    )
    # Auto-discovery via /pxe lands a fifth row with boot_mode=bty-inventory
    # so we can exercise all five badge variants in one table.
    client.get("/pxe/aa:bb:cc:dd:ee:01")
    r = client.get("/ui/machines")
    assert r.status_code == 200
    body = r.text
    # All five boot-policy badges should appear in the table (the badge
    # text is the full policy name).
    assert "bg-danger" in body and ">bty-flash-always<" in body
    assert ">bty-flash-once<" in body  # the bg-warning variant
    assert "bg-dark" in body and ">ipxe-exit<" in body
    assert "bg-info text-dark" in body and ">bty-tui<" in body
    assert "bg-primary" in body and ">bty-inventory<" in body
    # Table header has Boot column + Last flashed column.
    assert ">Boot</th>" in body
    assert ">Last flashed</th>" in body


def test_ui_machine_detail_shows_boot_state_tracking_signals(client: TestClient) -> None:
    """The machine view shows the lifecycle State next to the mode,
    derived from boot_mode + saw_flasher_boot + the completion
    signal column (``last_flashed_at`` / ``known_disks_at``).

    Three states for a bty-flash-once machine:
      - pre-PXE: ``pending flash``
      - PXE/iPXE chain ran (saw_flasher_boot=1) but no /done POST yet:
        ``live env running; awaiting flash``
      - /done POST landed (last_flashed_at set): ``flashed; booting disk``

    Pre-v0.33.22 the middle state didn't exist -- ``saw_flasher_boot``
    alone flipped the label to ``flashed; booting disk``, lying until
    the flasher actually completed.
    """
    from bty.web import _db as _bty_db

    _login(client)
    mac = "aa:bb:cc:dd:ee:11"
    client.put(
        f"/machines/{mac}",
        json={"boot_mode": "bty-flash-once"},
        cookies=AUTH,
    )
    body = client.get(f"/ui/machines/{mac}").text
    assert "pending flash" in body

    state_path = client.app.state.state_path  # type: ignore[attr-defined]
    # Arm saw_flasher_boot the way a flasher /boot fetch would.
    # last_flashed_at is still NULL -- live env booted, but the
    # flasher hasn't completed yet (no /pxe/{mac}/done POST).
    with _bty_db.open_db(state_path) as conn:
        conn.execute(
            "UPDATE machines SET saw_flasher_boot = 1 WHERE mac = ?",
            (mac,),
        )
        conn.commit()
    body = client.get(f"/ui/machines/{mac}").text
    assert "live env running; awaiting flash" in body, (
        f"REGRESSION (v0.33.22): saw_flasher_boot alone must NOT imply "
        f"'flashed; booting disk'. body excerpt: "
        f"{[ln for ln in body.splitlines() if 'flash' in ln.lower()]!r}"
    )
    assert "flashed; booting disk" not in body
    assert "pending flash" not in body

    # /pxe/{mac}/done POST landed -> last_flashed_at populated. NOW
    # the box has actually flashed; the label flips to the
    # "booting the just-flashed disk" state.
    with _bty_db.open_db(state_path) as conn:
        conn.execute(
            "UPDATE machines SET last_flashed_at = ? WHERE mac = ?",
            ("2026-05-26T12:00:00+00:00", mac),
        )
        conn.commit()
    body = client.get(f"/ui/machines/{mac}").text
    assert "flashed; booting disk" in body
    assert "live env running; awaiting flash" not in body
    # Mode is unchanged -- still bty-flash-once.
    assert client.get(f"/machines/{mac}", cookies=AUTH).json()["boot_mode"] == "bty-flash-once"


def test_ui_machine_detail_inventory_state_requires_actual_inventory(
    client: TestClient,
) -> None:
    """REGRESSION (v0.33.22 operator report): a bty-inventory machine
    showed 'inventoried; booting disk' the moment ``saw_flasher_boot``
    flipped -- i.e. when the live env's iPXE chain pulled
    ``/boot/kernel?mac=X``, BEFORE the live env had a chance to
    actually run, let alone POST ``/pxe/{mac}/inventory``.

    Post-fix the label only lights up when ``known_disks_at`` is set
    (which only the inventory POST writes). The in-between state --
    iPXE chain ran but no inventory yet -- gets its own honest
    'live env running; awaiting inventory' label.
    """
    from bty.web import _db as _bty_db

    _login(client)
    mac = "aa:bb:cc:dd:ee:12"
    # bty-inventory is the default for auto-discovered machines; an
    # operator can also PUT it explicitly.
    client.put(
        f"/machines/{mac}",
        json={"boot_mode": "bty-inventory"},
        cookies=AUTH,
    )
    body = client.get(f"/ui/machines/{mac}").text
    assert "pending inventory" in body

    state_path = client.app.state.state_path  # type: ignore[attr-defined]
    # The box booted iPXE + fetched /boot/kernel?mac= -> saw_flasher_boot
    # flipped to 1. The live env's bty has NOT yet POSTed the inventory.
    with _bty_db.open_db(state_path) as conn:
        conn.execute(
            "UPDATE machines SET saw_flasher_boot = 1 WHERE mac = ?",
            (mac,),
        )
        conn.commit()
    body = client.get(f"/ui/machines/{mac}").text
    assert "live env running; awaiting inventory" in body, (
        f"REGRESSION: saw_flasher_boot alone must NOT light up "
        f"'inventoried; booting disk'. body excerpt: "
        f"{[ln for ln in body.splitlines() if 'invent' in ln.lower()]!r}"
    )
    assert "inventoried; booting disk" not in body

    # The live env POSTs /pxe/{mac}/inventory -> known_disks_at + the
    # JSON blob land. NOW the box has actually inventoried.
    with _bty_db.open_db(state_path) as conn:
        conn.execute(
            "UPDATE machines SET known_disks_at = ?, known_disks = ? WHERE mac = ?",
            ("2026-05-26T12:00:00+00:00", '[{"path": "/dev/sda", "serial": "S1"}]', mac),
        )
        conn.commit()
    body = client.get(f"/ui/machines/{mac}").text
    assert "inventoried; booting disk" in body
    assert "live env running; awaiting inventory" not in body


def test_ui_events_renders_older_link_when_full_page(
    client: TestClient,
) -> None:
    """The /ui/events page renders an "Older" link with
    ``?before_id=<smallest-id-on-page>`` when a full page of 50
    rows comes back. Without the cursor an operator on a busy
    appliance can't page back beyond the first 50 events."""
    _login(client)
    # 60 PXE check-ins -> 120 events (machine.discovered +
    # pxe.offered per MAC) -- well past page_size=50.
    for i in range(60):
        client.get(f"/pxe/aa:bb:cc:dd:ee:{i:02x}")
    r = client.get("/ui/events", cookies=AUTH)
    assert r.status_code == 200
    body = r.text
    # Cursor link present.
    assert "before_id=" in body


def test_ui_events_pagination_cursor_returns_older_rows(
    client: TestClient,
) -> None:
    """Following the cursor from the first page yields rows whose
    ids are strictly less than the smallest id on page 1."""
    _login(client)
    for i in range(60):
        client.get(f"/pxe/aa:bb:cc:dd:ee:{i:02x}")
    # Use the JSON /events endpoint for precise id comparison
    # (the HTML page doesn't expose ids in machine-readable form).
    first = client.get("/events", params={"limit": 50}, cookies=AUTH).json()["events"]
    assert len(first) == 50
    smallest_on_page1 = first[-1]["id"]
    second = client.get(
        "/events",
        params={"limit": 50, "before_id": smallest_on_page1},
        cookies=AUTH,
    ).json()["events"]
    assert len(second) > 0
    assert all(e["id"] < smallest_on_page1 for e in second)


def test_ui_machine_detail_dropdown_lists_manifest_entry_after_upload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end of the auto-import fix from v0.19.1: upload a
    catalog.toml, then open /ui/machines/{mac} and assert the
    Image <select> contains an <option> for the manifest entry.
    Pre-v0.19.1 the entry was visible on /ui/images (via merge)
    but the machine-binding dropdown stayed empty for it.

    Builds a separate app per-test because the manifest_path is
    resolved at create_app() time from ``BTY_STATE_DIR``; the
    shared client fixture's app was already built before this
    test runs.
    """
    image_root = tmp_path / "images"
    image_root.mkdir()
    bty_state_dir = tmp_path / "bty-state"
    bty_state_dir.mkdir()
    monkeypatch.setenv("BTY_STATE_DIR", str(bty_state_dir))
    monkeypatch.setenv("BTY_ADMIN_PASSWORD", TEST_PASSWORD)
    fresh_app = create_app(
        state_path=tmp_path / "state.db",
        service_user=TEST_SERVICE_USER,
        secret_key=TEST_SECRET_KEY,
    )

    with TestClient(fresh_app, follow_redirects=False) as c:
        login = c.post("/ui/login", data={"password": TEST_PASSWORD})
        assert login.status_code == 303
        body = (
            b"version = 1\n\n"
            b"[[images]]\n"
            b'name = "rolling-from-upload"\n'
            b'src = "oras://ghcr.io/example/foo:latest"\n'
            b'format = "img.gz"\n'
        )
        r = c.post(
            "/ui/catalog/upload",
            files={"file": ("catalog.toml", body, "application/toml")},
        )
        assert r.status_code == 303, r.text
        # Discover a machine via /pxe so the detail page exists.
        c.get("/pxe/aa:bb:cc:dd:ee:21")
        detail = c.get("/ui/machines/aa:bb:cc:dd:ee:21")
        assert detail.status_code == 200
        body_html = detail.text
        # The bty_image_ref <select> contains the manifest entry's
        # name as an option label.
        assert 'name="bty_image_ref"' in body_html
        assert "rolling-from-upload" in body_html


def test_ui_machine_delete_via_form_records_event(client: TestClient) -> None:
    """The form-style /ui/machines/{mac}/delete must record a
    ``machine.deleted`` event so /ui/events shows operator
    actions consistently. Pre-fix the form delete silently
    removed the row, leaving the audit trail with discovery +
    upsert events but no delete event for the same MAC."""
    _login(client)
    client.put(
        "/machines/aa:bb:cc:dd:ee:11",
        json={
            "bty_image_ref": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
        },
        cookies=AUTH,
    )
    r = client.post("/ui/machines/aa:bb:cc:dd:ee:11/delete")
    assert r.status_code == 303
    # Event recorded.
    events = client.get(
        "/events",
        params={
            "subject_kind": "machine",
            "subject_id": "aa:bb:cc:dd:ee:11",
            "kind": "machine.deleted",
        },
        cookies=AUTH,
    ).json()["events"]
    assert len(events) == 1
    assert events[0]["actor"] == "operator"


def test_ui_machine_delete_via_form(client: TestClient) -> None:
    _login(client)
    client.put(
        "/machines/aa:bb:cc:dd:ee:ff",
        json={
            "bty_image_ref": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
        },
        cookies=AUTH,
    )
    r = client.post("/ui/machines/aa:bb:cc:dd:ee:ff/delete", follow_redirects=False)
    assert r.status_code == 303
    # v0.32.4: real delete redirects with ``?deleted=<mac>`` so the
    # /ui/machines render can render a success flash banner. ``:`` in
    # the MAC isn't percent-encoded in the query string (Starlette
    # treats it as reserved-but-allowed); the browser passes it through
    # to the request.query_params reader unchanged.
    assert "deleted=aa:bb:cc:dd:ee:ff" in r.headers["location"]
    api = client.get(
        "/machines/aa:bb:cc:dd:ee:ff",
        cookies=AUTH,
    )
    assert api.status_code == 404


def test_ui_machine_delete_missing_mac_flashes_missing(client: TestClient) -> None:
    """v0.32.4: ``POST /ui/machines/<mac>/delete`` against a MAC that
    doesn't exist (stale tab, hand-typed URL, second click after another
    operator's delete) redirects to ``/ui/machines?missing=<mac>``
    instead of silently 303'ing to ``/ui/machines``. The destination
    page renders a yellow info banner so the operator sees feedback
    either way -- the previous silent no-op left them wondering whether
    the click registered."""
    _login(client)
    r = client.post("/ui/machines/de:ad:be:ef:00:00/delete", follow_redirects=False)
    assert r.status_code == 303
    assert "missing=de:ad:be:ef:00:00" in r.headers["location"]
    # Follow the redirect and assert the missing-banner copy lands.
    body = client.get(r.headers["location"]).text
    assert "was not found" in body
    assert "de:ad:be:ef:00:00" in body


def test_ui_machine_delete_real_deletion_flashes_deleted(client: TestClient) -> None:
    """Symmetric to the missing test: a real delete lands on
    ``?deleted=<mac>`` and the destination page renders the green
    success banner."""
    _login(client)
    mac = "11:22:33:44:55:66"
    client.put(
        f"/machines/{mac}",
        json={
            "bty_image_ref": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
        },
        cookies=AUTH,
    )
    r = client.post(f"/ui/machines/{mac}/delete", follow_redirects=False)
    assert r.status_code == 303
    body = client.get(r.headers["location"]).text
    assert "Deleted machine" in body
    assert mac in body


def test_ui_images_renders(client: TestClient) -> None:
    _login(client)
    r = client.get("/ui/images")
    assert r.status_code == 200
    # v0.40: catalog page renders even with no entries; no fixture-
    # seeded local file gets surfaced (bty-web is out of the bytes
    # path).


# ---------- /ui/netboot/fetch-release ------------------------------------------


def test_ui_boot_fetch_success_renders_green_flash(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Happy path: ``_releases.fetch_release`` returns a
    ``FetchResult`` -> 200 with a green flash listing the
    artifact count + total bytes. Also records the
    ``netboot.artifacts.fetched`` event."""
    from bty.web import _releases

    def _stub(boot_root_arg: Path, *, tag: str) -> _releases.FetchResult:
        return _releases.FetchResult(
            base_url=f"https://example.invalid/releases/{tag}",
            artifacts=("a.efi", "b.vmlinuz", "c.initrd"),
            total_bytes=12345,
        )

    monkeypatch.setattr(_releases, "fetch_release", _stub)
    _login(client)
    r = client.post("/ui/netboot/fetch-release", data={"tag": "v0.1.2"})
    assert r.status_code == 200
    body = r.text
    assert "alert-success" in body
    assert "Fetched 3 artifacts" in body
    assert "12,345 bytes" in body
    events = client.get(
        "/events",
        params={"subject_kind": "netboot", "subject_id": "v0.1.2"},
        cookies=AUTH,
    ).json()["events"]
    assert any(e["kind"] == "netboot.artifacts.fetched" for e in events)


def test_ui_boot_fetch_failure_renders_red_flash_and_logs_event(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``FetchError`` (no network / 404 release tag / sha mismatch)
    surfaces on the page with a red flash + a
    ``netboot.artifacts.fetch.failed`` event."""
    from bty.web import _releases

    def _raise(boot_root_arg: Path, *, tag: str) -> _releases.FetchResult:
        raise _releases.FetchError(f"tag {tag!r} not found")

    monkeypatch.setattr(_releases, "fetch_release", _raise)
    _login(client)
    r = client.post("/ui/netboot/fetch-release", data={"tag": "v0.999.999"})
    assert r.status_code == 200
    assert "alert-danger" in r.text
    assert "Fetch failed" in r.text
    events = client.get(
        "/events",
        params={"subject_kind": "netboot", "subject_id": "v0.999.999"},
        cookies=AUTH,
    ).json()["events"]
    failed = [e for e in events if e["kind"] == "netboot.artifacts.fetch.failed"]
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
    r = client.post("/ui/netboot/fetch-release", data={"tag": ""})
    assert r.status_code == 200
    assert seen == ["latest"]


# ---------- event acknowledgement -------------------------------------------


def _seed_failed_event(client: TestClient, monkeypatch: pytest.MonkeyPatch, tag: str) -> int:
    """Drive the boot-fetch-failure path to record one
    ``netboot.artifacts.fetch.failed`` event; return its id (newest)."""
    from bty.web import _releases

    def _raise(boot_root_arg: Path, *, tag: str) -> _releases.FetchResult:
        raise _releases.FetchError(f"tag {tag!r} not found")

    monkeypatch.setattr(_releases, "fetch_release", _raise)
    client.post("/ui/netboot/fetch-release", data={"tag": tag})
    events = client.get("/events", params={"failed": "1"}, cookies=AUTH).json()["events"]
    return int(events[0]["id"])


def test_event_ack_endpoint_flips_acknowledged_flag(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POST /events/{id}/ack marks the event acknowledged; the JSON
    listing then reports ``acknowledged: true`` for that row."""
    _login(client)
    eid = _seed_failed_event(client, monkeypatch, "v0.0.1")
    before = client.get("/events", params={"failed": "1"}, cookies=AUTH).json()["events"]
    assert any(e["id"] == eid and e["acknowledged"] is False for e in before)
    r = client.post(f"/events/{eid}/ack", cookies=AUTH)
    assert r.status_code == 200
    assert r.json() == {"id": eid, "acknowledged": True}
    after = client.get("/events", params={"failed": "1"}, cookies=AUTH).json()["events"]
    assert any(e["id"] == eid and e["acknowledged"] is True for e in after)


def test_event_ack_unknown_id_404s(client: TestClient) -> None:
    """Acking a non-existent event id is a 404, not a silent no-op."""
    _login(client)
    r = client.post("/events/999999/ack", cookies=AUTH)
    assert r.status_code == 404


def test_unacknowledged_failure_trips_health_then_ack_clears_it(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unacknowledged failure shows on the dashboard Health
    Monitoring as a not-OK 'No unacknowledged errors' row;
    acknowledging it via the bulk endpoint returns the row to green,
    and clearing it (acknowledged=0) trips the row again.
    """
    _login(client)
    eid = _seed_failed_event(client, monkeypatch, "v0.0.2")
    body = client.get("/ui/dashboard").text
    assert "unacknowledged failure" in body  # the not-OK detail text
    r = client.post(
        "/ui/events/acknowledge",
        data={"ids": str(eid), "acknowledged": "1"},
        cookies=AUTH,
    )
    assert r.status_code == 200
    assert r.json() == {"updated": 1, "acknowledged": True}
    body2 = client.get("/ui/dashboard").text
    assert "No unacknowledged failed events." in body2
    # Clearing the acknowledgement puts the tripwire back.
    r2 = client.post(
        "/ui/events/acknowledge",
        data={"ids": str(eid), "acknowledged": "0"},
        cookies=AUTH,
    )
    assert r2.json() == {"updated": 1, "acknowledged": False}
    assert "unacknowledged failure" in client.get("/ui/dashboard").text


def test_ui_events_list_has_checkboxes_and_bulk_actions(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The /ui/events table shows a per-row select checkbox, a
    select-all, bulk Acknowledge / Clear buttons, and a per-row toggle
    that flips Acknowledge -> Clear once acked."""
    _login(client)
    eid = _seed_failed_event(client, monkeypatch, "v0.0.3")
    body = client.get("/ui/events", params={"failed": "1"}, cookies=AUTH).text
    # Selection + bulk controls.
    assert 'id="select-all"' in body
    assert 'class="form-check-input ev-check"' in body
    assert 'id="bulk-ack"' in body
    assert 'id="bulk-clear"' in body
    # Per-row toggle: unacked -> Ack.
    assert f'data-id="{eid}" data-ack="1"' in body
    assert ">Ack" in body
    # Once acked, the per-row toggle becomes Clear.
    client.post(
        "/ui/events/acknowledge",
        data={"ids": str(eid), "acknowledged": "1"},
        cookies=AUTH,
    )
    body2 = client.get("/ui/events", params={"failed": "1"}, cookies=AUTH).text
    assert f'data-id="{eid}" data-ack="0"' in body2
    assert ">Clear" in body2


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
        ("/static/bty-utils.js", b"btyUtils"),
    ]:
        r = client.get(path)
        assert r.status_code == 200, f"{path}: {r.status_code}"
        assert sniff in r.content, f"{path} missing expected marker {sniff!r}"


def test_layout_loads_bty_utils(client: TestClient) -> None:
    """Every authed page bundles the shared JS helpers (``esc`` +
    ``fmtBytes``) from /static/bty-utils.js. Pages alias them locally
    via ``window.btyUtils`` so a future helper addition lands in one
    place instead of three."""
    _login(client)
    body = client.get("/ui/backups").text
    assert "/static/bty-utils.js" in body
    assert "window.btyUtils.esc" in body
    assert "window.btyUtils.fmtBytes" in body


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


def test_events_workers_requires_auth(client: TestClient) -> None:
    """The worker-events SSE stream requires the same session-cookie
    auth as the machines stream. (Same body-streaming caveat: we
    don't read the body sync here; the bus unit tests cover the
    streaming contract.)"""
    r = client.get("/events/workers")
    assert r.status_code == 401


def test_layout_opens_shared_worker_events_source(client: TestClient) -> None:
    """Every authed page bundles the shared EventSource that
    dispatches ``bty-worker-state-changed`` CustomEvents on
    ``document``; the polling pages listen on that document event to
    refresh without each opening their own EventSource."""
    _login(client)
    body = client.get("/ui/backups").text
    # The layout-level subscriber lives in _layout.html so it's on
    # every page; we assert via /ui/backups since the test client
    # already exercises that path.
    assert 'new EventSource("/events/workers")' in body
    assert "bty-worker-state-changed" in body
    assert "bty-worker-events-connected" in body


def test_polling_pages_listen_for_sse_events(client: TestClient) -> None:
    """Each worker page listens for the shared CustomEvent (instead of
    opening its own EventSource) AND filters by kind so it only
    re-fetches when relevant. v0.41.2+: Backups + Netboot are the
    surviving polling pages; /ui/downloads and /ui/hashing are gone."""
    _login(client)

    backups = client.get("/ui/backups").text
    assert 'e.detail.kind === "backup"' in backups

    netboot = client.get("/ui/netboot").text
    assert 'e.detail.kind === "release"' in netboot
