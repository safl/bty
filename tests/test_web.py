"""Tests for ``bty.web``.

Use FastAPI's ``TestClient`` against an app constructed via
:func:`bty.web._app.create_app` with a ``tmp_path``-backed SQLite.
No monkeypatching of module-level globals; each test gets its own
isolated app + db. The ``app_client`` fixture drives ``POST /ui/login``
with ``$BTY_ADMIN_PASSWORD`` set to the admin password, captures the
resulting session cookie, and exposes it via ``AUTH`` for tests that
explicitly attach it.
"""

from __future__ import annotations

import typing
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from bty.web._app import create_app
from bty.web._releases import ARTIFACT_NAMES

TEST_SERVICE_USER = "bty-test"
TEST_SECRET_KEY = "test-secret-not-for-prod-use"
TEST_PASSWORD = "test-admin-pw"

# Mutated by the ``app_client`` fixture: tests authenticate via
# ``cookies=AUTH`` (a dict like ``{"bty-token": "..."}``); requests
# without the cookie hit the real auth dep and 401.
AUTH: dict[str, str] = {}


@pytest.fixture
def app_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """Yield a TestClient against an isolated bty-web app.

    ``$BTY_ADMIN_PASSWORD`` is set so auth is enabled; the fixture POSTs
    ``/ui/login`` once to mint a real session cookie, captures it for
    ``cookies=AUTH``, then clears the client's sticky cookies so each
    test opts in to authentication explicitly via ``cookies=AUTH`` (or
    omits it to test the unauthed path).
    """
    state = tmp_path / "state.db"
    boot_root = tmp_path / "boot"
    boot_root.mkdir()
    bty_state_dir = tmp_path / "bty-state"
    bty_state_dir.mkdir()
    # Seed a fake live-env triplet so /boot/{name} tests can hit real files.
    (boot_root / ARTIFACT_NAMES[0]).write_bytes(b"fake-kernel")
    (boot_root / ARTIFACT_NAMES[1]).write_bytes(b"fake-initrd")
    (boot_root / ARTIFACT_NAMES[2]).write_bytes(b"fake-squashfs")
    # Pin BTY_STATE_DIR so ``catalog.toml`` upload / fetch-release
    # tests can find the on-disk manifest under tmp_path. The default
    # is /var/lib/bty which would be unwritable in CI.
    monkeypatch.setenv("BTY_STATE_DIR", str(bty_state_dir))
    monkeypatch.setenv("BTY_ADMIN_PASSWORD", TEST_PASSWORD)
    app = create_app(
        state_path=state,
        service_user=TEST_SERVICE_USER,
        secret_key=TEST_SECRET_KEY,
        boot_root=boot_root,
    )

    with TestClient(app) as client:
        r = client.post(
            "/ui/login",
            data={"password": TEST_PASSWORD},
            follow_redirects=False,
        )
        assert r.status_code == 303, r.text
        cookie_value = r.cookies.get("bty-token")
        assert cookie_value is not None
        AUTH.clear()
        AUTH["bty-token"] = cookie_value
        # Drop sticky cookies so unauthed-path tests aren't accidentally authed.
        client.cookies.clear()
        # Expose the state.db path for the few tests that poke internal
        # columns directly (mirrors the e2e fixture).
        client.app.state.state_path = state  # type: ignore[attr-defined]
        try:
            yield client
        finally:
            AUTH.clear()


# ---------- open endpoints (no auth) ----------------------------------------


def test_root_redirects_to_login(app_client: TestClient) -> None:
    """``http://server/`` 303s to ``/ui/login`` so an operator typing the
    bare hostname lands at a useful page (rather than a 404). Already-
    authed visitors get bounced from there to ``/ui/dashboard``."""
    r = app_client.get("/", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/ui/login"


def test_login_form_redirects_authed_visitors_to_dashboard(
    app_client: TestClient,
) -> None:
    """``GET /ui/login`` skips the form when the visitor is already
    authenticated; this is what makes ``GET /`` -> ``/ui/login`` smart
    for both authed and unauthed cases."""
    # The fixture's AUTH cookie was minted via /ui/login already.
    r = app_client.get("/ui/login", cookies=AUTH, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/ui/dashboard"


def test_healthz_is_open(app_client: TestClient) -> None:
    r = app_client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_version_is_open(app_client: TestClient) -> None:
    r = app_client.get("/version")
    assert r.status_code == 200
    body = r.json()
    assert "version" in body and isinstance(body["version"], str) and body["version"]


def test_pxe_for_unknown_mac_returns_tui_template(app_client: TestClient) -> None:
    """An unknown MAC auto-discovers with ``boot_mode=bty-tui`` and is
    served the interactive-live-env iPXE chain. This is "bty-on-a-USB
    but over the network": first PXE contact lands the operator at
    ``bty`` without any prior server-side configuration.

    Since v0.22.10 the kernel cmdline only carries ``bty.server`` +
    ``bty.mac``; ``bty`` GETs ``/pxe/<mac>/plan`` to decide what to
    do (auto-flash, interactive, or no-op). The legacy
    ``bty.mode=interactive`` cmdline flag was retired with the
    server-side plan dispatcher.
    """
    r = app_client.get("/pxe/aa:bb:cc:dd:ee:ff")
    assert r.status_code == 200
    body = r.text
    assert "bty.server=" in body
    assert "bty.mac=aa:bb:cc:dd:ee:ff" in body
    assert "kernel" in body  # chains into the live env


def test_pxe_invalid_mac_returns_400(app_client: TestClient) -> None:
    r = app_client.get("/pxe/not-a-mac")
    assert r.status_code == 400


def test_pxe_bootstrap_returns_self_referential_chain(app_client: TestClient) -> None:
    """The static iPXE script that dnsmasq points iPXE clients at on
    second-stage DHCP. Must reference back to whichever Host the
    client used to reach the server, and use iPXE's runtime MAC
    substitution for the *current* (active / DHCP-leasing) NIC, NOT
    net0 specifically -- multi-NIC hosts with net0 link-down would
    otherwise query bty with net0's EEPROM MAC and get no match."""
    r = app_client.get("/pxe-bootstrap.ipxe", headers={"Host": "192.0.2.1:8080"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    body = r.text
    assert body.startswith("#!ipxe"), body
    # Self-referential chain: the URL uses the Host header AND iPXE's
    # active-NIC MAC variable (no ``netN/`` prefix).
    assert "chain http://192.0.2.1:8080/pxe/${mac:hexhyp}" in body
    # Guard against re-introducing the net0-hardcoded form on the
    # actual chain line (the comment block does reference it as
    # the anti-pattern to avoid, which is fine).
    chain_lines = [ln for ln in body.splitlines() if ln.startswith("chain ")]
    assert chain_lines, body
    for ln in chain_lines:
        assert "${net0/" not in ln, ln
    # No auth required (PXE clients have no token).


# ---------- auth ------------------------------------------------------------


def test_machines_without_token_is_401(app_client: TestClient) -> None:
    r = app_client.get("/machines")
    assert r.status_code == 401


def test_machines_with_wrong_token_is_401(app_client: TestClient) -> None:
    r = app_client.get("/machines", cookies={"bty-token": "wrong-not-a-real-token"})
    assert r.status_code == 401


def test_machines_with_right_token_is_200(app_client: TestClient) -> None:
    r = app_client.get("/machines", cookies=AUTH)
    assert r.status_code == 200
    assert r.json() == []


# ---------- machine CRUD ----------------------------------------------------


def test_machine_crud_round_trip(app_client: TestClient) -> None:
    mac = "aa:bb:cc:dd:ee:ff"
    body = {
        "bty_image_ref": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
        "labels": ["rack-3", "bty-test-01"],
    }

    # Create / upsert
    r = app_client.put(f"/machines/{mac}", json=body, cookies=AUTH)
    assert r.status_code == 200
    created = r.json()
    assert created["mac"] == mac
    assert (
        created["bty_image_ref"]
        == "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
    )
    # Side-table read order is alphabetical, regardless of insert order.
    assert created["labels"] == ["bty-test-01", "rack-3"]

    # Read back
    r = app_client.get(f"/machines/{mac}", cookies=AUTH)
    assert r.status_code == 200
    assert r.json()["mac"] == mac

    # List
    r = app_client.get("/machines", cookies=AUTH)
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 1
    assert rows[0]["mac"] == mac

    # Delete
    r = app_client.delete(f"/machines/{mac}", cookies=AUTH)
    assert r.status_code == 204

    # 404 after delete
    r = app_client.get(f"/machines/{mac}", cookies=AUTH)
    assert r.status_code == 404


def test_machine_upsert_normalises_mac(app_client: TestClient) -> None:
    """Upper-case input + dashes get normalised to canonical form."""
    r = app_client.put(
        "/machines/AA-BB-CC-DD-EE-FF",
        json={"labels": ["n"]},
        cookies=AUTH,
    )
    assert r.status_code == 200
    assert r.json()["mac"] == "aa:bb:cc:dd:ee:ff"


def test_pxe_for_known_mac_uses_assignment_template(app_client: TestClient) -> None:
    mac = "aa:bb:cc:dd:ee:ff"
    app_client.put(
        f"/machines/{mac}",
        json={
            "bty_image_ref": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
        },
        cookies=AUTH,
    )
    r = app_client.get(f"/pxe/{mac}")
    assert r.status_code == 200
    # The SHA-keyed machine record renders the short SHA prefix
    # (first 12 hex chars) into the iPXE comment block, not the
    # legacy image filename. The "no bty assignment" check below
    # catches the more interesting regression: that we are not
    # falling through to the unknown-MAC template.
    assert "0123456789ab" in r.text
    assert "no bty assignment" not in r.text  # not the fallback


# ---------- auto-discovery via /pxe ----------------------------------------


def test_pxe_auto_discovers_unknown_mac(app_client: TestClient) -> None:
    """A /pxe contact for an unknown MAC creates a placeholder record so the
    operator sees the machine in /machines and can claim it. The default
    ``boot_mode`` is ``bty-inventory``: the unknown MAC chains into the live
    env to self-report its disks, then sanboots -- so a new box auto-collects
    its inventory and just boots, with no prior server-side configuration."""
    mac = "11:22:33:44:55:66"

    # Pre-condition: not in the DB.
    pre = app_client.get(f"/machines/{mac}", cookies=AUTH)
    assert pre.status_code == 404

    # PXE client (no auth) hits the endpoint.
    r = app_client.get(f"/pxe/{mac}")
    assert r.status_code == 200
    # v0.22.10+: cmdline carries bty.server + bty.mac; dispatch
    # happens at GET /pxe/<mac>/plan, not at iPXE chain time.
    assert "bty.server=" in r.text
    assert f"bty.mac={mac}" in r.text

    # Now visible to the operator.
    found = app_client.get(f"/machines/{mac}", cookies=AUTH)
    assert found.status_code == 200
    body = found.json()
    assert body["mac"] == mac
    assert body["bty_image_ref"] is None  # discovered, not yet assigned
    assert body["boot_mode"] == "bty-inventory"  # auto-discovery default
    assert body["discovered_at"] is not None
    assert body["last_seen_at"] is not None


def test_pxe_updates_last_seen_on_repeat_contact(app_client: TestClient) -> None:
    """Subsequent /pxe contacts update last_seen_at, leave discovered_at fixed."""
    mac = "11:22:33:44:55:66"

    app_client.get(f"/pxe/{mac}")
    first = app_client.get(f"/machines/{mac}", cookies=AUTH).json()
    assert first["discovered_at"] == first["last_seen_at"]

    # Tiny pause to make the timestamp difference visible.
    import time

    time.sleep(0.01)
    app_client.get(f"/pxe/{mac}")
    second = app_client.get(f"/machines/{mac}", cookies=AUTH).json()
    # discovered_at is sticky; last_seen_at moves forward.
    assert second["discovered_at"] == first["discovered_at"]
    assert second["last_seen_at"] >= first["last_seen_at"]


def test_pxe_does_not_overwrite_assignment(app_client: TestClient) -> None:
    """A PUT-claimed machine that later PXE-boots keeps its assignment;
    the /pxe contact only updates last_seen_at."""
    mac = "aa:bb:cc:dd:ee:ff"
    app_client.put(
        f"/machines/{mac}",
        json={
            "bty_image_ref": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
        },
        cookies=AUTH,
    )
    before = app_client.get(f"/machines/{mac}", cookies=AUTH).json()
    expected_ref = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
    assert before["bty_image_ref"] == expected_ref
    assert before["discovered_at"] is None  # PUT-created

    app_client.get(f"/pxe/{mac}")
    after = app_client.get(f"/machines/{mac}", cookies=AUTH).json()
    assert (
        after["bty_image_ref"] == "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
    )  # untouched
    assert after["last_seen_at"] is not None
    # discovered_at is set on first /pxe contact even for PUT-created rows
    assert after["discovered_at"] is not None


# ---------- image / boot upload --------------------------------------------


def test_put_boot_uploads_to_boot_root(app_client: TestClient) -> None:
    """``PUT /boot/{name}`` symmetric to /images/{name} but lands
    under boot_root - this is how the live trio gets onto the
    appliance via the API instead of scp / fetch-from-release."""
    body = b"vmlinuz-bytes-here"
    r = app_client.put(f"/boot/{ARTIFACT_NAMES[0]}", content=body, cookies=AUTH)
    assert r.status_code == 200
    served = app_client.get(f"/boot/{ARTIFACT_NAMES[0]}")
    assert served.status_code == 200
    assert served.content == body


def test_put_boot_requires_auth(app_client: TestClient) -> None:
    r = app_client.put("/boot/anything", content=b"x")
    assert r.status_code == 401


def test_put_boot_rejects_path_traversal(app_client: TestClient) -> None:
    """``..`` mustn't escape the boot_root. Same defence-in-depth
    as PUT /images/{name} -- the URL-decoded ``..%2F`` is the
    classic traversal probe."""
    r = app_client.put("/boot/..%2Fescape.efi", content=b"x", cookies=AUTH)
    # Three valid rejects: 400 (explicit reject), 404 (no such
    # path), 405 (URL-decoded becomes ``..%2F...`` routing onto a
    # non-PUT handler). All deny the upload.
    assert r.status_code in {400, 404, 405}


def test_put_boot_rejects_oversized_upload(
    app_client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_stream_upload`` is shared between /images and /boot so
    the cap behaviour applies to both. Verify directly so a future
    refactor that splits the two paths can't silently drop the
    cap on /boot."""
    from bty.web import _config

    monkeypatch.setenv("BTY_TUNING_MAX_UPLOAD_BYTES", "16")
    # Cfg is loaded once at create_app time; reload now that the test
    # has set the env override the upload-cap reader consults.
    _config.set_active_config(_config.load_config(None))
    payload = b"a" * 64
    r = app_client.put("/boot/oversized.efi", content=payload, cookies=AUTH)
    assert r.status_code == 413
    boot_root = tmp_path / "boot"
    leftovers = sorted(p.name for p in boot_root.iterdir() if p.name.startswith("oversized"))
    assert leftovers == [], f"upload cap left behind: {leftovers}"


# ---------- images ----------------------------------------------------------


def test_list_images_is_open_for_pxe_clients(app_client: TestClient) -> None:
    """``GET /images`` is an open route: the PXE-booted ``bty`` flow needs
    to enumerate the catalog from inside the live env without first
    bootstrapping a session. Same trust model as ``GET /images/{name}``
    (already open) and the other ``/pxe/`` routes."""
    r = app_client.get("/images")  # no Authorization header
    assert r.status_code == 200


# ---------- create_app sanity ----------------------------------------------


# ---------- boot policy + flash chain --------------------------


def test_machine_default_boot_mode_is_sanboot(app_client: TestClient) -> None:
    """A fresh PUT without an explicit boot_mode gets ``sanboot`` -
    boot the local disk; operators opt INTO reflashing explicitly."""
    r = app_client.put(
        "/machines/aa:bb:cc:dd:ee:ff",
        json={
            "bty_image_ref": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
        },
        cookies=AUTH,
    )
    assert r.status_code == 200
    assert r.json()["boot_mode"] == "ipxe-exit"
    assert r.json()["last_flashed_at"] is None


def test_machine_upsert_rejects_malformed_sha256(app_client: TestClient) -> None:
    """``bty_image_ref`` must be 64 lower-case hex chars. A typo
    (uppercase, wrong length, non-hex) used to land in state.db
    verbatim and surface as a silent ``GET /pxe/<mac>`` lookup
    miss later. Validate at PUT time."""
    for bad in (
        "0123",  # too short
        "GHIJ" * 16,  # non-hex
        "0123456789abcdef" * 4 + "extra",  # too long
        "0123456789ABCDEF0123456789ABCDEF0123456789ABCDEF0123456789ABCDEF",  # uppercase
    ):
        r = app_client.put(
            "/machines/aa:bb:cc:dd:ee:ff",
            json={"bty_image_ref": bad, "boot_mode": "bty-flash-always"},
            cookies=AUTH,
        )
        assert r.status_code == 422, f"expected 422 for {bad!r}, got {r.status_code}"


def test_machine_upsert_rejects_empty_label_in_list(app_client: TestClient) -> None:
    """A blank string inside ``labels`` would land as a meaningless
    empty chip on the row. Per-item ``min_length=1`` rejects it at
    PUT time so the side table never holds a phantom row."""
    r = app_client.put(
        "/machines/aa:bb:cc:dd:ee:ff",
        json={
            "bty_image_ref": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
            "labels": [""],
        },
        cookies=AUTH,
    )
    assert r.status_code == 422


def test_machine_upsert_rejects_invalid_label_shapes(app_client: TestClient) -> None:
    """Each label is free-form (replaced the singular ``hostname`` in
    v0.58.0) but still constrained: first char alnum, body alnum +
    ``-``/``_``/``.``/space, max 64 chars. Off-pattern inputs (leading
    punctuation, control chars, > 64 chars) must 422 at PUT."""
    valid_sha = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
    for bad in (
        "-leading-dash",  # first char must be alnum
        ".leading-dot",  # ditto
        "_leading-underscore",
        " leading-space",
        "has\ttab",  # control char outside the class
        "has\nnewline",
        "x" * 65,  # one over max_length
    ):
        r = app_client.put(
            "/machines/aa:bb:cc:dd:ee:ff",
            json={"bty_image_ref": valid_sha, "labels": [bad]},
            cookies=AUTH,
        )
        assert r.status_code == 422, f"expected 422 for {bad!r}, got {r.status_code}"


def test_machine_upsert_accepts_real_label_shapes(app_client: TestClient) -> None:
    """The per-item label pattern accepts free-form operator tags:
    hostnames, snake_case, dotted FQDNs, mixed-case, spaces."""
    valid_sha = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
    for ok in (
        "host",
        "host01",
        "rack-01",
        "node-1.lab.example.org",
        "host_with_underscore",
        "lab box 3",
        "Lab-Box.3",
        "a",  # one-char label
    ):
        r = app_client.put(
            "/machines/aa:bb:cc:dd:ee:ff",
            json={"bty_image_ref": valid_sha, "labels": [ok]},
            cookies=AUTH,
        )
        assert r.status_code == 200, f"expected 200 for {ok!r}, got {r.status_code} {r.text}"


def test_machine_upsert_accepts_multiple_labels(app_client: TestClient) -> None:
    """Multiple tags coexist on one machine ("rack-3" + "noisy" +
    "gmktec-g10"), read back in alphabetical order."""
    valid_sha = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
    r = app_client.put(
        "/machines/aa:bb:cc:dd:ee:ff",
        json={
            "bty_image_ref": valid_sha,
            "labels": ["rack-3", "noisy", "gmktec-g10"],
        },
        cookies=AUTH,
    )
    assert r.status_code == 200
    assert r.json()["labels"] == ["gmktec-g10", "noisy", "rack-3"]


def test_machine_upsert_rejects_too_many_labels(app_client: TestClient) -> None:
    """The ``MAX_LABELS_PER_MACHINE`` cap (16) bounces 17+ at validation
    time. A forgotten comma in a 200-tag paste shouldn't land 200 rows."""
    valid_sha = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
    r = app_client.put(
        "/machines/aa:bb:cc:dd:ee:ff",
        json={"bty_image_ref": valid_sha, "labels": [f"tag{i}" for i in range(17)]},
        cookies=AUTH,
    )
    assert r.status_code == 422


def test_machine_upsert_rejects_unknown_fields(app_client: TestClient) -> None:
    """``MachineUpsert(extra="forbid")`` -- a typo sending an
    unknown field instead of ``bty_image_ref`` must 422 loudly.
    Without ``extra="forbid"``, unknown keys would be silently
    dropped, landing an assignment with ``bty_image_ref=NULL``
    that surfaces as "no bty assignment" at PXE-chain time. This
    pins the strict-extra contract so the failure surfaces at
    PUT time."""
    r = app_client.put(
        "/machines/aa:bb:cc:dd:ee:ff",
        json={
            "image": "stale-filename.qcow2",
            "boot_mode": "bty-flash-always",
        },
        cookies=AUTH,
    )
    assert r.status_code == 422
    body = r.json()
    # Pydantic v2's "extra fields" diagnostic carries the offending
    # key in the loc + an "Extra inputs are not permitted" message.
    assert any(
        "image" in str(err.get("loc", "")) and "extra" in err.get("type", "")
        for err in body.get("detail", [])
    )


def test_machine_upsert_accepts_boot_mode_flash(app_client: TestClient) -> None:
    r = app_client.put(
        "/machines/aa:bb:cc:dd:ee:ff",
        json={
            "bty_image_ref": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
            "boot_mode": "bty-flash-always",
        },
        cookies=AUTH,
    )
    assert r.status_code == 200
    assert r.json()["boot_mode"] == "bty-flash-always"


def test_machine_upsert_rejects_unknown_boot_mode(app_client: TestClient) -> None:
    r = app_client.put(
        "/machines/aa:bb:cc:dd:ee:ff",
        json={
            "bty_image_ref": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
            "boot_mode": "yolo",
        },
        cookies=AUTH,
    )
    assert r.status_code == 422


def test_pxe_default_sanboot_assigned_machine_returns_sanboot_template(
    app_client: TestClient,
) -> None:
    """An image-assigned machine on the default boot_mode (sanboot):
    iPXE boots the local disk, NOT the flash chain. Reflashing is
    opt-in via a bty-flash-* policy, not implicit on assignment."""
    app_client.put(
        "/machines/aa:bb:cc:dd:ee:ff",
        json={"bty_image_ref": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"},
        cookies=AUTH,
    )
    r = app_client.get("/pxe/aa:bb:cc:dd:ee:ff")
    assert r.status_code == 200
    body = r.text
    # ipxe_sanboot.j2 - explicitly NOT the flash chain
    assert "sanboot" in body
    assert "kernel" not in body
    assert "bty.image_url" not in body


def test_pxe_flash_policy_returns_chain_with_args(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """boot_mode=flash + bound image: chain into kernel/initrd
    with the minimal ``bty.server`` + ``bty.mac`` cmdline params.

    Since v0.22.10 the kernel cmdline no longer carries
    ``bty.image_url`` / ``bty.target_disk_serial`` / ``bty.image_format``
    -- those come from ``GET <server>/pxe/<mac>/plan`` once ``bty``
    runs on tty1. The iPXE chain still distinguishes flash vs tui
    so the audit log records the intended outcome; the cmdline
    shape is just the same minimal pair.

    Machines bind by ``bty_image_ref`` (the SHA-256 of the
    canonicalised src URL). The PXE handler resolves the ref through
    ``catalog_entries`` and emits ``/images/<ref>/<name>`` (still
    used in the audit-log details + plan endpoint output); the
    serve_image route handles cache-through.
    """
    flash_sha = "0123456789abcdef" * 4

    def fake_urlopen(*_a, **_kw):  # type: ignore[no-untyped-def]
        return _MockResp(b"", headers={"Content-Length": "0"})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr(
        "bty.catalog.fetch_sha256_for_url",
        lambda *_a, **_kw: flash_sha,
    )
    image_url = "https://example.invalid/demo.img.gz"
    r = app_client.post(
        "/catalog/entries",
        json={
            "image_url": image_url,
            "sha_url": "https://example.invalid/demo.img.gz.sha256",
        },
        cookies=AUTH,
    )
    assert r.status_code == 201, r.text
    ref = r.json()["bty_image_ref"]
    assert r.json()["disk_image_sha"] == flash_sha

    # boot_mode=flash still requires an explicit target_disk_serial
    # to route to the ipxe_flash.j2 template (vs the local-fallback);
    # the serial itself is now delivered via the plan endpoint, not
    # the cmdline.
    app_client.put(
        "/machines/aa:bb:cc:dd:ee:ff",
        json={
            "bty_image_ref": ref,
            "boot_mode": "bty-flash-always",
            "target_disk_serial": "WD-WX12345",
        },
        cookies=AUTH,
    )
    r = app_client.get("/pxe/aa:bb:cc:dd:ee:ff", headers={"Host": "bty.local:8080"})
    assert r.status_code == 200
    body = r.text
    assert body.startswith("#!ipxe"), body
    assert "set bty-base http://bty.local:8080" in body
    assert f"kernel ${{bty-base}}/boot/{ARTIFACT_NAMES[0]}" in body
    assert f"initrd ${{bty-base}}/boot/{ARTIFACT_NAMES[1]}" in body
    assert f"fetch=${{bty-base}}/boot/{ARTIFACT_NAMES[2]}" in body
    assert "console=ttyS0,115200" in body
    assert "bty.server=${bty-base}" in body
    assert "bty.mac=aa:bb:cc:dd:ee:ff" in body
    # Retired in v0.22.10: these come from /pxe/<mac>/plan now, not
    # the cmdline.
    assert "bty.image_url" not in body
    assert "bty.target_disk_serial" not in body
    assert "bty.image_format" not in body
    assert "bty.provisioning" not in body
    # Same plymouth-disable as the tui template; see that test for
    # the MS-01 wedge that drove this.
    assert "plymouth.enable=0" in body


def test_pxe_plan_unknown_mac_auto_discovers_and_returns_inventory(
    app_client: TestClient,
) -> None:
    """``GET /pxe/<mac>/plan`` on an unknown MAC mirrors the iPXE
    auto-discovery path: creates a machine record with
    ``boot_mode=bty-inventory`` and returns ``mode=inventory`` so
    ``bty`` posts its disks and reboots into a sanboot.

    A new box self-collects its inventory with no prior server-side
    configuration; the operator then assigns a flash policy from the
    now-populated disk dropdown.
    """
    mac = "11:22:33:44:55:66"
    pre = app_client.get(f"/machines/{mac}", cookies=AUTH)
    assert pre.status_code == 404

    r = app_client.get(f"/pxe/{mac}/plan", headers={"Host": "bty.local:8080"})
    assert r.status_code == 200
    body = r.json()
    assert body["mode"] == "inventory"

    # Auto-discovered as boot_mode=bty-inventory (matches /pxe/{mac}).
    row = app_client.get(f"/machines/{mac}", cookies=AUTH).json()
    assert row["boot_mode"] == "bty-inventory"


def test_pxe_plan_sanboot_mode_returns_local_mode(app_client: TestClient) -> None:
    """``boot_mode=ipxe-exit`` -> plan ``mode=exit`` so ``bty`` exits
    cleanly (sanboot is handled at the iPXE layer; the box never
    reaches the live env). The plan ``mode`` token is a live-env
    signal distinct from any boot_mode."""
    mac = "aa:bb:cc:dd:ee:ff"
    app_client.put(
        f"/machines/{mac}",
        json={"boot_mode": "ipxe-exit"},
        cookies=AUTH,
    )
    r = app_client.get(f"/pxe/{mac}/plan")
    assert r.status_code == 200
    assert r.json() == {"mode": "exit"}


def test_pxe_plan_flash_policy_with_target_returns_auto(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``boot_mode=flash`` + bindable ref + target_disk_serial ->
    ``mode=flash`` with the image URL and target serial filled in.
    ``bty`` runs the flash without prompts.

    For an https catalog entry without a withcache configured, the
    plan emits the origin URL directly -- bty-web is out of the
    bytes path. (v0.40: dropped the ``/images`` stream-proxy fallback
    for https sources; withcache 404s on miss anyway, and the live env
    fetches origin happily.)"""
    flash_sha = "0123456789abcdef" * 4

    def fake_urlopen(*_a, **_kw):  # type: ignore[no-untyped-def]
        return _MockResp(b"", headers={"Content-Length": "0"})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr(
        "bty.catalog.fetch_sha256_for_url",
        lambda *_a, **_kw: flash_sha,
    )
    r = app_client.post(
        "/catalog/entries",
        json={
            "image_url": "https://example.invalid/demo.img.gz",
            "sha_url": "https://example.invalid/demo.img.gz.sha256",
        },
        cookies=AUTH,
    )
    assert r.status_code == 201, r.text
    ref = r.json()["bty_image_ref"]

    mac = "aa:bb:cc:dd:ee:ff"
    app_client.put(
        f"/machines/{mac}",
        json={
            "bty_image_ref": ref,
            "boot_mode": "bty-flash-always",
            "target_disk_serial": "WD-WX12345",
        },
        cookies=AUTH,
    )
    r = app_client.get(f"/pxe/{mac}/plan", headers={"Host": "bty.local:8080"})
    assert r.status_code == 200
    body = r.json()
    assert body["mode"] == "flash"
    assert body["target_disk_serial"] == "WD-WX12345"
    # No withcache configured -> live env fetches direct from origin.
    assert body["image"] == "https://example.invalid/demo.img.gz"
    # The CONTENT sha rides along so the live env verifies the bytes
    # even though the origin URL doesn't embed the digest in its path.
    # This is disk_image_sha (hash of the bytes), NOT bty_image_ref
    # (ref = sha256 of the canonical URL, an identifier).
    assert body["disk_image_sha"] == flash_sha
    assert body["disk_image_sha"] != ref


def test_pxe_plan_flash_uses_withcache_url_when_blob_is_cached(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When a withcache is configured AND already holds the origin
    blob (is_cached True), the plan returns withcache's ``/b/<token>/``
    URL so the live env streams from the warm cache instead of
    re-fetching from origin every boot. On a cold cache (is_cached
    False) the plan returns the origin URL directly -- see
    ``test_pxe_plan_flash_policy_with_target_returns_auto``."""
    from bty.web import _settings_store, _withcache

    flash_sha = "0123456789abcdef" * 4

    def fake_urlopen(*_a, **_kw):  # type: ignore[no-untyped-def]
        return _MockResp(b"", headers={"Content-Length": "0"})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr("bty.catalog.fetch_sha256_for_url", lambda *_a, **_kw: flash_sha)
    # Pin a withcache URL via the override key.
    monkeypatch.setenv(_settings_store.ENV_WITHCACHE_URL, "http://cache.invalid:3000")
    # Force is_cached -> True without standing up a stub server.
    monkeypatch.setattr(_withcache, "is_cached", lambda *_a, **_kw: True)

    r = app_client.post(
        "/catalog/entries",
        json={
            "image_url": "https://example.invalid/demo.img.gz",
            "sha_url": "https://example.invalid/demo.img.gz.sha256",
        },
        cookies=AUTH,
    )
    assert r.status_code == 201, r.text
    ref = r.json()["bty_image_ref"]

    mac = "aa:bb:cc:dd:ee:c0"
    app_client.put(
        f"/machines/{mac}",
        json={
            "bty_image_ref": ref,
            "boot_mode": "bty-flash-always",
            "target_disk_serial": "WC-CACHED-SERIAL",
        },
        cookies=AUTH,
    )
    plan = app_client.get(f"/pxe/{mac}/plan", headers={"Host": "bty.local:8080"}).json()
    assert plan["mode"] == "flash"
    # Plan rewrites to withcache's ``/b/<urlsafe-b64(origin)>/<basename>``.
    assert plan["image"].startswith("http://cache.invalid:3000/b/")
    assert plan["image"].endswith("/demo.img.gz")
    # Observability: the plan event records the withcache decision so the
    # operator can see in /ui/events that the boot streamed from cache.
    events = app_client.get(
        "/events",
        params={"subject_kind": "machine", "subject_id": mac, "kind": "netboot.pxe.plan"},
        cookies=AUTH,
    ).json()["events"]
    assert events, "expected a netboot.pxe.plan event"
    assert events[0]["details"]["withcache"] == {
        "configured": True,
        "hit": True,
        "served_from": "withcache",
    }


def test_pxe_plan_flash_records_origin_when_withcache_misses(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On a cache miss the plan serves origin AND the plan event records
    served_from=origin / hit=False -- the operator can tell withcache was
    consulted but didn't have it (so it's now warming)."""
    from bty.web import _settings_store, _withcache

    flash_sha = "fedcba9876543210" * 4

    def fake_urlopen(*_a, **_kw):  # type: ignore[no-untyped-def]
        return _MockResp(b"", headers={"Content-Length": "0"})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr("bty.catalog.fetch_sha256_for_url", lambda *_a, **_kw: flash_sha)
    monkeypatch.setenv(_settings_store.ENV_WITHCACHE_URL, "http://cache.invalid:3000")
    monkeypatch.setattr(_withcache, "is_cached", lambda *_a, **_kw: False)  # cold

    r = app_client.post(
        "/catalog/entries",
        json={
            "image_url": "https://example.invalid/cold.img.gz",
            "sha_url": "https://example.invalid/cold.img.gz.sha256",
        },
        cookies=AUTH,
    )
    assert r.status_code == 201, r.text
    ref = r.json()["bty_image_ref"]
    mac = "aa:bb:cc:dd:ee:c1"
    app_client.put(
        f"/machines/{mac}",
        json={
            "bty_image_ref": ref,
            "boot_mode": "bty-flash-always",
            "target_disk_serial": "WC-COLD-SERIAL",
        },
        cookies=AUTH,
    )
    plan = app_client.get(f"/pxe/{mac}/plan", headers={"Host": "bty.local:8080"}).json()
    assert plan["image"] == "https://example.invalid/cold.img.gz"  # origin, not /b/
    events = app_client.get(
        "/events",
        params={"subject_kind": "machine", "subject_id": mac, "kind": "netboot.pxe.plan"},
        cookies=AUTH,
    ).json()["events"]
    assert events[0]["details"]["withcache"] == {
        "configured": True,
        "hit": False,
        "served_from": "origin",
    }


def test_pxe_plan_flash_uses_warm_withcache_even_when_resolved_src_null(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Netboot must consult a warm withcache for an entry whose import left
    ``resolved_src`` NULL. The plan's cache lookup keys on ``src`` only, so
    gating it on resolved_src (which it never uses) wrongly forced such
    entries to origin even when the cache held the bytes."""
    from bty.web import _db as _bty_db
    from bty.web import _settings_store, _withcache

    flash_sha = "0123456789abcdef" * 4

    def fake_urlopen(*_a, **_kw):  # type: ignore[no-untyped-def]
        return _MockResp(b"", headers={"Content-Length": "0"})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr("bty.catalog.fetch_sha256_for_url", lambda *_a, **_kw: flash_sha)
    monkeypatch.setenv(_settings_store.ENV_WITHCACHE_URL, "http://cache.invalid:3000")
    monkeypatch.setattr(_withcache, "is_cached", lambda *_a, **_kw: True)  # warm

    r = app_client.post(
        "/catalog/entries",
        json={
            "image_url": "https://example.invalid/nullres.img.gz",
            "sha_url": "https://example.invalid/nullres.img.gz.sha256",
        },
        cookies=AUTH,
    )
    assert r.status_code == 201, r.text
    ref = r.json()["bty_image_ref"]

    # Simulate an entry whose import never populated resolved_src.
    state_path: Path = app_client.app.state.state_path  # type: ignore[attr-defined]
    with _bty_db.open_db(state_path) as conn:
        conn.execute(
            "UPDATE catalog_entries SET resolved_src = NULL WHERE bty_image_ref = ?",
            (ref,),
        )
        conn.commit()

    mac = "aa:bb:cc:dd:ee:c2"
    app_client.put(
        f"/machines/{mac}",
        json={
            "bty_image_ref": ref,
            "boot_mode": "bty-flash-always",
            "target_disk_serial": "WC-NULLRES-SERIAL",
        },
        cookies=AUTH,
    )
    plan = app_client.get(f"/pxe/{mac}/plan", headers={"Host": "bty.local:8080"}).json()
    # Served from withcache despite resolved_src being NULL (regression guard
    # for the spurious `and resolved_src` gate on the netboot cache lookup).
    assert plan["image"].startswith("http://cache.invalid:3000/b/")
    assert plan["image"].endswith("/nullres.img.gz")
    events = app_client.get(
        "/events",
        params={"subject_kind": "machine", "subject_id": mac, "kind": "netboot.pxe.plan"},
        cookies=AUTH,
    ).json()["events"]
    assert events[0]["details"]["withcache"] == {
        "configured": True,
        "hit": True,
        "served_from": "withcache",
    }


def test_catalog_toml_rewrites_srcs_through_withcache_when_configured(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When a withcache URL is configured, ``GET /catalog.toml``
    rewrites EVERY remote entry's ``src`` to
    ``<withcache>/b/<b64(origin)>/<basename>`` regardless of original
    scheme. Withcache 0.6.0+ handles OCI / oras internally on a cold
    miss, so the catalog the live env consumes is scheme-uniform."""
    import base64

    from bty.web import _settings_store

    monkeypatch.setenv(_settings_store.ENV_WITHCACHE_URL, "http://cache.invalid:3000")

    # Use the JSON catalog-add API to seed two entries: one https,
    # one oras. The endpoint validates the URLs + resolves digests
    # (mocked); we then read /catalog.toml back and assert the
    # rewrite.
    def fake_urlopen(*_a, **_kw):  # type: ignore[no-untyped-def]
        return _MockResp(b"", headers={"Content-Length": "0"})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr("bty.catalog.fetch_sha256_for_url", lambda *_a, **_kw: "f" * 64)

    # Bypass the oras resolve dance with a static stub.
    from withcache import oras as _oras

    monkeypatch.setattr(
        _oras,
        "resolve_ref",
        lambda *_a, **_kw: _oras.ResolvedBlob(
            blob_url="https://ghcr.io/v2/owner/repo/blobs/sha256:dead",
            headers={"Authorization": "Bearer x"},
            digest="sha256:" + "d" * 64,
            size=1024,
            title="demo.img.gz",
        ),
    )

    https_src = "https://example.invalid/demo.img.gz"
    oras_src = "oras://ghcr.io/owner/repo:tag"
    # The /catalog.toml manifest schema requires sha256, so the https
    # entry needs a sha_url to populate it (oras gets a digest from
    # the manifest dance).
    r = app_client.post(
        "/catalog/entries",
        json={"image_url": https_src, "sha_url": https_src + ".sha256"},
        cookies=AUTH,
    )
    assert r.status_code == 201, r.text
    r = app_client.post("/catalog/entries", json={"image_url": oras_src}, cookies=AUTH)
    assert r.status_code == 201, r.text

    # Sanity: both entries should be present before the rewrite check
    # (a silent dedup at the listing layer would mask a real bug).
    entries = app_client.get("/catalog/entries", cookies=AUTH).json()
    seen_src = {e["src"] for e in entries}
    assert {https_src, oras_src}.issubset(seen_src), seen_src

    body = app_client.get("/catalog.toml").content.decode()

    def _b64(origin: str) -> str:
        return base64.urlsafe_b64encode(origin.encode()).decode().rstrip("=")

    # Both originals must be REWRITTEN -- never appear verbatim.
    assert https_src not in body, "https src must be rewritten through withcache"
    assert oras_src not in body, "oras src must be rewritten through withcache"
    # Both must appear under the withcache prefix with the right b64 token.
    assert f"http://cache.invalid:3000/b/{_b64(https_src)}/" in body
    assert f"http://cache.invalid:3000/b/{_b64(oras_src)}/" in body


def test_pxe_plan_flash_policy_without_target_falls_back_to_interactive(
    app_client: TestClient,
) -> None:
    """``boot_mode=flash`` but no target_disk_serial picked yet ->
    falls back to ``mode=interactive``. The auto-flash safety gate
    (mirrored from the iPXE chain) refuses to guess at a disk."""
    mac = "aa:bb:cc:dd:ee:ff"
    app_client.put(
        f"/machines/{mac}",
        json={
            "bty_image_ref": "0123456789abcdef" * 4,
            "boot_mode": "bty-flash-always",
        },
        cookies=AUTH,
    )
    r = app_client.get(f"/pxe/{mac}/plan", headers={"Host": "bty.local:8080"})
    assert r.status_code == 200
    body = r.json()
    assert body["mode"] == "interactive"
    assert body["catalog"] == "http://bty.local:8080/catalog.toml"


def test_pxe_plan_tui_policy_returns_interactive_with_catalog(
    app_client: TestClient,
) -> None:
    """``boot_mode=bty-tui`` -> ``mode=interactive`` with the
    server's catalog. Matches the iPXE ipxe_tui.j2 semantic: the
    operator picks at run time."""
    mac = "aa:bb:cc:dd:ee:ff"
    app_client.put(
        f"/machines/{mac}",
        json={"boot_mode": "bty-tui"},
        cookies=AUTH,
    )
    r = app_client.get(f"/pxe/{mac}/plan", headers={"Host": "bty.local:8080"})
    assert r.status_code == 200
    body = r.json()
    assert body["mode"] == "interactive"
    assert body["catalog"] == "http://bty.local:8080/catalog.toml"


def test_pxe_tui_policy_returns_interactive_chain(app_client: TestClient) -> None:
    """boot_mode=bty-tui: chain into the live env. ``bty-on-tty1.
    service`` launches ``bty``, which GETs ``/pxe/<mac>/plan`` and
    drops the operator into the wizard for boot_mode=bty-tui.

    Since v0.22.10 the cmdline carries only ``bty.server`` +
    ``bty.mac``; ``bty.mode=interactive`` was retired alongside
    the bty-flash-on-boot.service unit (now collapsed into
    ``bty-on-tty1.service`` running unconditionally with plan-endpoint
    dispatch).
    """
    app_client.put(
        "/machines/aa:bb:cc:dd:ee:ff",
        json={"boot_mode": "bty-tui"},
        cookies=AUTH,
    )
    r = app_client.get("/pxe/aa:bb:cc:dd:ee:ff", headers={"Host": "bty.local:8080"})
    assert r.status_code == 200
    body = r.text
    assert body.startswith("#!ipxe"), body
    assert "set bty-base http://bty.local:8080" in body
    assert f"kernel ${{bty-base}}/boot/{ARTIFACT_NAMES[0]}" in body
    assert f"initrd ${{bty-base}}/boot/{ARTIFACT_NAMES[1]}" in body
    assert "bty.server=${bty-base}" in body
    assert "bty.mac=aa:bb:cc:dd:ee:ff" in body
    # Retired in v0.22.10: dispatch happens at /pxe/<mac>/plan, not
    # via cmdline flags. Image + target details come from the plan
    # response, not the kernel cmdline.
    assert "bty.mode=" not in body
    assert "bty.image_url" not in body
    assert "bty.target_disk_serial" not in body
    assert "bty.provisioning" not in body
    # Plymouth is disabled on the kernel cmdline so plymouth-quit-wait
    # cannot hang on hardware whose iGPU framebuffer plymouth refuses
    # to release. Observed on a Minisforum MS-01 PXE-booting bty-
    # netboot v0.19.6: plymouth-quit-wait stayed in "Starting"
    # indefinitely. plymouth.enable=0 tells plymouthd to no-op, so
    # the quit-wait barrier completes immediately even when the
    # framebuffer would have wedged it.
    assert "plymouth.enable=0" in body


def test_machine_upsert_accepts_boot_mode_tui(app_client: TestClient) -> None:
    """``boot_mode='bty-tui'`` is accepted by Pydantic validation alongside
    ``local`` and ``flash``."""
    r = app_client.put(
        "/machines/aa:bb:cc:dd:ee:ff",
        json={"boot_mode": "bty-tui"},
        cookies=AUTH,
    )
    assert r.status_code == 200
    assert r.json()["boot_mode"] == "bty-tui"


def test_pxe_done_updates_last_flashed_at(app_client: TestClient) -> None:
    app_client.put(
        "/machines/aa:bb:cc:dd:ee:ff",
        json={
            "bty_image_ref": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
            "boot_mode": "bty-flash-always",
        },
        cookies=AUTH,
    )
    before = app_client.get("/machines/aa:bb:cc:dd:ee:ff", cookies=AUTH).json()
    assert before["last_flashed_at"] is None

    r = app_client.post("/pxe/aa:bb:cc:dd:ee:ff/done")
    assert r.status_code == 204

    after = app_client.get("/machines/aa:bb:cc:dd:ee:ff", cookies=AUTH).json()
    assert after["last_flashed_at"] is not None
    # Critical: the policy is preserved. Per-job CI cadence stays
    # boot_mode=flash across reflashes.
    assert after["boot_mode"] == "bty-flash-always"


def test_pxe_done_404_for_unknown_mac(app_client: TestClient) -> None:
    r = app_client.post("/pxe/00:11:22:33:44:55/done")
    assert r.status_code == 404


def test_pxe_done_404_logs_orphan_event(app_client: TestClient) -> None:
    """v0.33.28+: when a live env POSTs /done for a MAC bty-web has
    no row for (operator deleted mid-cycle, foreign live env), the
    404 also lands a ``pxe.client.orphan`` event so /ui/events shows
    "a box tried to report flash completion for an unknown MAC".
    Without the event the anomaly was silent."""
    mac = "00:11:22:33:44:65"
    r = app_client.post(f"/pxe/{mac}/done")
    assert r.status_code == 404
    events = app_client.get(
        "/events",
        params={"subject_kind": "machine", "subject_id": mac, "kind": "pxe.client.orphan"},
        cookies=AUTH,
    ).json()["events"]
    assert len(events) == 1
    assert events[0]["details"]["signal"] == "done"


def test_pxe_done_touches_last_seen_at(app_client: TestClient) -> None:
    """v0.33.28+: /done POST refreshes last_seen_at so /ui/machines
    reflects the most recent live-env contact. Pre-fix the last-seen
    timestamp could lag the actual contact by minutes (the live env
    POSTed /done but no /pxe call landed for a while afterward)."""
    mac = "aa:bb:cc:dd:ee:90"
    app_client.put(
        f"/machines/{mac}",
        json={
            "bty_image_ref": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
            "boot_mode": "bty-flash-once",
        },
        cookies=AUTH,
    )
    # First /pxe sets last_seen_at to T0.
    app_client.get(f"/pxe/{mac}")
    before = app_client.get(f"/machines/{mac}", cookies=AUTH).json()
    last_seen_t0 = before["last_seen_at"]
    assert last_seen_t0 is not None
    # /done should refresh last_seen_at to T1 > T0.
    import time

    time.sleep(0.01)
    assert app_client.post(f"/pxe/{mac}/done").status_code == 204
    after = app_client.get(f"/machines/{mac}", cookies=AUTH).json()
    assert after["last_seen_at"] > last_seen_t0


def test_pxe_flash_once_emits_flash_chain_like_flash(
    app_client: TestClient,
) -> None:
    """``boot_mode=bty-flash-once`` returns the same iPXE flash chain
    as ``flash`` on the first PXE boot; it's only the completion
    signal that differs."""
    app_client.put(
        "/machines/11:22:33:44:55:66",
        json={
            "bty_image_ref": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
            "boot_mode": "bty-flash-once",
        },
        cookies=AUTH,
    )
    r = app_client.get("/pxe/11:22:33:44:55:66")
    assert r.status_code == 200
    # The flash chain includes the per-MAC live-env kernel cmdline
    # markers; the sanboot fallback never does.
    assert "bty_image_ref" in r.text or "bty_flash_key" in r.text


def test_pxe_done_records_flash_without_mutating_mode(app_client: TestClient) -> None:
    """``/pxe/{mac}/done`` records last_flashed_at but does NOT mutate
    boot_mode -- the mode is the operator's intent and stays put. The
    post-flash "boot the disk" behaviour comes from the saw_flasher_boot
    bit instead. (Pre-mode/state this flipped flash-once -> sanboot,
    which lied about the configured mode.)"""
    app_client.put(
        "/machines/22:33:44:55:66:77",
        json={
            "bty_image_ref": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
            "boot_mode": "bty-flash-once",
        },
        cookies=AUTH,
    )
    before = app_client.get("/machines/22:33:44:55:66:77", cookies=AUTH).json()
    assert before["boot_mode"] == "bty-flash-once"
    assert before["last_flashed_at"] is None

    r = app_client.post("/pxe/22:33:44:55:66:77/done")
    assert r.status_code == 204

    after = app_client.get("/machines/22:33:44:55:66:77", cookies=AUTH).json()
    assert after["last_flashed_at"] is not None
    # Mode is NOT mutated -- flash-once stays flash-once.
    assert after["boot_mode"] == "bty-flash-once"


def test_pxe_sanboot_mode_returns_sanboot_template(app_client: TestClient) -> None:
    """``boot_mode=ipxe-exit`` emits an iPXE ``sanboot --drive ... ||
    exit`` (bty boots the local disk itself), defaulting to drive
    0x80, NOT the flash chain."""
    app_client.put(
        "/machines/aa:bb:cc:dd:ee:01",
        json={"boot_mode": "ipxe-exit"},
        cookies=AUTH,
    )
    r = app_client.get("/pxe/aa:bb:cc:dd:ee:01")
    assert r.status_code == 200
    body = r.text
    assert "sanboot" in body
    assert "--drive 0x80" in body
    assert "|| exit" in body
    # Not the flash chain.
    assert "kernel" not in body


def test_pxe_sanboot_mode_uses_per_machine_drive_override(app_client: TestClient) -> None:
    """``sanboot_drive`` overrides the default 0x80 so multi-disk
    boxes can point iPXE at the right BIOS drive."""
    app_client.put(
        "/machines/aa:bb:cc:dd:ee:02",
        json={"boot_mode": "ipxe-exit", "sanboot_drive": "0x81"},
        cookies=AUTH,
    )
    r = app_client.get("/pxe/aa:bb:cc:dd:ee:02")
    assert r.status_code == 200
    assert "--drive 0x81" in r.text


def test_machine_upsert_rejects_malformed_sanboot_drive(app_client: TestClient) -> None:
    """``sanboot_drive`` must be an iPXE BIOS drive (``0x`` + 1-2 hex);
    a bad value is a 422 at the API edge."""
    r = app_client.put(
        "/machines/aa:bb:cc:dd:ee:03",
        json={"boot_mode": "ipxe-exit", "sanboot_drive": "sda"},
        cookies=AUTH,
    )
    assert r.status_code == 422


def test_pxe_done_flash_once_second_call_is_idempotent(
    app_client: TestClient,
) -> None:
    """Two /pxe/{mac}/done calls in a row both return 204 cleanly and
    leave boot_mode untouched (bty-flash-once stays flash-once). Guards
    cosmic-ray retries from the live env (a network blip between the
    flash signal and the rebooting kernel)."""
    app_client.put(
        "/machines/33:44:55:66:77:88",
        json={
            "bty_image_ref": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
            "boot_mode": "bty-flash-once",
        },
        cookies=AUTH,
    )
    r1 = app_client.post("/pxe/33:44:55:66:77:88/done")
    assert r1.status_code == 204
    r2 = app_client.post("/pxe/33:44:55:66:77:88/done")
    assert r2.status_code == 204
    after = app_client.get("/machines/33:44:55:66:77:88", cookies=AUTH).json()
    # Mode stays put across both calls -- no mutation to sanboot.
    assert after["boot_mode"] == "bty-flash-once"
    # Two flash events recorded -- one per /done call. The audit
    # trail captures the retry.
    events = app_client.get(
        "/events",
        params={
            "subject_kind": "machine",
            "subject_id": "33:44:55:66:77:88",
            "kind": "machine.flashed",
        },
        cookies=AUTH,
    ).json()["events"]
    assert len(events) == 2


def test_pxe_inventory_persists_disks_and_logs_event(app_client: TestClient) -> None:
    """``POST /pxe/{mac}/inventory`` lands the disk list on the
    machine row (visible via GET /machines/{mac}) and records a
    machine.inventory event. Open endpoint -- no auth needed,
    same trust model as /pxe/{mac}."""
    # Seed the machine via /pxe so a row exists.
    app_client.get("/pxe/aa:bb:cc:dd:ee:aa")
    r = app_client.post(
        "/pxe/aa:bb:cc:dd:ee:aa/inventory",
        json={
            "disks": [
                {
                    "path": "/dev/sda",
                    "size": "500G",
                    "model": "WDC WD5000",
                    "serial": "WD-WX12345",
                    "tran": "sata",
                    "removable": False,
                    "readonly": False,
                },
                {
                    "path": "/dev/nvme0n1",
                    "size": "1T",
                    "model": "Samsung 970",
                    "serial": "S5GXNF0NB12345",
                    "tran": "nvme",
                    "removable": False,
                    "readonly": False,
                },
            ],
        },
    )
    assert r.status_code == 204, r.text
    machine = app_client.get("/machines/aa:bb:cc:dd:ee:aa", cookies=AUTH).json()
    assert machine["known_disks_at"] is not None
    serials = {d["serial"] for d in machine["known_disks"]}
    assert serials == {"WD-WX12345", "S5GXNF0NB12345"}
    events = app_client.get(
        "/events",
        params={"subject_kind": "machine", "subject_id": "aa:bb:cc:dd:ee:aa"},
        cookies=AUTH,
    ).json()["events"]
    inv_events = [e for e in events if e["kind"] == "machine.inventory"]
    assert len(inv_events) == 1
    assert inv_events[0]["details"]["count"] == 2


_LSHW_SAMPLE = {
    "id": "sys",
    "class": "system",
    "product": "Test Box",
    "children": [
        {
            "id": "core",
            "class": "bus",
            "children": [
                {"id": "cpu:0", "class": "processor", "product": "Test CPU @ 3.0GHz"},
                {"id": "memory", "class": "memory", "size": 17179869184},
                {
                    "id": "network",
                    "class": "network",
                    "logicalname": "eth0",
                    "serial": "aa:bb:cc:dd:ee:ff",
                    "product": "Test NIC",
                },
            ],
        },
    ],
}


def test_pxe_inventory_stores_lshw_and_serves_raw_download(app_client: TestClient) -> None:
    """An inventory POST carrying ``lshw`` stores the blob; the raw
    download (auth-gated) serves it back verbatim, and the
    machine.inventory event notes the lshw presence."""
    mac = "aa:bb:cc:dd:ee:c0"
    app_client.get(f"/pxe/{mac}")
    r = app_client.post(
        f"/pxe/{mac}/inventory",
        json={"disks": [{"path": "/dev/sda"}], "lshw": _LSHW_SAMPLE},
    )
    assert r.status_code == 204, r.text

    # Raw download requires a session.
    assert app_client.get(f"/machines/{mac}/lshw.json").status_code == 401
    dl = app_client.get(f"/machines/{mac}/lshw.json", cookies=AUTH)
    assert dl.status_code == 200, dl.text
    assert dl.headers["content-type"].startswith("application/json")
    cd = dl.headers.get("content-disposition", "")
    assert "attachment" in cd
    # The download filename must be Windows-safe (no colons from the MAC).
    assert "filename=" in cd and ":" not in cd.split("filename=", 1)[1]
    assert dl.json()["product"] == "Test Box"

    inv = app_client.get(
        "/events",
        params={"subject_kind": "machine", "subject_id": mac, "kind": "machine.inventory"},
        cookies=AUTH,
    ).json()["events"]
    assert inv[0]["details"]["lshw"] is True


def test_machine_lshw_404_when_absent(app_client: TestClient) -> None:
    """A machine that never posted lshw (e.g. only sanbooted) 404s the
    raw download rather than serving an empty body."""
    mac = "aa:bb:cc:dd:ee:c1"
    app_client.get(f"/pxe/{mac}")  # discovered, no lshw yet
    r = app_client.get(f"/machines/{mac}/lshw.json", cookies=AUTH)
    assert r.status_code == 404


def test_pxe_inventory_lshw_absent_does_not_clobber_prior(app_client: TestClient) -> None:
    """A later inventory POST without ``lshw`` leaves a previously
    stored blob intact (COALESCE), so a boot where lshw hiccuped
    doesn't wipe good hardware data."""
    mac = "aa:bb:cc:dd:ee:c2"
    app_client.get(f"/pxe/{mac}")
    app_client.post(f"/pxe/{mac}/inventory", json={"disks": [], "lshw": _LSHW_SAMPLE})
    # Second post, no lshw.
    app_client.post(f"/pxe/{mac}/inventory", json={"disks": [{"path": "/dev/sda"}]})
    dl = app_client.get(f"/machines/{mac}/lshw.json", cookies=AUTH)
    assert dl.status_code == 200
    assert dl.json()["product"] == "Test Box"


def test_pxe_inventory_oversize_lshw_skipped_keeps_prior(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An lshw blob over LSHW_MAX_BYTES is skipped (not truncated to
    invalid JSON), the prior blob is kept, and the event flags it."""
    from bty.web import _app as _app_mod

    monkeypatch.setattr(_app_mod, "LSHW_MAX_BYTES", 200)
    mac = "aa:bb:cc:dd:ee:c3"
    app_client.get(f"/pxe/{mac}")
    # First: a small blob (well under the 200B test cap) lands.
    small = {"id": "sys", "class": "system", "product": "Test Box"}
    app_client.post(f"/pxe/{mac}/inventory", json={"disks": [], "lshw": small})
    # Second: an oversize blob (well over the cap) is skipped.
    big = {"id": "sys", "class": "system", "junk": "x" * 500}
    app_client.post(f"/pxe/{mac}/inventory", json={"disks": [], "lshw": big})
    dl = app_client.get(f"/machines/{mac}/lshw.json", cookies=AUTH)
    assert dl.status_code == 200
    assert dl.json()["product"] == "Test Box"  # prior blob intact, not the oversize one
    inv = app_client.get(
        "/events",
        params={"subject_kind": "machine", "subject_id": mac, "kind": "machine.inventory"},
        cookies=AUTH,
    ).json()["events"]
    # Most recent event notes the skipped lshw + did not flip the stored flag.
    assert inv[0]["details"]["lshw"] is False


def _saw_flasher_bit_for(app_client: TestClient, mac: str) -> int:
    """Helper: read the saw_flasher_boot column directly from state.db
    for the machine row identified by ``mac``."""
    from bty.web import _db as _bty_db

    state_path: Path = app_client.app.state.state_path  # type: ignore[attr-defined]
    with _bty_db.open_db(state_path) as conn:
        return int(
            conn.execute("SELECT saw_flasher_boot FROM machines WHERE mac = ?", (mac,)).fetchone()[
                "saw_flasher_boot"
            ]
        )


def test_upsert_resets_saw_flasher_boot_on_boot_mode_change(app_client: TestClient) -> None:
    """boot_mode changes invalidate the in-flight cycle: a
    flash-always machine half-way through the flash chain becomes a
    flash-once machine with the bit cleared (so the next /pxe
    serves the flash chain, not a stale sanboot)."""
    mac = "aa:bb:cc:dd:ee:c4"
    app_client.put(f"/machines/{mac}", json={"boot_mode": "bty-flash-always"}, cookies=AUTH)
    app_client.get(f"/boot/{ARTIFACT_NAMES[0]}?mac={mac}", headers={"Host": "bty.local:8080"})
    assert _saw_flasher_bit_for(app_client, mac) == 1, "precondition: /boot?mac= armed"

    app_client.put(f"/machines/{mac}", json={"boot_mode": "bty-flash-once"}, cookies=AUTH)
    assert _saw_flasher_bit_for(app_client, mac) == 0


def test_upsert_resets_saw_flasher_boot_on_image_ref_change(app_client: TestClient) -> None:
    """bty_image_ref changes invalidate the cycle. If armed=1 and
    the operator pivots to a different image, the next /pxe must
    NOT sanboot the disk that holds the OLD image (which the
    operator just decided isn't the right one). Reset to force a
    flash of the new image."""
    mac = "aa:bb:cc:dd:ee:c5"
    ref_a = "a" * 64
    ref_b = "b" * 64
    app_client.put(
        f"/machines/{mac}",
        json={
            "boot_mode": "bty-flash-always",
            "bty_image_ref": ref_a,
            "target_disk_serial": "SN1",
        },
        cookies=AUTH,
    )
    app_client.get(f"/boot/{ARTIFACT_NAMES[0]}?mac={mac}", headers={"Host": "bty.local:8080"})
    assert _saw_flasher_bit_for(app_client, mac) == 1

    # Same mode + target; only image_ref changes.
    app_client.put(
        f"/machines/{mac}",
        json={
            "boot_mode": "bty-flash-always",
            "bty_image_ref": ref_b,
            "target_disk_serial": "SN1",
        },
        cookies=AUTH,
    )
    assert _saw_flasher_bit_for(app_client, mac) == 0


def test_upsert_resets_saw_flasher_boot_on_target_disk_serial_change(
    app_client: TestClient,
) -> None:
    """target_disk_serial changes invalidate the cycle. The box may
    have been flashing the OLD target; sanbooting it now is wrong
    (the new target_disk_serial doesn't match what got written)."""
    mac = "aa:bb:cc:dd:ee:c6"
    ref = "c" * 64
    app_client.put(
        f"/machines/{mac}",
        json={
            "boot_mode": "bty-flash-always",
            "bty_image_ref": ref,
            "target_disk_serial": "SN-OLD",
        },
        cookies=AUTH,
    )
    app_client.get(f"/boot/{ARTIFACT_NAMES[0]}?mac={mac}", headers={"Host": "bty.local:8080"})
    assert _saw_flasher_bit_for(app_client, mac) == 1

    app_client.put(
        f"/machines/{mac}",
        json={
            "boot_mode": "bty-flash-always",
            "bty_image_ref": ref,
            "target_disk_serial": "SN-NEW",
        },
        cookies=AUTH,
    )
    assert _saw_flasher_bit_for(app_client, mac) == 0


def test_upsert_preserves_saw_flasher_boot_on_labels_only_change(
    app_client: TestClient,
) -> None:
    """REGRESSION (v0.33.22): pre-fix, ANY upsert reset
    saw_flasher_boot. An operator renaming a box mid-flash (or
    tweaking sanboot_drive) silently interrupted the in-flight
    cycle. Post-fix, cosmetic-only changes preserve the bit.

    Labels live in a side-table since v0.58.0, so set-labels is
    distinct from the machines-row UPDATE entirely; the CASE-WHEN
    guard never fires for label edits."""
    mac = "aa:bb:cc:dd:ee:c7"
    ref = "d" * 64
    app_client.put(
        f"/machines/{mac}",
        json={
            "boot_mode": "bty-flash-always",
            "bty_image_ref": ref,
            "target_disk_serial": "SN1",
        },
        cookies=AUTH,
    )
    app_client.get(f"/boot/{ARTIFACT_NAMES[0]}?mac={mac}", headers={"Host": "bty.local:8080"})
    assert _saw_flasher_bit_for(app_client, mac) == 1

    # Only the label changes; the cycle-invalidating fields stay.
    app_client.put(
        f"/machines/{mac}",
        json={
            "boot_mode": "bty-flash-always",
            "bty_image_ref": ref,
            "target_disk_serial": "SN1",
            "labels": ["lab-box-1"],
        },
        cookies=AUTH,
    )
    assert _saw_flasher_bit_for(app_client, mac) == 1, (
        "REGRESSION: labels-only upsert must not disrupt the in-flight cycle. "
        "Pre-v0.33.22 the saw_flasher_boot=0 unconditional reset meant the next "
        "/pxe served the flash chain instead of the post-flash sanboot."
    )


def test_boot_fetch_logs_netboot_flasher_armed_on_first_arm(
    app_client: TestClient,
) -> None:
    """v0.33.23+: the 0->1 transition of saw_flasher_boot lands a
    ``netboot.flasher.armed`` event so an operator's /ui/events
    timeline shows the live-env arm without inferring it from a
    chain of /boot fetches + the next /pxe contact's offer_kind.

    Subsequent /boot fetches in the same cycle (kernel + initrd +
    squashfs hit the route separately) are no-ops on the bit and
    MUST NOT spam the audit log."""
    mac = "aa:bb:cc:dd:ee:ca"
    app_client.put(f"/machines/{mac}", json={"boot_mode": "bty-flash-always"}, cookies=AUTH)

    # First /boot artifact fetch (the kernel) -- arms the bit.
    app_client.get(f"/boot/{ARTIFACT_NAMES[0]}?mac={mac}", headers={"Host": "bty.local:8080"})
    # Two more /boot fetches simulating initrd + squashfs in the
    # same live-env boot.
    app_client.get(f"/boot/{ARTIFACT_NAMES[1]}?mac={mac}", headers={"Host": "bty.local:8080"})
    app_client.get(f"/boot/{ARTIFACT_NAMES[2]}?mac={mac}", headers={"Host": "bty.local:8080"})

    r = app_client.get(
        "/events",
        params={
            "subject_kind": "machine",
            "subject_id": mac,
            "kind": "netboot.flasher.armed",
        },
        cookies=AUTH,
    )
    armed_events = r.json()["events"]
    assert len(armed_events) == 1, (
        f"exactly ONE armed event per cycle (the 0->1 transition); "
        f"got {len(armed_events)} -- idempotent re-arms must not spam"
    )
    assert "saw_flasher_boot" in armed_events[0]["summary"]


def test_boot_fetch_touches_last_seen_at_unconditionally(
    app_client: TestClient,
) -> None:
    """v0.33.28+: /boot/{name}?mac= touches last_seen_at + last_seen_ip
    on every fetch (kernel + initrd + squashfs), not just the bit's
    0->1 transition. A /boot fetch is a live-env heartbeat and the
    operator's /ui/machines should reflect each one, even when the
    bit has already armed (no policy match for the bit also means
    no last_seen_at update would happen via the bit-gated UPDATE)."""
    import time

    mac = "aa:bb:cc:dd:ee:cc"
    app_client.put(f"/machines/{mac}", json={"boot_mode": "bty-flash-always"}, cookies=AUTH)
    app_client.get(f"/pxe/{mac}", headers={"Host": "bty.local:8080"})
    before = app_client.get(f"/machines/{mac}", cookies=AUTH).json()
    last_seen_t0 = before["last_seen_at"]
    assert last_seen_t0 is not None

    # First /boot fetch: bit transitions 0->1 AND last_seen_at moves.
    time.sleep(0.01)
    app_client.get(f"/boot/{ARTIFACT_NAMES[0]}?mac={mac}", headers={"Host": "bty.local:8080"})
    mid = app_client.get(f"/machines/{mac}", cookies=AUTH).json()
    last_seen_t1 = mid["last_seen_at"]
    assert last_seen_t1 > last_seen_t0

    # Second /boot fetch (idempotent re-arm): bit stays at 1 but
    # last_seen_at STILL moves -- the unconditional last_seen UPDATE
    # is separate from the bit-gated transition UPDATE.
    time.sleep(0.01)
    app_client.get(f"/boot/{ARTIFACT_NAMES[1]}?mac={mac}", headers={"Host": "bty.local:8080"})
    final = app_client.get(f"/machines/{mac}", cookies=AUTH).json()
    assert final["last_seen_at"] > last_seen_t1


def test_boot_fetch_touches_last_seen_at_even_for_ineligible_mode(
    app_client: TestClient,
) -> None:
    """A /boot fetch with ?mac=X for a machine in ipxe-exit / bty-tui
    mode doesn't arm the bit (the WHERE policy filter rejects the
    bit UPDATE), but last_seen_at MUST still move. The fetch IS a
    contact regardless of policy; the operator's /ui/machines
    should reflect it."""
    import time

    mac = "aa:bb:cc:dd:ee:cd"
    app_client.put(f"/machines/{mac}", json={"boot_mode": "ipxe-exit"}, cookies=AUTH)
    app_client.get(f"/pxe/{mac}", headers={"Host": "bty.local:8080"})
    before = app_client.get(f"/machines/{mac}", cookies=AUTH).json()
    last_seen_t0 = before["last_seen_at"]
    assert last_seen_t0 is not None

    time.sleep(0.01)
    app_client.get(f"/boot/{ARTIFACT_NAMES[0]}?mac={mac}", headers={"Host": "bty.local:8080"})
    after = app_client.get(f"/machines/{mac}", cookies=AUTH).json()
    assert after["last_seen_at"] > last_seen_t0


def test_boot_fetch_skips_arm_event_for_ineligible_boot_mode(
    app_client: TestClient,
) -> None:
    """Boxes in ipxe-exit / bty-tui mode don't consume the bit, so
    the arm WHERE clause skips them. No UPDATE -> no event. Logging
    an arm event for a machine the bit won't be read for would be
    operator-misleading."""
    mac = "aa:bb:cc:dd:ee:cb"
    # ipxe-exit: doesn't consume saw_flasher_boot.
    app_client.put(f"/machines/{mac}", json={"boot_mode": "ipxe-exit"}, cookies=AUTH)
    app_client.get(f"/boot/{ARTIFACT_NAMES[0]}?mac={mac}", headers={"Host": "bty.local:8080"})

    r = app_client.get(
        "/events",
        params={
            "subject_kind": "machine",
            "subject_id": mac,
            "kind": "netboot.flasher.armed",
        },
        cookies=AUTH,
    )
    assert r.json()["events"] == []


def test_upsert_clears_completion_signals_on_boot_mode_change(
    app_client: TestClient,
) -> None:
    """v0.33.28+: when an operator changes boot_mode on PUT
    /machines/{mac}, the in-flight cycle's completion signals
    (last_flashed_at, known_disks_at) get cleared alongside
    saw_flasher_boot. Pre-fix, stale last_flashed_at + a future
    crashed flasher cycle = the /pxe consume served sanboot of a
    half-flashed disk (armed=True + has_flashed=True from the OLD
    cycle satisfied the consume gate even though the box just
    crashed mid-flash).
    """
    mac = "aa:bb:cc:dd:ee:e0"
    # Initial: bty-flash-once + a known flash completion.
    app_client.put(
        f"/machines/{mac}",
        json={
            "bty_image_ref": "1111111111111111111111111111111111111111111111111111111111111111",
            "boot_mode": "bty-flash-once",
            "target_disk_serial": "SN-AAA",
        },
        cookies=AUTH,
    )
    app_client.get(f"/pxe/{mac}")  # discovery
    app_client.get(f"/boot/{ARTIFACT_NAMES[0]}?mac={mac}", headers={"Host": "bty.local:8080"})
    assert app_client.post(f"/pxe/{mac}/done").status_code == 204
    # Confirm completion signal landed.
    before = app_client.get(f"/machines/{mac}", cookies=AUTH).json()
    assert before["last_flashed_at"] is not None
    # Operator rebinds: change boot_mode -> bty-inventory. Old
    # cycle's last_flashed_at must clear.
    app_client.put(
        f"/machines/{mac}",
        json={
            "bty_image_ref": "1111111111111111111111111111111111111111111111111111111111111111",
            "boot_mode": "bty-inventory",
            "target_disk_serial": "SN-AAA",
        },
        cookies=AUTH,
    )
    after = app_client.get(f"/machines/{mac}", cookies=AUTH).json()
    assert after["last_flashed_at"] is None, (
        "boot_mode change must clear last_flashed_at; stale signal would let "
        "a future crashed flasher cycle wrongly satisfy /pxe consume gate"
    )


def test_upsert_clears_completion_signals_on_target_disk_change(
    app_client: TestClient,
) -> None:
    """Target-disk-serial change has the same blast radius as a
    boot_mode change: the completion signals belong to the OLD
    cycle (which targeted a different disk) and must clear."""
    mac = "aa:bb:cc:dd:ee:e1"
    app_client.put(
        f"/machines/{mac}",
        json={
            "bty_image_ref": "2222222222222222222222222222222222222222222222222222222222222222",
            "boot_mode": "bty-flash-always",
            "target_disk_serial": "SN-BBB",
        },
        cookies=AUTH,
    )
    app_client.get(f"/pxe/{mac}")
    app_client.get(f"/boot/{ARTIFACT_NAMES[0]}?mac={mac}", headers={"Host": "bty.local:8080"})
    assert app_client.post(f"/pxe/{mac}/done").status_code == 204
    before = app_client.get(f"/machines/{mac}", cookies=AUTH).json()
    assert before["last_flashed_at"] is not None
    # Same mode, but operator picked a new target disk.
    app_client.put(
        f"/machines/{mac}",
        json={
            "bty_image_ref": "2222222222222222222222222222222222222222222222222222222222222222",
            "boot_mode": "bty-flash-always",
            "target_disk_serial": "SN-CCC",
        },
        cookies=AUTH,
    )
    after = app_client.get(f"/machines/{mac}", cookies=AUTH).json()
    assert after["last_flashed_at"] is None


def test_upsert_preserves_completion_signals_on_cosmetic_change(
    app_client: TestClient,
) -> None:
    """labels / sanboot_drive are display modifiers; they don't
    invalidate the cycle. Pre-fix CASE-WHEN gates this on PUT for
    saw_flasher_boot; v0.33.28 extends the same to completion
    signals -- those must NOT clear on cosmetic edits or operators
    can't relabel a flashed box without losing its flash history."""
    mac = "aa:bb:cc:dd:ee:e2"
    app_client.put(
        f"/machines/{mac}",
        json={
            "bty_image_ref": "3333333333333333333333333333333333333333333333333333333333333333",
            "boot_mode": "bty-flash-once",
            "target_disk_serial": "SN-DDD",
            "labels": ["node-a"],
        },
        cookies=AUTH,
    )
    app_client.get(f"/pxe/{mac}")
    app_client.get(f"/boot/{ARTIFACT_NAMES[0]}?mac={mac}", headers={"Host": "bty.local:8080"})
    assert app_client.post(f"/pxe/{mac}/done").status_code == 204
    before = app_client.get(f"/machines/{mac}", cookies=AUTH).json()
    last_flashed_t0 = before["last_flashed_at"]
    assert last_flashed_t0 is not None
    # Labels-only edit: preserves last_flashed_at.
    app_client.put(
        f"/machines/{mac}",
        json={
            "bty_image_ref": "3333333333333333333333333333333333333333333333333333333333333333",
            "boot_mode": "bty-flash-once",
            "target_disk_serial": "SN-DDD",
            "labels": ["node-a-renamed"],
        },
        cookies=AUTH,
    )
    after = app_client.get(f"/machines/{mac}", cookies=AUTH).json()
    assert after["last_flashed_at"] == last_flashed_t0


def test_upsert_preserves_saw_flasher_boot_on_sanboot_drive_only_change(
    app_client: TestClient,
) -> None:
    """Same as the labels case but for sanboot_drive. The drive
    selector is read at sanboot template render time; changing it
    doesn't invalidate the in-flight flash."""
    mac = "aa:bb:cc:dd:ee:c8"
    ref = "e" * 64
    app_client.put(
        f"/machines/{mac}",
        json={
            "boot_mode": "bty-flash-always",
            "bty_image_ref": ref,
            "target_disk_serial": "SN1",
            "sanboot_drive": "0x80",
        },
        cookies=AUTH,
    )
    app_client.get(f"/boot/{ARTIFACT_NAMES[0]}?mac={mac}", headers={"Host": "bty.local:8080"})
    assert _saw_flasher_bit_for(app_client, mac) == 1

    app_client.put(
        f"/machines/{mac}",
        json={
            "boot_mode": "bty-flash-always",
            "bty_image_ref": ref,
            "target_disk_serial": "SN1",
            "sanboot_drive": "0x81",
        },
        cookies=AUTH,
    )
    assert _saw_flasher_bit_for(app_client, mac) == 1


def test_machine_lshw_404_for_unknown_mac(app_client: TestClient) -> None:
    """The raw lshw download 404s for a MAC with no machine record at
    all (distinct from a known machine that just hasn't posted lshw)."""
    r = app_client.get("/machines/00:11:22:33:44:fe/lshw.json", cookies=AUTH)
    assert r.status_code == 404


def test_machine_disks_raw_download(app_client: TestClient) -> None:
    """GET /machines/{mac}/disks.json serves the lsblk-derived disk
    inventory verbatim (auth-gated, Windows-safe filename), 404s when no
    inventory has been posted."""
    mac = "aa:bb:cc:dd:ee:d7"
    app_client.get(f"/pxe/{mac}")
    # 404 before any inventory.
    assert app_client.get(f"/machines/{mac}/disks.json", cookies=AUTH).status_code == 404
    app_client.post(
        f"/pxe/{mac}/inventory",
        json={"disks": [{"path": "/dev/sda", "serial": "S1", "size": "8G"}]},
    )
    assert app_client.get(f"/machines/{mac}/disks.json").status_code == 401  # needs auth
    dl = app_client.get(f"/machines/{mac}/disks.json", cookies=AUTH)
    assert dl.status_code == 200
    assert dl.headers["content-type"].startswith("application/json")
    cd = dl.headers.get("content-disposition", "")
    assert "filename=" in cd and ":" not in cd.split("filename=", 1)[1]
    body = dl.json()
    assert body[0]["path"] == "/dev/sda"
    assert body[0]["serial"] == "S1"
    # Unknown MAC -> 404.
    assert app_client.get("/machines/00:11:22:33:44:fd/disks.json", cookies=AUTH).status_code == 404


def test_auto_discovery_default_agrees_across_pxe_and_plan(app_client: TestClient) -> None:
    """Both auto-discovery sites (GET /pxe/{mac} and /pxe/{mac}/plan)
    must create the placeholder row with the SAME boot_mode -- a drift
    between the two INSERTs would make a box behave differently
    depending on which endpoint it hit first."""
    mac_a, mac_b = "0a:0a:0a:0a:0a:01", "0b:0b:0b:0b:0b:02"
    app_client.get(f"/pxe/{mac_a}")
    app_client.get(f"/pxe/{mac_b}/plan", headers={"Host": "bty.local:8080"})
    pa = app_client.get(f"/machines/{mac_a}", cookies=AUTH).json()["boot_mode"]
    pb = app_client.get(f"/machines/{mac_b}", cookies=AUTH).json()["boot_mode"]
    assert pa == pb == "bty-inventory"


def test_pxe_inventory_404_for_unknown_mac(app_client: TestClient) -> None:
    """Inventory POST for a MAC that was never discovered returns
    404 -- prevents ``bty`` from silently creating ghost machines
    that the operator never saw on /ui/machines."""
    r = app_client.post(
        "/pxe/00:11:22:33:44:99/inventory",
        json={"disks": []},
    )
    assert r.status_code == 404


def test_pxe_inventory_404_logs_orphan_event(app_client: TestClient) -> None:
    """v0.33.28+: same /ui/events surface as the /done variant.
    A live env that POSTs inventory for a deleted machine should
    show up in the audit log so the operator can correlate."""
    mac = "00:11:22:33:44:99"
    r = app_client.post(
        f"/pxe/{mac}/inventory",
        json={"disks": [{"path": "/dev/sda", "serial": "GHOST"}]},
    )
    assert r.status_code == 404
    events = app_client.get(
        "/events",
        params={"subject_kind": "machine", "subject_id": mac, "kind": "pxe.client.orphan"},
        cookies=AUTH,
    ).json()["events"]
    assert len(events) == 1
    assert events[0]["details"]["signal"] == "inventory"
    assert events[0]["details"]["disk_count"] == 1


def test_pxe_inventory_touches_last_seen_at(app_client: TestClient) -> None:
    """v0.33.28+: inventory POST refreshes last_seen_at so a machine
    in bty-inventory mode that sits at the wizard after posting
    inventory still shows a recent last-seen timestamp."""
    mac = "aa:bb:cc:dd:ee:91"
    app_client.put(
        f"/machines/{mac}",
        json={"boot_mode": "bty-inventory"},
        cookies=AUTH,
    )
    app_client.get(f"/pxe/{mac}")
    before = app_client.get(f"/machines/{mac}", cookies=AUTH).json()
    last_seen_t0 = before["last_seen_at"]
    assert last_seen_t0 is not None
    import time

    time.sleep(0.01)
    r = app_client.post(
        f"/pxe/{mac}/inventory",
        json={"disks": [{"path": "/dev/sda", "serial": "SN-LIVE"}]},
    )
    assert r.status_code == 204
    after = app_client.get(f"/machines/{mac}", cookies=AUTH).json()
    assert after["last_seen_at"] > last_seen_t0


def test_pxe_inventory_rejects_oversize_list(app_client: TestClient) -> None:
    """64-disk cap on the inventory list (matches the InventoryPost
    Pydantic max_length). 65 disks gets a 422."""
    app_client.get("/pxe/aa:bb:cc:dd:ee:bb")
    disks_payload = [
        {
            "path": f"/dev/sd{chr(ord('a') + i % 26)}{i}",
            "size": "1G",
            "serial": f"S{i:04d}",
        }
        for i in range(65)
    ]
    r = app_client.post(
        "/pxe/aa:bb:cc:dd:ee:bb/inventory",
        json={"disks": disks_payload},
    )
    assert r.status_code == 422


def test_pxe_flash_with_orphan_ref_surfaces_reason_on_offered_event(
    app_client: TestClient,
) -> None:
    """Operator-visible failure mode: machine bound to a
    ``bty_image_ref`` whose catalog_entries row has been deleted.
    /pxe returns the local fallback (ipxe.j2) and the always-runs
    ``netboot.pxe.offered`` event carries ``reason: orphan_ref`` +
    the dangling ref in its details payload. v0.33.26+ collapsed
    the standalone ``pxe.flash.orphan_ref`` event into the offered
    event's reason field (one event, not two).
    """
    # Bind to a ref that doesn't exist in catalog_entries.
    orphan_ref = "deadbeef" * 8
    app_client.put(
        "/machines/aa:bb:cc:dd:ee:bd",
        json={
            "bty_image_ref": orphan_ref,
            "boot_mode": "bty-flash-always",
            "target_disk_serial": "SN12345",
        },
        cookies=AUTH,
    )
    r = app_client.get("/pxe/aa:bb:cc:dd:ee:bd")
    assert r.status_code == 200
    # ipxe.j2 (local fallback) -- not the flash chain.
    assert "kernel ${bty-base}/boot/" not in r.text
    events = app_client.get(
        "/events",
        params={"subject_kind": "machine", "subject_id": "aa:bb:cc:dd:ee:bd"},
        cookies=AUTH,
    ).json()["events"]
    offered = next(e for e in events if e["kind"] == "netboot.pxe.offered")
    assert offered["details"]["reason"] == "orphan_ref"
    assert offered["details"]["bty_image_ref"] == orphan_ref
    assert offered["details"]["offer_kind"] == "exit-fallback"


def test_pxe_flash_refuses_chain_surfaces_reason_on_offered_event(
    app_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Safety gate end-to-end: seed a real catalog row so the ref
    resolves, bind the machine to it with boot_mode=flash but
    leave target_disk_serial NULL. The /pxe hit returns ipxe.j2
    (local fallback) and the always-runs ``netboot.pxe.offered``
    event carries ``reason: no_target_disk`` so the operator can
    see why the box isn't reflashing on /ui/events. v0.33.26+
    collapsed the standalone ``pxe.flash.no_target_disk`` event
    into the offered event's reason field."""
    flash_sha = "abcdef0123456789" * 4
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda *_a, **_kw: _MockResp(b"", {"Content-Length": "0"}),
    )
    monkeypatch.setattr("bty.catalog.fetch_sha256_for_url", lambda *_a, **_kw: flash_sha)
    add = app_client.post(
        "/catalog/entries",
        json={
            "image_url": "https://example.invalid/safe.img.gz",
            "sha_url": "https://example.invalid/safe.img.gz.sha256",
        },
        cookies=AUTH,
    )
    assert add.status_code == 201
    ref = add.json()["bty_image_ref"]
    app_client.put(
        "/machines/aa:bb:cc:dd:ee:dd",
        json={"bty_image_ref": ref, "boot_mode": "bty-flash-always"},
        cookies=AUTH,
    )
    r = app_client.get("/pxe/aa:bb:cc:dd:ee:dd")
    assert r.status_code == 200
    # ipxe.j2 (local fallback) -- NOT ipxe_flash.j2.
    assert "kernel ${bty-base}/boot/" not in r.text
    events = app_client.get(
        "/events",
        params={"subject_kind": "machine", "subject_id": "aa:bb:cc:dd:ee:dd"},
        cookies=AUTH,
    ).json()["events"]
    offered = next(e for e in events if e["kind"] == "netboot.pxe.offered")
    assert offered["details"]["reason"] == "no_target_disk"
    assert offered["details"]["boot_mode"] == "bty-flash-always"
    assert offered["details"]["bty_image_ref"] == ref


def test_machines_upsert_accepts_target_disk_serial(app_client: TestClient) -> None:
    """The JSON API takes target_disk_serial and persists it."""
    r = app_client.put(
        "/machines/aa:bb:cc:dd:ee:ee",
        json={
            "boot_mode": "ipxe-exit",
            "target_disk_serial": "Z9YHHRWZ",
        },
        cookies=AUTH,
    )
    assert r.status_code == 200, r.text
    assert r.json()["target_disk_serial"] == "Z9YHHRWZ"


def test_machines_upsert_rejects_oversize_target_disk_serial(
    app_client: TestClient,
) -> None:
    """MachineUpsert.target_disk_serial has max_length=128 to keep
    the column small + bound the kernel cmdline length. >128 chars
    -> 422 (Pydantic rejection)."""
    r = app_client.put(
        "/machines/aa:bb:cc:dd:ee:ef",
        json={"target_disk_serial": "X" * 200},
        cookies=AUTH,
    )
    assert r.status_code == 422


def test_pxe_inventory_ignores_unknown_per_disk_fields(app_client: TestClient) -> None:
    """``InventoryDisk`` is ``extra="ignore"`` so a future ``bty.disks``
    release adding a new field (NVMe namespace, partition table type,
    etc.) doesn't break older bty-web instances. The known fields
    survive; the unknown ones drop on the floor."""
    app_client.get("/pxe/aa:bb:cc:dd:ee:f4")
    r = app_client.post(
        "/pxe/aa:bb:cc:dd:ee:f4/inventory",
        json={
            "disks": [
                {
                    "path": "/dev/sda",
                    "serial": "ABCD1234",
                    "size": "500G",
                    "future_field": "ignored",
                    "another_new_thing": 42,
                },
            ],
        },
    )
    assert r.status_code == 204, r.text
    machine = app_client.get("/machines/aa:bb:cc:dd:ee:f4", cookies=AUTH).json()
    disk = machine["known_disks"][0]
    assert disk["serial"] == "ABCD1234"
    assert disk["size"] == "500G"
    assert "future_field" not in disk
    assert "another_new_thing" not in disk


def test_pxe_inventory_rejects_non_json_body(app_client: TestClient) -> None:
    """A ``bty`` posting garbage (e.g. binary noise from a corrupted
    payload buffer) must produce a clean 4xx, not a 500. FastAPI's
    Pydantic dispatch rejects with 422 when the body is not valid
    JSON or doesn't fit the schema."""
    app_client.get("/pxe/aa:bb:cc:dd:ee:f7")
    r = app_client.post(
        "/pxe/aa:bb:cc:dd:ee:f7/inventory",
        content=b"\x00\x01\x02 not json at all",
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code in (400, 422)
    assert r.status_code != 500


def test_pxe_inventory_rejects_disks_wrong_shape(app_client: TestClient) -> None:
    """``disks`` must be an array of objects; a string in the array
    or a top-level scalar gets 422 (not 500)."""
    app_client.get("/pxe/aa:bb:cc:dd:ee:f8")
    for body in [
        {"disks": "not an array"},
        {"disks": [123]},
        {"disks": [{"path": 42}]},  # path must be str
    ]:
        r = app_client.post(
            "/pxe/aa:bb:cc:dd:ee:f8/inventory",
            json=body,
        )
        assert r.status_code == 422, body


def test_pxe_inventory_accepts_empty_disks_list(app_client: TestClient) -> None:
    """A target with zero disks (a fresh chassis, NVMe-only in a
    USB-boot test rig, etc.) reports ``disks: []``. That's valid
    -- the operator just can't pick a target until the disks are
    physically installed."""
    app_client.get("/pxe/aa:bb:cc:dd:ee:f9")
    r = app_client.post(
        "/pxe/aa:bb:cc:dd:ee:f9/inventory",
        json={"disks": []},
    )
    assert r.status_code == 204
    machine = app_client.get("/machines/aa:bb:cc:dd:ee:f9", cookies=AUTH).json()
    assert machine["known_disks"] == []
    assert machine["known_disks_at"] is not None


def test_pxe_inventory_rejects_unknown_top_level_fields(app_client: TestClient) -> None:
    """``InventoryPost`` is ``extra="forbid"`` at the top level so a
    typo in the ``bty`` payload (``disk`` instead of ``disks``)
    fails 422 loudly rather than silently persisting an empty
    inventory."""
    app_client.get("/pxe/aa:bb:cc:dd:ee:f5")
    r = app_client.post(
        "/pxe/aa:bb:cc:dd:ee:f5/inventory",
        json={"disk": [{"path": "/dev/sda", "serial": "X"}]},  # typo
    )
    assert r.status_code == 422


def test_pxe_plan_flash_chain_carries_target_disk_serial(
    app_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The target disk serial moved from the iPXE kernel cmdline to
    the plan-endpoint JSON in v0.22.10. The iPXE template still
    renders the serial in its header comment block (for operator
    curl-inspection / audit) but it is no longer a kernel param.
    The plan endpoint is the contract ``bty`` consumes.
    """
    flash_sha = "deadbeef" * 8
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda *_a, **_kw: _MockResp(b"", {"Content-Length": "0"}),
    )
    monkeypatch.setattr("bty.catalog.fetch_sha256_for_url", lambda *_a, **_kw: flash_sha)
    add = app_client.post(
        "/catalog/entries",
        json={
            "image_url": "https://example.invalid/img.img.gz",
            "sha_url": "https://example.invalid/img.img.gz.sha256",
        },
        cookies=AUTH,
    )
    assert add.status_code == 201
    ref = add.json()["bty_image_ref"]
    app_client.put(
        "/machines/aa:bb:cc:dd:ee:f6",
        json={
            "bty_image_ref": ref,
            "boot_mode": "bty-flash-always",
            "target_disk_serial": "WD-SERIAL-XYZ",
        },
        cookies=AUTH,
    )
    # Plan endpoint carries the serial.
    plan = app_client.get("/pxe/aa:bb:cc:dd:ee:f6/plan", headers={"Host": "bty.local:8080"}).json()
    assert plan["mode"] == "flash"
    assert plan["target_disk_serial"] == "WD-SERIAL-XYZ"
    # iPXE chain advertises the pin in the header comment so an
    # operator inspecting curl output can see it; the kernel cmdline
    # no longer carries it.
    r = app_client.get("/pxe/aa:bb:cc:dd:ee:f6")
    assert r.status_code == 200
    body = r.text
    assert "target_disk_serial: WD-SERIAL-XYZ" in body
    assert "bty.target_disk_serial" not in body


def test_pxe_hit_records_pxe_offered_event(app_client: TestClient) -> None:
    """Every /pxe/{mac} hit emits a ``pxe.offered`` event recording
    which template was returned. Gives the operator a full timeline
    of "client X showed up, server handed back Y" without enabling
    debug logging."""
    # Bind a machine on sanboot so we get a non-tui offer.
    ref = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
    app_client.put(
        "/machines/aa:bb:cc:dd:ee:f0",
        json={"bty_image_ref": ref, "boot_mode": "ipxe-exit"},
        cookies=AUTH,
    )
    app_client.get("/pxe/aa:bb:cc:dd:ee:f0")
    r = app_client.get(
        "/events",
        params={
            "subject_kind": "machine",
            "subject_id": "aa:bb:cc:dd:ee:f0",
            "kind": "netboot.pxe.offered",
        },
        cookies=AUTH,
    )
    assert r.status_code == 200
    events = r.json()["events"]
    assert len(events) == 1
    ev = events[0]
    assert ev["kind"] == "netboot.pxe.offered"
    assert ev["subject_id"] == "aa:bb:cc:dd:ee:f0"
    assert ev["actor"] == "pxe-client"
    assert ev["details"]["offer"] == "sanboot"
    assert ev["details"]["boot_mode"] == "ipxe-exit"


def test_pxe_hit_records_inventory_offer_for_unknown_mac(app_client: TestClient) -> None:
    """Auto-discovery (unknown MAC) records both ``machine.discovered``
    AND a ``pxe.offered`` event with offer=bty-inventory."""
    app_client.get("/pxe/aa:bb:cc:dd:ee:f1")
    r = app_client.get(
        "/events",
        params={"subject_kind": "machine", "subject_id": "aa:bb:cc:dd:ee:f1"},
        cookies=AUTH,
    )
    events = {e["kind"]: e for e in r.json()["events"]}
    assert set(events) == {"machine.discovered", "netboot.pxe.offered"}
    assert events["netboot.pxe.offered"]["details"]["offer"] == "bty-inventory"


def test_machine_discovered_details_mirror_upserted_shape(app_client: TestClient) -> None:
    """v0.33.27+: the ``machine.discovered`` audit event carries the
    same 5-key details payload shape as ``machine.created`` /
    ``machine.upserted`` (bty_image_ref, boot_mode, sanboot_drive,
    labels, target_disk_serial). At discovery time only boot_mode
    has a value (the auto-default ``bty-inventory``); the rest are
    explicitly NULL so an operator pivoting on a MAC across the
    audit log sees a consistent payload shape, not a missing-keys
    surprise on the discovery row."""
    app_client.get("/pxe/aa:bb:cc:dd:ee:f7")
    r = app_client.get(
        "/events",
        params={
            "subject_kind": "machine",
            "subject_id": "aa:bb:cc:dd:ee:f7",
            "kind": "machine.discovered",
        },
        cookies=AUTH,
    )
    evts = r.json()["events"]
    assert len(evts) == 1
    details = evts[0]["details"]
    assert details == {
        "bty_image_ref": None,
        "boot_mode": "bty-inventory",
        "sanboot_drive": None,
        "labels": [],
        "target_disk_serial": None,
    }


def test_machine_discovered_via_plan_endpoint_carries_same_details(
    app_client: TestClient,
) -> None:
    """The /pxe/{mac}/plan discovery path emits the same details
    payload as /pxe/{mac} so the two discovery routes don't drift
    apart."""
    app_client.get("/pxe/aa:bb:cc:dd:ee:f8/plan")
    r = app_client.get(
        "/events",
        params={
            "subject_kind": "machine",
            "subject_id": "aa:bb:cc:dd:ee:f8",
            "kind": "machine.discovered",
        },
        cookies=AUTH,
    )
    evts = r.json()["events"]
    assert len(evts) == 1
    assert evts[0]["details"] == {
        "bty_image_ref": None,
        "boot_mode": "bty-inventory",
        "sanboot_drive": None,
        "labels": [],
        "target_disk_serial": None,
    }


def test_pxe_concurrent_discovery_no_race(app_client: TestClient) -> None:
    """REGRESSION (v0.33.6): two concurrent ``/pxe/{mac}`` requests
    for the same fresh MAC must not return 500. Pre-fix did
    SELECT-then-plain-INSERT inside a thread-pool handler, so two
    iPXE-retry-induced parallel requests could both see ``row=None``
    and both fire ``INSERT``, with the second hitting
    ``UNIQUE(mac)`` -> ``sqlite3.IntegrityError`` -> 500 in the journal.

    The TestClient + a thread pool doesn't reliably reproduce the
    race (Starlette serialises sync handlers through one event loop
    thread), but the post-fix path is idempotent under any
    interleaving, so we just hammer the route with N parallel
    requests for the same MAC and assert every response is 2xx and
    only ONE ``machine.discovered`` event lands.
    """
    from concurrent.futures import ThreadPoolExecutor

    mac = "aa:bb:cc:dd:ee:f2"
    n = 8

    def hit() -> int:
        return app_client.get(f"/pxe/{mac}").status_code

    with ThreadPoolExecutor(max_workers=n) as pool:
        statuses = list(pool.map(lambda _: hit(), range(n)))

    assert all(s == 200 for s in statuses), f"expected all 200, got {statuses!r}"

    r = app_client.get(
        "/events",
        params={
            "subject_kind": "machine",
            "subject_id": mac,
            "kind": "machine.discovered",
        },
        cookies=AUTH,
    )
    discovery_events = r.json()["events"]
    assert len(discovery_events) == 1, (
        f"machine.discovered must fire exactly once across {n} concurrent /pxe hits "
        f"(the upsert RETURNING clause is the gate); got {len(discovery_events)}"
    )


def test_pxe_plan_concurrent_discovery_no_race(app_client: TestClient) -> None:
    """Mirror of the /pxe/{mac} race test for /pxe/{mac}/plan, which
    used the same SELECT-then-INSERT shape before v0.33.6."""
    from concurrent.futures import ThreadPoolExecutor

    mac = "aa:bb:cc:dd:ee:f3"
    n = 8

    def hit() -> int:
        return app_client.get(f"/pxe/{mac}/plan").status_code

    with ThreadPoolExecutor(max_workers=n) as pool:
        statuses = list(pool.map(lambda _: hit(), range(n)))

    assert all(s == 200 for s in statuses), f"expected all 200, got {statuses!r}"

    r = app_client.get(
        "/events",
        params={
            "subject_kind": "machine",
            "subject_id": mac,
            "kind": "machine.discovered",
        },
        cookies=AUTH,
    )
    assert len(r.json()["events"]) == 1


def test_pxe_discovery_is_race_safe_under_direct_sqlite_repro(
    tmp_path: Path,
) -> None:
    """REGRESSION (v0.33.6 + v0.33.25): pin the discovery race shape
    against a real sqlite DB so a future refactor can't silently
    regress.

    Two layers being verified together:

    1. **Row race** (v0.33.6): two threads inserting the same fresh
       MAC must not raise ``UNIQUE constraint failed`` -- the second
       upsert is a quiet no-op. The pre-v0.33.6 ``SELECT then plain
       INSERT`` would have raised; ``INSERT ... ON CONFLICT DO
       NOTHING`` keeps it quiet.

    2. **is_new discriminator race** (v0.33.25): the pre-v0.33.25
       discriminator was ``(created_at = ?) AS is_new`` -- a timestamp
       compare. If two requests' ``_now_iso()`` happened to TIE (low-
       resolution clock, virtualised host), BOTH saw is_new=1 and
       BOTH logged the discovery event. The post-v0.33.25 pattern
       uses ``INSERT ... ON CONFLICT DO NOTHING RETURNING 1`` -- the
       RETURNING row materialises iff the insert fired -- which is
       timestamp-independent. Hit it with two same-timestamp writes
       and assert only the first gets RETURNING populated.
    """
    import sqlite3 as _sqlite

    from bty.web import _db

    state = tmp_path / "state.db"
    _db.init_db(state)
    mac = "aa:bb:cc:dd:ee:99"
    # Force a timestamp TIE between the two writers. Under the old
    # ``(created_at = ?)`` discriminator this was the bug case; the
    # new pattern is timestamp-independent and must still get it right.
    now = "2026-05-25T12:00:00.000000+00:00"
    later = "2026-05-25T12:00:01.000000+00:00"

    discover_sql = """
        INSERT INTO machines
            (mac, boot_mode,
             discovered_at, last_seen_at, last_seen_ip,
             created_at, updated_at)
        VALUES (?, 'bty-inventory', ?, ?, ?, ?, ?)
        ON CONFLICT(mac) DO NOTHING
        RETURNING 1
    """

    # Two separate connections, simulating two thread-pool workers.
    conn_a = _sqlite.connect(state, timeout=5.0)
    conn_a.row_factory = _sqlite.Row
    conn_b = _sqlite.connect(state, timeout=5.0)
    conn_b.row_factory = _sqlite.Row
    try:
        # Thread A wins discovery.
        args_a = (mac, now, now, "10.0.0.1", now, now)
        inserted_a = conn_a.execute(discover_sql, args_a).fetchone()
        conn_a.commit()
        assert inserted_a is not None, "first INSERT must populate RETURNING (is_new)"

        # Thread B arrives with the SAME timestamp as A. Pre-v0.33.25
        # this is the timestamp-tie bug; post-fix the second writer's
        # RETURNING is empty because DO NOTHING suppresses it.
        args_b = (mac, now, now, "10.0.0.2", now, now)
        inserted_b = conn_b.execute(discover_sql, args_b).fetchone()
        conn_b.commit()
        assert inserted_b is None, "second INSERT must NOT populate RETURNING (is_new=False)"

        # The follow-up UPDATE (the second statement in the handler)
        # still touches last_seen_*; verify the contract.
        row = conn_b.execute(
            """
            UPDATE machines
               SET last_seen_at  = ?,
                   last_seen_ip  = ?,
                   updated_at    = ?,
                   discovered_at = COALESCE(discovered_at, ?)
             WHERE mac = ?
            RETURNING *
            """,
            (later, "10.0.0.2", later, later, mac),
        ).fetchone()
        assert row is not None
        assert row["created_at"] == now, "created_at must stay at first insert's value"
        assert row["updated_at"] == later
        assert row["last_seen_ip"] == "10.0.0.2"
    finally:
        conn_a.close()
        conn_b.close()


def test_machines_upsert_accepts_flash_once(app_client: TestClient) -> None:
    """bty-flash-once is in BOOT_MODES so Pydantic accepts it."""
    r = app_client.put(
        "/machines/33:44:55:66:77:88",
        json={"boot_mode": "bty-flash-once"},
        cookies=AUTH,
    )
    assert r.status_code == 200, r.text
    assert r.json()["boot_mode"] == "bty-flash-once"


# ---------- /events API (audit log) -------------------------------------


def test_events_list_requires_auth(app_client: TestClient) -> None:
    r = app_client.get("/events")
    assert r.status_code == 401


def test_events_list_no_operator_or_pxe_activity_initially(app_client: TestClient) -> None:
    """Before any operator / PXE activity, the only rows the audit
    log has are auto-import side-effects (the lifespan hashes
    seeded images and emits ``image.hashed``) plus the fixture's
    bootstrap ``auth.login.succeeded`` row. The test fixture seeds
    ``demo.qcow2`` so that one is expected; everything else should
    be absent."""
    r = app_client.get("/events", cookies=AUTH)
    assert r.status_code == 200
    events = r.json()["events"]
    # No operator-driven or pxe-client-driven rows yet. The fixture's
    # bootstrap login is an ``auth``-subject row, not the machine /
    # catalog operator activity this test guards against.
    assert all(
        e["actor"] not in {"operator", "pxe-client"} for e in events if e["subject_kind"] != "auth"
    )


def test_events_list_includes_machine_lifecycle(app_client: TestClient) -> None:
    """End-to-end: a /pxe contact + a /machines PUT + a /pxe done
    should each land an event row. Verifies that the recording
    hooks in the handlers actually fire."""
    mac = "aa:bb:cc:dd:ee:ff"
    # Auto-discovery via /pxe -> machine.discovered
    app_client.get(f"/pxe/{mac}")
    # Operator upsert -> machine.upserted (existing record, so not "created")
    app_client.put(
        f"/machines/{mac}",
        json={
            "bty_image_ref": "0" * 64,
            "boot_mode": "bty-flash-always",
        },
        cookies=AUTH,
    )
    # PXE-done signal -> machine.flashed
    app_client.post(f"/pxe/{mac}/done")

    r = app_client.get("/events", cookies=AUTH)
    assert r.status_code == 200
    kinds = [e["kind"] for e in r.json()["events"]]
    # Newest first: discovered came first chronologically, so it's last.
    assert "machine.discovered" in kinds
    assert "machine.upserted" in kinds
    assert "machine.flashed" in kinds
    # All three reference the MAC.
    for e in r.json()["events"]:
        if e["subject_kind"] == "machine":
            assert e["subject_id"] == mac


def test_source_ip_uses_x_forwarded_for_when_trusted_proxy(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When ``BTY_SERVER_TRUSTED_PROXY`` is set, ``_client_ip`` reads
    the leftmost ``X-Forwarded-For`` value instead of
    ``request.client.host``. This is what bty-web operators behind
    nginx / caddy need so audit rows show the real client IP, not
    the proxy's loopback."""
    from bty.web import _config

    monkeypatch.setenv("BTY_SERVER_TRUSTED_PROXY", "1")
    _config.set_active_config(_config.load_config(None))
    mac = "aa:bb:cc:dd:ee:f8"
    app_client.get(f"/pxe/{mac}", headers={"X-Forwarded-For": "192.168.1.42, 10.0.0.1"})
    r = app_client.get("/events", params={"kind": "machine.discovered"}, cookies=AUTH)
    events = r.json()["events"]
    assert events
    assert events[0]["source_ip"] == "192.168.1.42"


def test_source_ip_ignores_x_forwarded_for_when_proxy_not_trusted(
    app_client: TestClient,
) -> None:
    """Without ``BTY_SERVER_TRUSTED_PROXY``, ``X-Forwarded-For`` is
    ignored (the header is client-spoofable). Defensive default: we
    trust only the connection-level ``request.client.host``."""
    mac = "aa:bb:cc:dd:ee:f7"
    app_client.get(f"/pxe/{mac}", headers={"X-Forwarded-For": "1.2.3.4"})
    r = app_client.get("/events", params={"kind": "machine.discovered"}, cookies=AUTH)
    events = r.json()["events"]
    assert events
    # The TestClient connects locally; ``request.client.host`` is
    # ``testclient`` (Starlette default) -- definitely not the
    # spoofed X-F-F value.
    assert events[0]["source_ip"] != "1.2.3.4"


def test_events_carry_source_ip(app_client: TestClient) -> None:
    """Operator + pxe-client events both record the request's
    client host into ``source_ip`` so the audit log can answer
    "what did this IP do?" end-to-end. FastAPI's TestClient sets
    ``request.client.host == 'testclient'``; that flows through
    :func:`normalize_ip` (a no-op for non-IP transports) and
    lands in the row.
    """
    mac = "aa:bb:cc:dd:ee:fe"
    app_client.get(f"/pxe/{mac}")
    app_client.put(f"/machines/{mac}", json={"boot_mode": "ipxe-exit"}, cookies=AUTH)
    r = app_client.get("/events", cookies=AUTH)
    assert r.status_code == 200
    by_kind = {e["kind"]: e for e in r.json()["events"]}
    # Both pxe-client and operator events carry the same
    # testclient host (the ASGI default for httpx TestClient).
    assert by_kind["machine.discovered"]["source_ip"] == "testclient"
    upsert = by_kind.get("machine.created") or by_kind.get("machine.upserted")
    assert upsert is not None
    assert upsert["source_ip"] == "testclient"


def test_events_filter_failed_only_returns_only_failure_kinds(
    app_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``?failed=1`` returns only events whose kind ends in
    ``.failed`` or ``_failed``. Cross-kind shortcut for the
    operator's "show me everything that broke" triage view --
    one toggle instead of cycling through 6+ failure kinds in
    the per-kind dropdown."""
    # Force a netboot.artifacts.fetch.failed event (deterministic).
    from bty.web import _releases

    def _explode(*_a: object, **_kw: object) -> None:
        raise _releases.FetchError("simulated fetch failure")

    monkeypatch.setattr(_releases, "fetch_release", _explode)
    app_client.post(
        "/ui/netboot/fetch-release",
        data={"tag": "v0.0.0"},
        cookies=AUTH,
        follow_redirects=False,
    )
    # Ensure at least one non-failure event exists too (auto-import).
    r = app_client.get("/events", params={"failed": "1"}, cookies=AUTH)
    events = r.json()["events"]
    assert events
    assert all(e["kind"].endswith(".failed") or e["kind"].endswith("_failed") for e in events), [
        e["kind"] for e in events
    ]

    # Without failed=1, the auto-import image.hashed event is in the
    # mix, so the filtered slice is strictly smaller.
    r_all = app_client.get("/events", cookies=AUTH)
    assert len(r_all.json()["events"]) > len(events)


def test_events_filter_by_actor(app_client: TestClient) -> None:
    """``GET /events?actor=operator`` returns only operator-driven
    rows; ``actor=pxe-client`` only PXE check-ins. Powers the
    /ui/events actor dropdown for triaging "show me what
    operators did" vs "show me what targets phoned home"."""
    mac = "aa:bb:cc:dd:ee:fb"
    app_client.get(f"/pxe/{mac}")  # pxe-client: machine.discovered
    app_client.put(  # operator: machine.upserted
        f"/machines/{mac}",
        json={"boot_mode": "ipxe-exit"},
        cookies=AUTH,
    )
    r = app_client.get("/events", params={"actor": "operator"}, cookies=AUTH)
    events = r.json()["events"]
    assert events
    assert all(e["actor"] == "operator" for e in events)

    r = app_client.get("/events", params={"actor": "pxe-client"}, cookies=AUTH)
    events = r.json()["events"]
    assert events
    assert all(e["actor"] == "pxe-client" for e in events)


def test_events_filter_by_source_ip(app_client: TestClient) -> None:
    """``GET /events?source_ip=<ip>`` returns only rows recorded
    with that IP -- the API mirror of the /ui/events filter pivot."""
    app_client.get("/pxe/aa:bb:cc:dd:ee:fd")
    r = app_client.get("/events", params={"source_ip": "testclient"}, cookies=AUTH)
    assert r.status_code == 200
    events = r.json()["events"]
    assert events  # at least one
    assert all(e["source_ip"] == "testclient" for e in events)


def test_catalog_entry_add_sha_failure_logs_event(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When ``POST /catalog/entries`` is given an image_url +
    sha_url and the sha resolution fails (CatalogError from
    bty.catalog.fetch_sha256_for_url), a
    ``catalog.entry.add.failed`` event lands in the audit log
    instead of just a bare 400 response."""
    from bty import catalog as _catalog

    def boom(*_a: object, **_kw: object) -> str:
        raise _catalog.CatalogError("upstream gave 404")

    monkeypatch.setattr(_catalog, "fetch_sha256_for_url", boom)
    r = app_client.post(
        "/catalog/entries",
        json={
            "image_url": "https://example.com/foo.img.gz",
            "sha_url": "https://example.com/foo.sha256",
        },
        cookies=AUTH,
    )
    assert r.status_code == 400
    r = app_client.get("/events", params={"kind": "catalog.entry.add.failed"}, cookies=AUTH)
    events = r.json()["events"]
    assert len(events) == 1
    row = events[0]
    assert row["actor"] == "operator"
    assert row["subject_kind"] == "catalog"
    assert row["subject_id"] == "https://example.com/foo.img.gz"
    assert row["details"] is not None
    assert "upstream gave 404" in row["details"]["error"]


def test_catalog_entry_add_https_populates_resolved_src(app_client: TestClient) -> None:
    """An https catalog entry stores ``resolved_src`` equal to ``src``;
    there's no manifest walk for plain HTTPS, the URL is the URL. Used
    downstream by the withcache HEAD probe + PXE plan rewrite, which
    key on ``resolved_src`` (so the oras vs https paths converge on one
    field)."""
    r = app_client.post(
        "/catalog/entries",
        json={"image_url": "https://example.com/path/foo.img.gz"},
        cookies=AUTH,
    )
    assert r.status_code == 201
    row = next(
        e
        for e in app_client.get("/catalog/entries", cookies=AUTH).json()
        if e["src"] == "https://example.com/path/foo.img.gz"
    )
    assert row["resolved_src"] == "https://example.com/path/foo.img.gz"


def test_catalog_entry_add_oras_populates_resolved_src_with_blob_url(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An oras catalog entry's ``resolved_src`` carries the canonical
    ``https://<host>/v2/<repo>/blobs/sha256:<digest>`` URL produced by
    ``bty.oras.resolve_ref`` at import time. Withcache sees a plain
    HTTPS URL it can warm against; nothing downstream needs to know
    the source was ``oras://``."""
    from withcache import oras as _oras

    blob_url = "https://ghcr.io/v2/safl/nosi/freebsd-14-headless/blobs/sha256:abc123"
    monkeypatch.setattr(
        _oras,
        "resolve_ref",
        lambda *_a, **_kw: _oras.ResolvedBlob(
            blob_url=blob_url,
            headers={"Authorization": "Bearer x"},
            digest="sha256:abc123",
            size=12345,
            title="freebsd-14-headless.img.zst",
        ),
    )
    r = app_client.post(
        "/catalog/entries",
        json={"image_url": "oras://ghcr.io/safl/nosi/freebsd-14-headless:latest"},
        cookies=AUTH,
    )
    assert r.status_code == 201
    row = next(
        e
        for e in app_client.get("/catalog/entries", cookies=AUTH).json()
        if e["src"] == "oras://ghcr.io/safl/nosi/freebsd-14-headless:latest"
    )
    assert row["resolved_src"] == blob_url


def test_events_filter_by_subject_id(app_client: TestClient) -> None:
    """The per-MAC embedded card on /ui/machines/{mac} drives this
    filter -- only events for the given MAC come back. The /pxe
    hit emits two events (discovery + offer) so we assert the
    count is 2 and the subject_id matches."""
    app_client.get("/pxe/aa:bb:cc:dd:ee:01")
    app_client.get("/pxe/aa:bb:cc:dd:ee:02")
    r = app_client.get(
        "/events",
        params={"subject_kind": "machine", "subject_id": "aa:bb:cc:dd:ee:01"},
        cookies=AUTH,
    )
    assert r.status_code == 200
    events = r.json()["events"]
    assert len(events) == 2
    assert {e["subject_id"] for e in events} == {"aa:bb:cc:dd:ee:01"}
    assert {e["kind"] for e in events} == {"machine.discovered", "netboot.pxe.offered"}


def test_ui_events_page_renders(app_client: TestClient) -> None:
    """The /ui/events page renders without 500-ing. Search for a
    string that has no rows yet to exercise the empty-state
    branch (auto-import emits ``image.hashed`` so the unfiltered
    list isn't empty)."""
    r = app_client.get("/ui/events", params={"q": "machine.deleted"}, cookies=AUTH)
    assert r.status_code == 200
    body = r.text
    # Title + search input land in the markup.
    assert "Event log" in body
    assert "/ui/events" in body
    # Inline-pagination empty-state text.
    assert "No events." in body


def test_ui_events_page_renders_filtered(app_client: TestClient) -> None:
    """A populated page shows the row + the kind badge."""
    app_client.get("/pxe/aa:bb:cc:dd:ee:ff")
    r = app_client.get(
        "/ui/events",
        params={"q": "machine.discovered"},
        cookies=AUTH,
    )
    assert r.status_code == 200
    body = r.text
    assert "machine.discovered" in body
    assert "aa:bb:cc:dd:ee:ff" in body


def test_ui_events_page_search_narrows_results(
    app_client: TestClient,
) -> None:
    """The single ``?q=`` substring search is the only filter on
    /ui/events after the v0.57 simplification. Typing an actor /
    kind / IP / subject substring narrows the table; clearing the
    search restores the full view."""
    app_client.get("/pxe/aa:bb:cc:dd:ee:f9")
    app_client.put(
        "/machines/aa:bb:cc:dd:ee:f9",
        json={"boot_mode": "ipxe-exit"},
        cookies=AUTH,
    )
    # Unfiltered view: rows exist; the inline-pagination total line
    # shows the full count (no "of <N> events" prefix tied to a
    # filter).
    full = app_client.get("/ui/events", cookies=AUTH).text
    assert "aa:bb:cc:dd:ee:f9" in full
    # Search by actor: only operator-recorded rows match.
    filtered = app_client.get("/ui/events", params={"q": "operator"}, cookies=AUTH).text
    assert "operator" in filtered


def test_ui_events_page_renders_failure_with_danger_badge(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Failure-kind events (anything ending ``.failed``) render
    with the ``bg-danger`` Bootstrap badge so they pop in a long
    log instead of blending in with their success siblings
    (``netboot.artifacts.fetched`` vs ``netboot.artifacts.fetch.failed``,
    same family / different colour). Guards the failed-kind branch
    in the events / per-machine templates against a future refactor
    of the badge map."""
    # Trigger a netboot.artifacts.fetch.failed event (deterministic --
    # monkeypatch the fetch to raise FetchError).
    from bty.web import _releases

    def _explode(*_a: object, **_kw: object) -> None:
        raise _releases.FetchError("simulated fetch failure")

    monkeypatch.setattr(_releases, "fetch_release", _explode)
    app_client.post(
        "/ui/netboot/fetch-release",
        data={"tag": "v0.0.0"},
        cookies=AUTH,
        follow_redirects=False,
    )
    r = app_client.get(
        "/ui/events",
        params={"q": "netboot.artifacts.fetch.failed"},
        cookies=AUTH,
    )
    assert r.status_code == 200
    body = r.text
    assert "netboot.artifacts.fetch.failed" in body
    # Danger badge appears in the rendered row.
    assert "bg-danger" in body


def test_ui_events_page_shows_source_ip_column(app_client: TestClient) -> None:
    """The ``Source IP`` column is in the table header and populated
    cells render as click-pivot links to ``/ui/events?q=<ip>`` so
    the operator can drill into a single client's activity. Post-
    v0.57 the click-pivot uses the unified ``?q=`` substring
    search (was ``?source_ip=``)."""
    app_client.get("/pxe/aa:bb:cc:dd:ee:fc")
    r = app_client.get("/ui/events", cookies=AUTH)
    assert r.status_code == 200
    body = r.text
    # Column header.
    assert "Source IP" in body
    # Click-pivot link with the test client's host.
    assert "/ui/events?q=testclient" in body


# ---------- /boot and /images file serving --------------------


def test_boot_artifact_serves_file(app_client: TestClient) -> None:
    r = app_client.get(f"/boot/{ARTIFACT_NAMES[0]}")
    assert r.status_code == 200
    assert r.content == b"fake-kernel"


def test_boot_artifact_404_for_missing(app_client: TestClient) -> None:
    r = app_client.get("/boot/does-not-exist.bin")
    assert r.status_code == 404


# ---------- HTTP-Boot (UEFI arch 16) -----------------------------------
#
# UEFI HTTP-Boot fetches the bootfile via plain HTTP -- no TFTP --
# from the URL the LAN DHCP server hands it (option 60 = "HTTPClient",
# option 67 = "http://<bty>:8080/boot/ipxe.efi"). The firmware
# expects standard HTTP semantics: 200 OK with Content-Length set so
# it can size the boot buffer before the GET completes, HEAD support
# for cheap probing, and no auth gate (PXE clients have no session
# cookie). These pin those expectations against the ``/boot/{name}``
# route so a future template-render or auth-tightening commit
# doesn't silently break HTTP-Boot.


def test_http_boot_serves_ipxe_efi_with_content_length(
    app_client: TestClient, tmp_path: Path
) -> None:
    """HTTP-Boot firmware allocates a buffer for the bootfile based
    on Content-Length BEFORE the body arrives. Without that header
    some implementations refuse to start the download."""
    fake_efi = b"FAKE-EFI-BINARY!" * 256  # 4 KiB sentinel
    (tmp_path / "boot" / "ipxe.efi").write_bytes(fake_efi)
    r = app_client.get("/boot/ipxe.efi")
    assert r.status_code == 200
    assert "content-length" in r.headers
    assert int(r.headers["content-length"]) == len(fake_efi)
    assert r.content == fake_efi


def test_http_boot_head_request_works(app_client: TestClient, tmp_path: Path) -> None:
    """Some UEFI HTTP-Boot implementations probe with HEAD before
    issuing the GET (to size the fetch / verify the URL). HEAD must
    return the same status + Content-Length as GET, no body."""
    fake_efi = b"FAKE-EFI" * 128  # 1 KiB
    (tmp_path / "boot" / "ipxe.efi").write_bytes(fake_efi)
    r = app_client.head("/boot/ipxe.efi")
    assert r.status_code == 200
    assert int(r.headers["content-length"]) == len(fake_efi)
    assert r.content == b""  # HEAD has no body


def test_http_boot_is_unauthenticated(app_client: TestClient, tmp_path: Path) -> None:
    """PXE clients have no session cookie -- the /boot/* route must
    serve without auth. Pin so a future ``Depends(require_auth)`` on
    the API surface doesn't accidentally creep onto this route."""
    fake_efi = b"FAKE-EFI" * 16
    (tmp_path / "boot" / "ipxe.efi").write_bytes(fake_efi)
    # No cookies= argument -- explicitly unauthenticated client.
    r = app_client.get("/boot/ipxe.efi", cookies={})
    assert r.status_code == 200
    assert r.content == fake_efi


def test_http_boot_returns_octet_stream_content_type(
    app_client: TestClient, tmp_path: Path
) -> None:
    """The .efi extension shouldn't accidentally pick up a text/html
    MIME type from FastAPI's mimetypes guess. application/octet-stream
    is the safest default for a UEFI binary."""
    (tmp_path / "boot" / "ipxe.efi").write_bytes(b"FAKE-EFI")
    r = app_client.get("/boot/ipxe.efi")
    assert r.status_code == 200
    ct = r.headers.get("content-type", "").lower()
    # Either application/octet-stream or a *.efi-specific MIME that's
    # not text/html. The wire shape doesn't strictly care; what we
    # guard against is the "served as text" case.
    assert "text/html" not in ct
    assert "text/plain" not in ct


def test_http_boot_404_for_missing_ipxe_efi(app_client: TestClient) -> None:
    """Operator deployment forgets to stage ipxe.efi under boot_root
    -- the firmware should get a clean 404 rather than a hang or a
    redirect to the login page."""
    r = app_client.get("/boot/ipxe.efi")
    assert r.status_code == 404


def test_http_boot_arch_specific_filenames_routable(app_client: TestClient, tmp_path: Path) -> None:
    """The router-config cheatsheet on /ui/settings lists arch-
    specific bootfiles (ipxe-i386.efi, ipxe-arm64.efi, etc.) for
    targets where the default ipxe.efi (x86_64) isn't appropriate.
    The route must serve any operator-staged file by name."""
    (tmp_path / "boot" / "ipxe-arm64.efi").write_bytes(b"ARM64-EFI")
    r = app_client.get("/boot/ipxe-arm64.efi")
    assert r.status_code == 200
    assert r.content == b"ARM64-EFI"


def test_boot_artifact_rejects_traversal(app_client: TestClient) -> None:
    """Slash in a single-segment ``{name}`` is impossible (FastAPI's
    path converter splits on /), but the explicit guards reject the
    edge cases too: empty, dot, dotdot, encoded.

    ``follow_redirects=False`` because some httpx URL-normalisations
    on ``..`` resolve to ``/`` which now 303s to ``/ui/login`` (the
    root-redirect for usability). The boot handler never serves a
    file; that's what we're asserting."""
    for bad in ("", ".", ".."):
        r = app_client.get(f"/boot/{bad}", follow_redirects=False)
        # Some encodings 404 / 422 from FastAPI's router before reaching
        # the boot handler; others 400 from our guard; ``..`` URL-
        # normalises to ``/`` which 303s. None of these are 200 from
        # the boot handler - that's the only thing this test cares
        # about.
        assert r.status_code != 200


def test_serve_image_404_for_unknown_sha(app_client: TestClient) -> None:
    """A 64-hex-char key that doesn't match any oras catalog entry
    returns 404 cleanly (not a server error). v0.40: bty-web is out
    of the bytes path entirely for https sources; ``/images`` only
    serves oras blobs now."""
    r = app_client.get("/images/" + "0" * 64)
    assert r.status_code == 404


# ---------- /catalog endpoints ---------------------------------------------


# ---------- /workers/backups HTTP layer -----------------------------------
#
# The BackupManager has direct tests in test_web_backup_manager.py, but the
# HTTP layer that wraps it (GET/POST/DELETE /workers/backups) had zero
# end-to-end coverage. Three small tests fill the gap.


def test_workers_backups_get_requires_auth(app_client: TestClient) -> None:
    """Unauth'd GET returns 401 -- the backup list reveals operator-
    pressed timings + bundle ids, so it's behind the same cookie as
    every other operator-facing JSON API."""
    r = app_client.get("/workers/backups")
    assert r.status_code == 401


def test_workers_backups_get_empty_returns_stable_shape(app_client: TestClient) -> None:
    """Fresh fixture: no backups have run. The endpoint still returns
    a parseable JSON envelope so the Backups page's polling loop
    renders an empty-state row instead of crashing on a None field."""
    r = app_client.get("/workers/backups", cookies=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["backups"] == []
    assert "backups_root" in body
    assert "max_parallel" in body
    assert isinstance(body["max_parallel"], int) and body["max_parallel"] >= 1


def test_workers_backups_post_runs_to_completion(app_client: TestClient) -> None:
    """POST without body enqueues a manual backup. Status code is 202
    (accepted); body carries the freshly-minted backup_id. A
    follow-up GET reflects the job in the list."""
    r = app_client.post("/workers/backups", json={"trigger": "manual"}, cookies=AUTH)
    assert r.status_code == 202, r.text
    enqueued = r.json()
    assert enqueued["status"] in ("queued", "running", "completed")
    assert "backup_id" in enqueued
    # Poll the list until the backup reaches a terminal state. The
    # metadata-only export finishes in milliseconds so the cap is generous.
    import time

    deadline = time.monotonic() + 5.0
    body: dict = {}
    while time.monotonic() < deadline:
        body = app_client.get("/workers/backups", cookies=AUTH).json()
        if body["backups"] and body["backups"][0]["status"] in (
            "completed",
            "failed",
            "cancelled",
        ):
            break
        time.sleep(0.05)
    assert body.get("backups"), "the enqueued backup must surface on the list"
    assert body["backups"][0]["backup_id"] == enqueued["backup_id"]


def test_workers_backups_delete_unknown_returns_404(app_client: TestClient) -> None:
    """Cancelling against an unknown backup_id is a 404 (not 500),
    matching the sibling /catalog/downloads cancel shape."""
    r = app_client.delete("/workers/backups/2026-99-99T00-00-00Z", cookies=AUTH)
    assert r.status_code == 404
    assert "no active backup" in r.json()["detail"]


def test_workers_backups_delete_requires_auth(app_client: TestClient) -> None:
    """Cancel needs the cookie; otherwise an unauth'd client could
    cancel operator-initiated backups."""
    r = app_client.delete("/workers/backups/anything")
    assert r.status_code == 401


def test_workers_backups_post_emits_lifecycle_events(app_client: TestClient) -> None:
    """v0.33.29+: POST /workers/backups triggers the full lifecycle:
    backup.create.requested (operator click) -> backup.create.started
    (worker pickup) -> backup.created (terminal success). Lets an
    operator scrolling /ui/events see "did the click register? did
    the worker pick it up? did it finish?" without inferring from
    absence."""
    import time

    r = app_client.post("/workers/backups", json={"trigger": "manual"}, cookies=AUTH)
    assert r.status_code == 202, r.text
    backup_id = r.json()["backup_id"]
    # Backup is metadata-only; finishes in milliseconds. Poll
    # briefly for the terminal.
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        body = app_client.get("/workers/backups", cookies=AUTH).json()
        match = next((b for b in body["backups"] if b["backup_id"] == backup_id), None)
        if match and match["status"] in ("completed", "failed"):
            break
        time.sleep(0.05)
    events = app_client.get(
        "/events",
        params={"subject_kind": "backup", "subject_id": backup_id},
        cookies=AUTH,
    ).json()["events"]
    kinds = [e["kind"] for e in reversed(events)]  # oldest first
    assert "backup.create.requested" in kinds, kinds
    assert "backup.create.started" in kinds, kinds
    assert "backup.created" in kinds, kinds
    # Ordering: requested -> started -> created.
    assert (
        kinds.index("backup.create.requested")
        < kinds.index("backup.create.started")
        < kinds.index("backup.created")
    ), kinds


def test_workers_backups_post_invalid_trigger_returns_422(app_client: TestClient) -> None:
    """``trigger`` must be one of ``{"manual", "scheduled"}``. An
    unknown value gets rejected at the Pydantic layer (422) rather
    than silently triggering a backup with a bogus tag in the
    ``backup.created`` event."""
    r = app_client.post(
        "/workers/backups",
        json={"trigger": "totally-bogus"},
        cookies=AUTH,
    )
    assert r.status_code == 422, r.text


# ---------- HEAD /boot/{name} edge cases ----------------------------------
#
# UEFI HTTP-Boot firmware HEADs the URL before GET; if the artifact is
# missing it must surface 404 cleanly so the firmware can fall back to
# the next boot order entry, not stall on a server error.


def test_http_boot_head_missing_artifact_returns_404(app_client: TestClient) -> None:
    """HEAD against a non-existent /boot artifact returns 404 (with
    empty body, matching the HEAD contract). Without this, UEFI
    HTTP-Boot firmware that does a HEAD probe before GET would see
    a server error and abort the boot order traversal."""
    r = app_client.head("/boot/definitely-not-an-artifact.bin")
    assert r.status_code == 404
    assert r.content == b""


# ---------- HEAD /images/{key} edge cases ---------------------------------


def test_head_images_missing_returns_404(app_client: TestClient) -> None:
    """Same UEFI HEAD-probe story for /images/<sha>. A missing image
    returns 404 not 500."""
    # 64-hex pattern but file isn't on disk.
    nonexistent = "0" * 64
    r = app_client.head(f"/images/{nonexistent}")
    assert r.status_code == 404
    assert r.content == b""


# ---------- DELETE /catalog/entries edge cases ----------------------------


def test_delete_catalog_entry_unknown_src_returns_404(app_client: TestClient) -> None:
    """Deleting a src that isn't in the catalog -> 404, not 500.
    Operator-clickable Delete on a stale UI tab shouldn't crash
    bty-web."""
    r = app_client.request(
        "DELETE",
        "/catalog/entries?src=https://example.invalid/never-existed.img",
        cookies=AUTH,
    )
    assert r.status_code == 404
    assert "no catalog entry" in r.json()["detail"]


def test_delete_catalog_entry_requires_auth(app_client: TestClient) -> None:
    """The catalog delete is operator-only; unauth'd clients get 401."""
    r = app_client.request("DELETE", "/catalog/entries?src=anything")
    assert r.status_code == 401


# ---------- full reflash-cycle state machine ------------------------------
#
# bty's reason for existence. The interplay of:
#
#   1. /pxe/{mac}            -- server picks a chain based on policy
#                                + saw_flasher_boot bit
#   2. /boot/{name}?mac=X    -- arming step: live env fetched a
#                                /boot artifact => sets bit
#   3. /pxe/{mac}/done       -- live env reports success (does NOT
#                                mutate boot_mode; v0.25+ contract)
#   4. /pxe/{mac}            -- next contact: sanboot vs flash chain
#                                decided by bit + policy
#
# Only one slice was tested before (operator-upsert resets the bit).
# These tests pin the full state machine so the v0.30.2-class bug
# ("flash-once behaved like flash-always") can't regress silently.


def _seed_flashable_machine(
    app_client: TestClient, mac: str, *, boot_mode: str, monkeypatch: pytest.MonkeyPatch
) -> str:
    """Set up a machine that's flash-ready: catalog entry exists, ref
    bound, target_disk_serial picked. Returns the ref so callers can
    cross-reference if they need to."""
    flash_sha = "deadbeef" * 8
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda *_a, **_kw: _MockResp(b"", {"Content-Length": "0"}),
    )
    monkeypatch.setattr("bty.catalog.fetch_sha256_for_url", lambda *_a, **_kw: flash_sha)
    add = app_client.post(
        "/catalog/entries",
        json={
            "image_url": f"https://example.invalid/{mac.replace(':', '')}.img.gz",
            "sha_url": f"https://example.invalid/{mac.replace(':', '')}.img.gz.sha256",
        },
        cookies=AUTH,
    )
    assert add.status_code == 201, add.text
    ref = add.json()["bty_image_ref"]
    app_client.put(
        f"/machines/{mac}",
        json={
            "bty_image_ref": ref,
            "boot_mode": boot_mode,
            "target_disk_serial": "REFLASH-SERIAL",
        },
        cookies=AUTH,
    )
    return ref


def _saw_flasher_bit(app_client: TestClient, mac: str) -> int:
    """Read the saw_flasher_boot bit directly from state.db."""
    from bty.web import _db as _bty_db

    state_path: Path = app_client.app.state.state_path  # type: ignore[attr-defined]
    with _bty_db.open_db(state_path) as conn:
        return int(
            conn.execute("SELECT saw_flasher_boot FROM machines WHERE mac = ?", (mac,)).fetchone()[
                "saw_flasher_boot"
            ]
        )


def _latest_offer_kind(app_client: TestClient, mac: str) -> str:
    """Return the offer_kind from the most recent ``netboot.pxe.offered``
    event for ``mac``. Tests use this to assert which iPXE chain the
    server handed back without scraping the rendered text."""
    r = app_client.get(
        "/events",
        params={"subject_kind": "machine", "subject_id": mac, "kind": "netboot.pxe.offered"},
        cookies=AUTH,
    )
    events = r.json()["events"]
    assert events, f"no netboot.pxe.offered events for {mac!r}"
    # /events is newest-first.
    return str(events[0]["details"]["offer_kind"])


def test_reflash_lifecycle_bty_flash_always_alternates(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``bty-flash-always`` alternates flash-chain vs sanboot across
    reboots so the box actually boots its just-flashed disk once
    before being reflashed.

    v0.33.24+: the sanboot consume requires BOTH ``saw_flasher_boot``
    (iPXE armed) AND ``last_flashed_at`` (live env actually called
    /pxe/{mac}/done). Cycle:

        PXE  -> flash chain (offer=bty-flash-always)
        /boot?mac= (arming step)
        /pxe/{mac}/done (live env finished -> last_flashed_at set)
        PXE  -> sanboot the just-flashed disk (offer=bty-flash-always-sanboot)
                + bit CLEARED
        PXE  -> flash chain again (offer=bty-flash-always)
                + bit stays 0 (cleared until next /boot)
    """
    mac = "aa:bb:cc:dd:ee:a1"
    _seed_flashable_machine(app_client, mac, boot_mode="bty-flash-always", monkeypatch=monkeypatch)

    # Iter 1: PXE -> flash chain offered.
    r = app_client.get(f"/pxe/{mac}", headers={"Host": "bty.local:8080"})
    assert r.status_code == 200
    assert _latest_offer_kind(app_client, mac) == "bty-flash-always"
    assert _saw_flasher_bit(app_client, mac) == 0

    # Live env booted -> fetches a /boot artifact with ?mac= -> arm.
    app_client.get(f"/boot/{ARTIFACT_NAMES[0]}?mac={mac}", headers={"Host": "bty.local:8080"})
    assert _saw_flasher_bit(app_client, mac) == 1
    # Live env actually completed -> /pxe/{mac}/done POST.
    app_client.post(f"/pxe/{mac}/done")

    # Iter 2: PXE -> sanboot the just-flashed disk; bit cleared.
    r = app_client.get(f"/pxe/{mac}", headers={"Host": "bty.local:8080"})
    assert r.status_code == 200
    assert _latest_offer_kind(app_client, mac) == "bty-flash-always-sanboot"
    assert _saw_flasher_bit(app_client, mac) == 0, "bit must be cleared after sanboot serve"

    # Iter 3: PXE again (no /boot fetch since iter 2) -> flash chain.
    r = app_client.get(f"/pxe/{mac}", headers={"Host": "bty.local:8080"})
    assert r.status_code == 200
    assert _latest_offer_kind(app_client, mac) == "bty-flash-always"
    assert _saw_flasher_bit(app_client, mac) == 0


def test_reflash_lifecycle_bty_flash_once_is_terminal(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``bty-flash-once`` is terminal: after the box has been flashed
    once + booted the disk, every subsequent /pxe contact must
    serve sanboot. The bit STAYS armed (NOT cleared) so a re-PXE
    can't re-trigger the flash chain.

    v0.33.24+: requires the /pxe/{mac}/done POST to fire before
    the sanboot consume path -- iPXE arm alone (without /done) now
    re-serves the flash chain so a crashed flasher self-heals
    instead of looping on a stuck sanboot.

    Cycle:
        PXE  -> flash chain (offer=bty-flash-once)
        /boot?mac= (arming step)
        /pxe/{mac}/done (live env finished -> last_flashed_at set)
        PXE  -> sanboot (offer=bty-flash-once-sanboot) + bit KEPT
        PXE  -> sanboot again (offer=bty-flash-once-sanboot) + bit KEPT
    """
    mac = "aa:bb:cc:dd:ee:a2"
    _seed_flashable_machine(app_client, mac, boot_mode="bty-flash-once", monkeypatch=monkeypatch)

    # Iter 1: flash chain.
    app_client.get(f"/pxe/{mac}", headers={"Host": "bty.local:8080"})
    assert _latest_offer_kind(app_client, mac) == "bty-flash-once"
    assert _saw_flasher_bit(app_client, mac) == 0

    # Live env arms the bit AND completes (/done).
    app_client.get(f"/boot/{ARTIFACT_NAMES[0]}?mac={mac}", headers={"Host": "bty.local:8080"})
    assert _saw_flasher_bit(app_client, mac) == 1
    app_client.post(f"/pxe/{mac}/done")

    # Iter 2: sanboot, bit KEPT (this is the terminal contract --
    # was broken pre-v0.30.2: the code cleared the bit for both
    # flash-once and flash-always, making flash-once reflash on
    # next /pxe).
    app_client.get(f"/pxe/{mac}", headers={"Host": "bty.local:8080"})
    assert _latest_offer_kind(app_client, mac) == "bty-flash-once-sanboot"
    assert _saw_flasher_bit(app_client, mac) == 1, (
        "REGRESSION: bty-flash-once must KEEP the bit armed (terminal state); "
        "clearing it would make the next /pxe serve the flash chain and reflash"
    )

    # Iter 3: still sanboot -- forever, until operator re-saves.
    app_client.get(f"/pxe/{mac}", headers={"Host": "bty.local:8080"})
    assert _latest_offer_kind(app_client, mac) == "bty-flash-once-sanboot"
    assert _saw_flasher_bit(app_client, mac) == 1


def test_reflash_lifecycle_crashed_flasher_retries_chain(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """v0.33.24+: if the live env arms ``saw_flasher_boot`` (via /boot
    fetch) but crashes BEFORE calling /pxe/{mac}/done, the next /pxe
    contact must re-serve the flash chain -- NOT sanboot a
    half-flashed disk.

    Pre-fix behaviour was: armed -> sanboot regardless of /done.
    For bty-flash-once that meant a crashed flasher landed in a
    permanently-stuck-sanboot state requiring operator intervention.
    For bty-flash-always the next cycle self-healed via the
    bit-clear, but at the cost of one wasted sanboot of an
    unflashed disk.

    With the gate on ``last_flashed_at``, both modes auto-retry the
    chain until /done lands.
    """
    mac = "aa:bb:cc:dd:ee:a7"
    _seed_flashable_machine(app_client, mac, boot_mode="bty-flash-once", monkeypatch=monkeypatch)

    # Iter 1: flash chain.
    app_client.get(f"/pxe/{mac}", headers={"Host": "bty.local:8080"})
    assert _latest_offer_kind(app_client, mac) == "bty-flash-once"

    # Live env boots + arms -- then crashes before /done.
    app_client.get(f"/boot/{ARTIFACT_NAMES[0]}?mac={mac}", headers={"Host": "bty.local:8080"})
    assert _saw_flasher_bit(app_client, mac) == 1
    # NO /pxe/{mac}/done call.

    # Iter 2: PXE -> RE-SERVES the flash chain (NOT sanboot).
    app_client.get(f"/pxe/{mac}", headers={"Host": "bty.local:8080"})
    offer = _latest_offer_kind(app_client, mac)
    assert offer == "bty-flash-once", (
        f"REGRESSION (v0.33.24): armed without /done must re-serve the flash chain; "
        f"got {offer!r}. Pre-fix served bty-flash-once-sanboot and stuck the box."
    )
    # The audit event details the retry reason for operator visibility.
    r = app_client.get(
        "/events",
        params={"subject_kind": "machine", "subject_id": mac, "kind": "netboot.pxe.offered"},
        cookies=AUTH,
    )
    latest = r.json()["events"][0]
    assert latest["details"].get("retry_after_armed_no_done") is True

    # Now the (retry) live env succeeds + posts /done.
    app_client.get(f"/boot/{ARTIFACT_NAMES[0]}?mac={mac}", headers={"Host": "bty.local:8080"})
    app_client.post(f"/pxe/{mac}/done")

    # Iter 3: NOW the sanboot consume fires (armed + last_flashed_at).
    app_client.get(f"/pxe/{mac}", headers={"Host": "bty.local:8080"})
    assert _latest_offer_kind(app_client, mac) == "bty-flash-once-sanboot"


def test_reflash_lifecycle_inventory_crashed_live_env_retries(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same fix shape for bty-inventory: armed without /pxe/{mac}/inventory
    POST -> re-serve the inventory chain instead of sanbooting an
    empty disk."""
    mac = "aa:bb:cc:dd:ee:a8"
    app_client.put(f"/machines/{mac}", json={"boot_mode": "bty-inventory"}, cookies=AUTH)

    # Iter 1: inventory chain.
    app_client.get(f"/pxe/{mac}", headers={"Host": "bty.local:8080"})
    assert _latest_offer_kind(app_client, mac) == "bty-inventory"

    # Live env arms but crashes before inventory POST.
    app_client.get(f"/boot/{ARTIFACT_NAMES[0]}?mac={mac}", headers={"Host": "bty.local:8080"})
    assert _saw_flasher_bit(app_client, mac) == 1
    # NO /pxe/{mac}/inventory POST.

    # Iter 2: PXE -> re-serves inventory chain.
    app_client.get(f"/pxe/{mac}", headers={"Host": "bty.local:8080"})
    assert _latest_offer_kind(app_client, mac) == "bty-inventory"
    r = app_client.get(
        "/events",
        params={"subject_kind": "machine", "subject_id": mac, "kind": "netboot.pxe.offered"},
        cookies=AUTH,
    )
    latest = r.json()["events"][0]
    assert latest["details"].get("retry_after_armed_no_post") is True


def test_pxe_done_does_not_mutate_boot_mode(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``POST /pxe/{mac}/done`` updates last_flashed_at + records a
    machine.flashed event, but MUST NOT mutate boot_mode (the
    mode/state split: mode is the operator's intent, state is the
    saw_flasher_boot bit). Pre-v0.25 this flipped flash-once ->
    sanboot, which lied about the operator's configured mode."""
    mac = "aa:bb:cc:dd:ee:a3"
    _seed_flashable_machine(app_client, mac, boot_mode="bty-flash-once", monkeypatch=monkeypatch)

    r = app_client.post(f"/pxe/{mac}/done")
    assert r.status_code == 204

    # Mode unchanged.
    machine = app_client.get(f"/machines/{mac}", cookies=AUTH).json()
    assert machine["boot_mode"] == "bty-flash-once"
    assert machine["last_flashed_at"] is not None


def test_boot_fetch_does_not_arm_sanboot_machine(app_client: TestClient) -> None:
    """If a machine in ``ipxe-exit`` (operator chose "boot local disk")
    somehow fetches /boot with ?mac= (mis-typed by a curl test, MAC
    rotation, etc.), the bit MUST NOT arm -- those policies don't
    consume the bit and a stray arm would leak into a future flash
    cycle if the operator later switched to bty-flash-always."""
    mac = "aa:bb:cc:dd:ee:a4"
    app_client.put(
        f"/machines/{mac}",
        json={"boot_mode": "ipxe-exit"},
        cookies=AUTH,
    )
    app_client.get(f"/boot/{ARTIFACT_NAMES[0]}?mac={mac}", headers={"Host": "bty.local:8080"})
    from bty.web import _db as _bty_db

    state_path: Path = app_client.app.state.state_path  # type: ignore[attr-defined]
    with _bty_db.open_db(state_path) as conn:
        bit = conn.execute(
            "SELECT saw_flasher_boot FROM machines WHERE mac = ?", (mac,)
        ).fetchone()["saw_flasher_boot"]
    assert bit == 0, (
        "the arming WHERE clause must confine saw_flasher_boot to "
        "bty-flash-always / bty-flash-once / bty-inventory -- a stray "
        "/boot?mac= on a sanboot machine MUST NOT leak the bit"
    )


def test_boot_fetch_arms_bty_inventory(app_client: TestClient) -> None:
    """``bty-inventory`` also consumes saw_flasher_boot (boot live
    env, post inventory, sanboot once, then re-cycle). /boot?mac=
    for an inventory-mode machine MUST arm so the next /pxe serves
    the post-inventory sanboot."""
    mac = "aa:bb:cc:dd:ee:a5"
    # bty-inventory is the default on auto-discovered machines.
    app_client.get(f"/pxe/{mac}", headers={"Host": "bty.local:8080"})
    app_client.get(f"/boot/{ARTIFACT_NAMES[0]}?mac={mac}", headers={"Host": "bty.local:8080"})
    from bty.web import _db as _bty_db

    state_path: Path = app_client.app.state.state_path  # type: ignore[attr-defined]
    with _bty_db.open_db(state_path) as conn:
        bit = conn.execute(
            "SELECT saw_flasher_boot FROM machines WHERE mac = ?", (mac,)
        ).fetchone()["saw_flasher_boot"]
    assert bit == 1


def test_pxe_plan_oras_entry_ships_raw_url_for_live_env_to_resolve(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """v0.60.0: with no withcache configured (or a cold cache for an
    oras entry) the plan ships the original ``oras://`` URL. The live
    env's bty TUI handles the OCI dance internally via
    ``withcache.oras`` (resolve_ref + bearer + curl). The plan's
    ``format`` field is the authoritative format hint -- the URL is
    no longer required to carry an extension."""
    from withcache import oras as _oras

    fake_blob = _oras.ResolvedBlob(
        blob_url="https://ghcr.io/v2/safl/nosi/blobs/sha256:" + "a" * 64,
        headers={},
        digest="sha256:" + "a" * 64,
        size=42_000_000,
        title="nosi fedora-sysdev (x86_64, rolling)",  # no extension!
    )
    monkeypatch.setattr(_oras, "resolve_ref", lambda url: fake_blob)

    src = "oras://ghcr.io/safl/nosi/fedora-sysdev:latest"
    r = app_client.post("/catalog/entries", json={"image_url": src}, cookies=AUTH)
    assert r.status_code == 201, r.text
    body = r.json()
    ref = body["bty_image_ref"]
    # The catalog entry stores format derived from the title's
    # extension (here None -> defaults to "img.gz" per the handler).
    assert body["format"] == "img.gz"
    assert body["name"] == "nosi fedora-sysdev (x86_64, rolling)"

    mac = "aa:bb:cc:dd:ee:b1"
    app_client.put(
        f"/machines/{mac}",
        json={
            "bty_image_ref": ref,
            "boot_mode": "bty-flash-always",
            "target_disk_serial": "ORAS-SERIAL",
        },
        cookies=AUTH,
    )
    plan = app_client.get(f"/pxe/{mac}/plan", headers={"Host": "bty.local:8080"}).json()
    assert plan["mode"] == "flash"
    assert plan["target_disk_serial"] == "ORAS-SERIAL"
    # The descriptive name lands on the plan's ``name`` field; the
    # live env displays this on the flash screen.
    assert plan["name"] == "nosi fedora-sysdev (x86_64, rolling)"
    # The plan also carries the format explicitly -- this is the
    # format hint the live env uses (extension-detection from URL
    # is no longer required since the URL is now a raw ``oras://``).
    assert plan["format"] == "img.gz"
    # No withcache configured + oras src -> plan ships the raw URL.
    # The live env's ``flash._curl_args_for_source`` then resolves
    # via ``withcache.oras`` + curls the resolved blob URL.
    assert plan["image"] == src
    # The legacy ``/images/{ref}/<name>`` proxy route was removed in
    # v0.60.0; the plan must NOT route oras entries through bty-web.
    assert "/images/" not in plan["image"], (
        "REGRESSION: oras plan must ship the raw oras:// URL "
        f"(or withcache URL when configured), not the bty-web "
        f"/images proxy; got: {plan['image']!r}"
    )


def test_pxe_plan_keeps_name_when_it_has_extension(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The synthesis path triggers ONLY when the catalog name lacks a
    detectable extension. For an http(s) entry with a real filename
    (``demo.img.gz``), the URL's last segment must keep that name
    verbatim -- not silently rewrite to ``image.img.gz``."""

    def fake_urlopen(*_a, **_kw):
        return _MockResp(b"", headers={"Content-Length": "12345"})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    flash_sha = "0123456789abcdef" * 4
    monkeypatch.setattr("bty.catalog.fetch_sha256_for_url", lambda *_a, **_kw: flash_sha)
    r = app_client.post(
        "/catalog/entries",
        json={
            "image_url": "https://example.invalid/demo.img.gz",
            "sha_url": "https://example.invalid/demo.img.gz.sha256",
        },
        cookies=AUTH,
    )
    assert r.status_code == 201
    ref = r.json()["bty_image_ref"]

    mac = "aa:bb:cc:dd:ee:b2"
    app_client.put(
        f"/machines/{mac}",
        json={
            "bty_image_ref": ref,
            "boot_mode": "bty-flash-always",
            "target_disk_serial": "PLAIN-SERIAL",
        },
        cookies=AUTH,
    )
    plan = app_client.get(f"/pxe/{mac}/plan", headers={"Host": "bty.local:8080"}).json()
    assert plan["mode"] == "flash"
    assert plan["name"] == "demo.img.gz"
    # https + no withcache -> origin URL straight through. The
    # synthesis-when-extension-is-missing path only fires when
    # bty-web builds a /images URL (still hit for oras entries).
    assert plan["image"] == "https://example.invalid/demo.img.gz"


def test_pxe_plan_orphan_ref_falls_back_to_interactive(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Machine is bound to a ``bty_image_ref`` whose catalog entry
    has been DELETED. /pxe/{mac}/plan must NOT 500 -- the live env
    should be able to fall back to the wizard and let the operator
    pick another image. Real scenario: operator binds machine A to
    catalog entry X, then deletes entry X, then machine A reboots.
    """
    # Bind to a 64-hex ref that has no catalog row.
    mac = "aa:bb:cc:dd:ee:b3"
    orphan_ref = "deadbeef" * 8
    app_client.put(
        f"/machines/{mac}",
        json={
            "bty_image_ref": orphan_ref,
            "boot_mode": "bty-flash-always",
            "target_disk_serial": "ORPHAN-SERIAL",
        },
        cookies=AUTH,
    )
    plan = app_client.get(f"/pxe/{mac}/plan", headers={"Host": "bty.local:8080"}).json()
    # No catalog row to resolve -> live env falls back to wizard.
    assert plan["mode"] == "interactive", (
        f"orphan ref must fall back to wizard, not crash; got {plan!r}"
    )
    assert "catalog" in plan


def test_reflash_lifecycle_pxe_offered_event_per_iteration(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every /pxe contact lands one netboot.pxe.offered event with
    the offer_kind. Audit-log timeline = full reflash history. If a
    future refactor stops emitting events on the sanboot branch,
    operators lose visibility into "did the box come back?".

    Real-world cadence (PXE-first BIOS):

        PXE  flash  -- bit=0 -> flash chain
        /boot       -- bit -> 1
        PXE sanboot -- bit=1 -> sanboot, bit -> 0
        PXE flash   -- box reboots without re-fetching /boot
                       (the disk booted; bit stays 0) -> flash again
        /boot       -- bit -> 1
        PXE sanboot -- bit=1 -> sanboot, bit -> 0
    """
    mac = "aa:bb:cc:dd:ee:a6"
    _seed_flashable_machine(app_client, mac, boot_mode="bty-flash-always", monkeypatch=monkeypatch)

    # Cycle 1: flash, arm, /done, sanboot.
    app_client.get(f"/pxe/{mac}", headers={"Host": "bty.local:8080"})  # flash
    app_client.get(f"/boot/{ARTIFACT_NAMES[0]}?mac={mac}", headers={"Host": "bty.local:8080"})
    app_client.post(f"/pxe/{mac}/done")  # v0.33.24+: required to graduate to sanboot
    app_client.get(f"/pxe/{mac}", headers={"Host": "bty.local:8080"})  # sanboot + clear
    # Cycle 2: flash, arm, /done, sanboot.
    app_client.get(f"/pxe/{mac}", headers={"Host": "bty.local:8080"})  # flash (bit=0)
    app_client.get(f"/boot/{ARTIFACT_NAMES[0]}?mac={mac}", headers={"Host": "bty.local:8080"})
    app_client.post(f"/pxe/{mac}/done")
    app_client.get(f"/pxe/{mac}", headers={"Host": "bty.local:8080"})  # sanboot + clear

    r = app_client.get(
        "/events",
        params={"subject_kind": "machine", "subject_id": mac, "kind": "netboot.pxe.offered"},
        cookies=AUTH,
    )
    events = r.json()["events"]
    offers = [e["details"]["offer_kind"] for e in events]
    # 4 /pxe hits -> 4 offers. Newest first: sanboot, flash, sanboot, flash.
    assert len(offers) >= 4, offers
    assert offers[0] == "bty-flash-always-sanboot"
    assert offers[1] == "bty-flash-always"
    assert offers[2] == "bty-flash-always-sanboot"
    assert offers[3] == "bty-flash-always"


def test_pxe_flash_mode_with_no_ref_falls_back_to_unknown(
    app_client: TestClient,
) -> None:
    """A machine with ``boot_mode=bty-flash-always`` but NO
    ``bty_image_ref`` bound hits the ``ipxe_unknown.j2`` fallback
    template (sanboots first disk, falls back to firmware exit).
    Pre-this-test the branch was uncovered -- the JSON PUT
    /machines/{mac} accepts this shape (the policy-without-image
    state), so the PXE handler's behavior under that state needs
    to be pinned.

    The offer_kind in the audit event lands as ``"unknown"``."""
    mac = "aa:bb:cc:dd:ee:55"
    # Stage the machine with a flash policy but no image binding.
    r = app_client.put(
        f"/machines/{mac}",
        json={"boot_mode": "bty-flash-always", "target_disk_serial": "SERIAL-X"},
        cookies=AUTH,
    )
    assert r.status_code == 200, r.text

    r = app_client.get(f"/pxe/{mac}")
    assert r.status_code == 200
    # The rendered chain doesn't matter much for the contract; the
    # event log is the authoritative record of what was offered.
    r = app_client.get(
        "/events",
        params={"subject_kind": "machine", "subject_id": mac, "kind": "netboot.pxe.offered"},
        cookies=AUTH,
    )
    events = r.json()["events"]
    assert len(events) >= 1
    assert events[0]["details"]["offer_kind"] == "unknown", (
        f"flash-mode-without-ref must offer 'unknown', got "
        f"{events[0]['details'].get('offer_kind')!r}"
    )


def test_create_app_rotates_stale_state_db_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """INTEGRATION (v0.33.0 contract): a state.db whose bty_version
    disagrees with the running release must be rotated to a .bak by
    the time ``create_app`` finishes building the app. The init_db-
    level rotation tests in test_web_db.py cover the SQL primitive,
    but only an end-to-end app-build catches a refactor that puts
    the rotation behind a flag, skips it on app startup, or moves
    init_db to a different entry point.

    Pre-condition: state.db exists, stamped with an old version,
    contains an operator-typed machine row to prove it was non-empty.
    Post-condition: a ``state.db.<oldver>.<ts>.bak`` file exists; the
    fresh state.db carries the running version and one
    ``system.schema.reset`` event in the events table.
    """
    import sqlite3 as _sqlite

    import bty
    from bty.web import _db

    state = tmp_path / "state.db"
    bty_state_dir = tmp_path / "bty-state"
    bty_state_dir.mkdir()
    monkeypatch.setenv("BTY_STATE_DIR", str(bty_state_dir))

    # Stamp state.db with a non-current version + a sentinel row so
    # we can prove the rotation preserved the OLD DB while leaving
    # the fresh DB empty of machines.
    _db.init_db(state)
    with _sqlite.connect(state) as conn:
        conn.execute("UPDATE bty_version SET version = ?", ("0.0.1-fake-old",))
        conn.execute(
            "INSERT INTO machines (mac, boot_mode, created_at, updated_at) "
            "VALUES (?, 'bty-inventory', ?, ?)",
            ("aa:bb:cc:dd:ee:ff", "2026-05-26T00:00:00+00:00", "2026-05-26T00:00:00+00:00"),
        )
        conn.commit()

    # Build the app -- this triggers the rotation as part of
    # init_db (called via open_db inside create_app's lifespan).
    app = create_app(
        state_path=state,
        service_user=TEST_SERVICE_USER,
        secret_key=TEST_SECRET_KEY,
    )
    with TestClient(app) as client:
        # /healthz forces the lifespan to fully start (open_db
        # against the fresh state.db).
        r = client.get("/healthz")
        assert r.status_code == 200

    # The rotated .bak exists and is named for the old version.
    baks = list(tmp_path.glob("state.db.0.0.1-fake-old.*.bak"))
    assert len(baks) == 1, f"expected one .bak, found {[b.name for b in baks]!r}"

    # The fresh state.db carries the running version + one
    # schema_reset event + no machines (rotated out with the old DB).
    with _sqlite.connect(state) as conn:
        version_row = conn.execute("SELECT version FROM bty_version").fetchone()
        machines_count = conn.execute("SELECT COUNT(*) FROM machines").fetchone()[0]
        reset_events = conn.execute(
            "SELECT details FROM events WHERE kind = 'system.schema.reset'"
        ).fetchall()
    assert version_row[0] == bty.__version__
    assert machines_count == 0, "rotated state.db must not carry pre-rotation rows"
    assert len(reset_events) == 1, "exactly one system.schema.reset event must surface"

    # The .bak still has the operator's pre-rotation machine row, so
    # they can sqlite3-spelunk if they need to recover something.
    with _sqlite.connect(baks[0]) as conn:
        old_machines = [r[0] for r in conn.execute("SELECT mac FROM machines")]
    assert old_machines == ["aa:bb:cc:dd:ee:ff"]


# ---------- operator-curated catalog entries -------------------------------


def test_catalog_entries_add_with_sha_url_resolves_sha(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``POST /catalog/entries`` with both image_url + sha_url:
    server fetches sha_url, parses, picks the digest matching the
    image-URL filename, stores the entry."""
    sha = "a" * 64
    manifest_body = f"{sha}  ubuntu-22.04.img.gz\n{'b' * 64}  other.img.gz\n"

    def fake_urlopen(req, *_a, **_kw):  # type: ignore[no-untyped-def]
        url = req if isinstance(req, str) else req.full_url
        if url.endswith(".sha256"):
            return _MockResp(manifest_body.encode())
        # HEAD on the image URL: return Content-Length.
        return _MockResp(b"", headers={"Content-Length": "12345"})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    r = app_client.post(
        "/catalog/entries",
        json={
            "image_url": "https://example.invalid/ubuntu-22.04.img.gz",
            "sha_url": "https://example.invalid/SHA256SUMS.sha256",
        },
        cookies=AUTH,
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["disk_image_sha"] == sha
    assert body["src"] == "https://example.invalid/ubuntu-22.04.img.gz"
    assert body["name"] == "ubuntu-22.04.img.gz"
    assert body["format"] == "img.gz"
    assert body["size_bytes"] == 12345
    # Every add returns a bty_image_ref (sha256 of canonicalised src).
    assert len(body["bty_image_ref"]) == 64


def test_catalog_entries_add_without_sha_url_is_url_only(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """sha_url is optional; without it the entry stores
    sha256=NULL. Surfaces in /images as a URL-only row."""

    def fake_urlopen(*_a, **_kw):  # type: ignore[no-untyped-def]
        return _MockResp(b"", headers={"Content-Length": "999"})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    r = app_client.post(
        "/catalog/entries",
        json={"image_url": "https://example.invalid/foo.img.gz"},
        cookies=AUTH,
    )
    assert r.status_code == 201, r.text
    assert r.json()["disk_image_sha"] is None
    assert len(r.json()["bty_image_ref"]) == 64

    r2 = app_client.get("/images")
    rows = r2.json()
    by_name = {row["name"]: row for row in rows}
    assert "foo.img.gz" in by_name
    assert by_name["foo.img.gz"]["url"] == "https://example.invalid/foo.img.gz"
    assert by_name["foo.img.gz"]["sha_short"] is None  # sha256 unknown


def test_catalog_entries_add_rejects_non_https(app_client: TestClient) -> None:
    """``image_url`` / ``sha_url`` must be http(s) or oras://; a typo
    with a different scheme should 422 at the Pydantic layer rather
    than land an unflashable entry."""
    r = app_client.post(
        "/catalog/entries",
        json={"image_url": "ftp://example.invalid/foo.img.gz"},
        cookies=AUTH,
    )
    assert r.status_code == 422


def test_catalog_entries_add_with_oras_ref_resolves_manifest(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``POST /catalog/entries`` with an ``oras://`` image_url resolves
    the manifest at add time. The picked layer's content-addressed
    digest becomes the row's sha256 (= machine-bindable); the layer
    title annotation becomes the name; the layer size becomes
    size_bytes. ``sha_url`` is ignored (manifest is authoritative)."""
    import io
    import json as _json

    manifest = {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "layers": [
            {
                "mediaType": "application/vnd.nosi.disk-image.layer.v1+gzip",
                "digest": "sha256:" + "ab" * 32,
                "size": 12345678,
                "annotations": {
                    "org.opencontainers.image.title": "nosi-debian-sysdev-x86_64.img.gz"
                },
            },
        ],
    }

    def fake_urlopen(req, *_a, **_kw):
        url = req if isinstance(req, str) else req.full_url

        class _Resp(io.BytesIO):
            # No-op headers attr; the fetch_to_cache path reads
            # ``Content-Length`` off it, but the manifest / token
            # responses here are short fixed JSON blobs that bypass
            # the streaming branch.
            headers: typing.ClassVar[dict[str, str]] = {}

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

        if "/token" in url:
            return _Resp(_json.dumps({"token": "anon-tok"}).encode())
        if "/manifests/" in url:
            return _Resp(_json.dumps(manifest).encode())
        raise AssertionError(f"unexpected URL: {url}")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    r = app_client.post(
        "/catalog/entries",
        json={
            "image_url": "oras://ghcr.io/safl/nosi/debian-sysdev:latest",
            "sha_url": None,
        },
        cookies=AUTH,
    )
    assert r.status_code == 201, r.text
    payload = r.json()
    assert payload["src"] == "oras://ghcr.io/safl/nosi/debian-sysdev:latest"
    assert payload["disk_image_sha"] == "ab" * 32  # stripped algorithm prefix
    assert payload["name"] == "nosi-debian-sysdev-x86_64.img.gz"
    assert payload["format"] == "img.gz"
    assert payload["size_bytes"] == 12345678
    assert payload["sha_url"] is None
    assert len(payload["bty_image_ref"]) == 64


def test_catalog_entries_add_with_oras_ref_propagates_resolve_failure(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Token / manifest fetch failure for an oras ref must 400 rather
    than landing a half-populated row. The event log records the
    failure with the operator's source IP."""

    def fake_urlopen(*_a, **_kw):
        raise OSError("connection refused")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    r = app_client.post(
        "/catalog/entries",
        json={"image_url": "oras://ghcr.io/safl/nosi/no-such-pkg:latest"},
        cookies=AUTH,
    )
    assert r.status_code == 400
    assert "oras" in r.json()["detail"].lower()


def test_catalog_entries_add_duplicate_src_409(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same image_url posted twice: 409. Operator must DELETE
    first to replace."""

    def fake_urlopen(*_a, **_kw):  # type: ignore[no-untyped-def]
        return _MockResp(b"", headers={"Content-Length": "111"})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    body = {"image_url": "https://example.invalid/dup.img.gz"}
    r1 = app_client.post("/catalog/entries", json=body, cookies=AUTH)
    assert r1.status_code == 201
    r2 = app_client.post("/catalog/entries", json=body, cookies=AUTH)
    assert r2.status_code == 409


def test_catalog_entries_list_and_delete(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_urlopen(*_a, **_kw):  # type: ignore[no-untyped-def]
        return _MockResp(b"", headers={"Content-Length": "0"})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    url = "https://example.invalid/del.img.gz"
    app_client.post("/catalog/entries", json={"image_url": url}, cookies=AUTH)

    # Auto-import sweeps dir-scan files into catalog_entries on
    # bty-web startup; the app_client fixture seeds ``demo.qcow2``
    # so we filter by src to isolate the URL-added entry under test.
    r = app_client.get("/catalog/entries", cookies=AUTH)
    assert r.status_code == 200
    by_src = {row["src"]: row for row in r.json()}
    assert url in by_src
    assert by_src[url]["src"] == url

    r = app_client.delete("/catalog/entries", params={"src": url}, cookies=AUTH)
    assert r.status_code == 204

    r = app_client.get("/catalog/entries", cookies=AUTH)
    remaining = {row["src"] for row in r.json()}
    assert url not in remaining


def test_catalog_import_from_local_path(app_client: TestClient, tmp_path: Path) -> None:
    """``POST /catalog/import?source=<path>`` parses the TOML and
    inserts entries into ``catalog_entries``. No bytes fetched."""
    manifest = tmp_path / "catalog.toml"
    manifest.write_text(
        """
        version = 1

        [[images]]
        name = "alpha.img.gz"
        format = "img.gz"
        size_bytes = 1024
        src = "https://example.invalid/alpha.img.gz"

        [[images]]
        name = "beta.img.gz"
        format = "img.gz"
        size_bytes = 2048
        src = "https://example.invalid/beta.img.gz"
        """,
        encoding="utf-8",
    )
    r = app_client.post(
        "/catalog/import",
        params={"source": str(manifest)},
        cookies=AUTH,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["imported"] == 2
    assert body["skipped"] == 0
    assert body["errors"] == []
    # The fixture seeds ``demo.qcow2`` which the auto-import sweep
    # imports as ``file://demo.qcow2``; filter to the entries this
    # test added.
    r2 = app_client.get("/catalog/entries", cookies=AUTH)
    names = {row["name"] for row in r2.json()}
    assert "alpha.img.gz" in names
    assert "beta.img.gz" in names
    # No bytes fetched: /images shows cached=False
    r3 = app_client.get("/images")
    images_rows = {row["name"]: row for row in r3.json()}
    assert "alpha.img.gz" in images_rows
    assert images_rows["alpha.img.gz"]["cached"] is False


def test_catalog_import_idempotent_skips_duplicates(app_client: TestClient, tmp_path: Path) -> None:
    """Re-importing the same manifest counts duplicates as ``skipped``,
    leaves the table unchanged."""
    manifest = tmp_path / "catalog.toml"
    manifest.write_text(
        """
        version = 1
        [[images]]
        name = "gamma.img.gz"
        format = "img.gz"
        src = "https://example.invalid/gamma.img.gz"
        """,
        encoding="utf-8",
    )
    r1 = app_client.post("/catalog/import", params={"source": str(manifest)}, cookies=AUTH)
    assert r1.json()["imported"] == 1
    r2 = app_client.post("/catalog/import", params={"source": str(manifest)}, cookies=AUTH)
    assert r2.json()["imported"] == 0
    assert r2.json()["skipped"] == 1
    # Filter to the gamma row; the auto-import sweep adds file://
    # entries for fixture-seeded files (e.g. demo.qcow2).
    names = {row["name"] for row in app_client.get("/catalog/entries", cookies=AUTH).json()}
    assert "gamma.img.gz" in names


def test_catalog_import_rejects_invalid_source_scheme(
    app_client: TestClient,
) -> None:
    """Unsupported schemes (ftp://, etc.) return 400."""
    r = app_client.post(
        "/catalog/import",
        params={"source": "ftp://example.invalid/catalog.toml"},
        cookies=AUTH,
    )
    assert r.status_code == 400
    assert "ftp" in r.text or "scheme" in r.text


def test_catalog_import_requires_auth(app_client: TestClient) -> None:
    r = app_client.post(
        "/catalog/import",
        params={"source": "https://example.invalid/catalog.toml"},
    )
    assert r.status_code == 401


def test_catalog_import_with_oras_entry_resolves_sha(
    app_client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Manifest entries with ``oras://`` src and no pre-pinned
    sha256 get resolved at import time so the imported row carries
    a machine-bindable digest. The resolve uses the same code path
    as ``POST /catalog/entries`` for oras URLs."""
    import io
    import json as _json

    manifest_text = """
    version = 1
    [[images]]
    name = "nosi-debian-sysdev.img.gz"
    format = "img.gz"
    src = "oras://ghcr.io/safl/nosi/debian-sysdev:latest"
    """
    manifest_file = tmp_path / "catalog.toml"
    manifest_file.write_text(manifest_text, encoding="utf-8")

    oras_manifest = {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "layers": [
            {
                "mediaType": "application/vnd.nosi.disk-image.layer.v1+gzip",
                "digest": "sha256:" + "cd" * 32,
                "size": 7654321,
                "annotations": {"org.opencontainers.image.title": "nosi-debian-sysdev.img.gz"},
            },
        ],
    }

    def fake_urlopen(req, *_a, **_kw):  # type: ignore[no-untyped-def]
        url = req if isinstance(req, str) else req.full_url

        class _Resp(io.BytesIO):
            headers: typing.ClassVar[dict[str, str]] = {}

            def __enter__(self):  # type: ignore[no-untyped-def]
                return self

            def __exit__(self, *_args):  # type: ignore[no-untyped-def]
                return None

        if "/token" in url:
            return _Resp(_json.dumps({"token": "anon-tok"}).encode())
        if "/manifests/" in url:
            return _Resp(_json.dumps(oras_manifest).encode())
        raise AssertionError(f"unexpected URL: {url}")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    r = app_client.post(
        "/catalog/import",
        params={"source": str(manifest_file)},
        cookies=AUTH,
    )
    assert r.status_code == 200, r.text
    assert r.json()["imported"] == 1
    # Filter to the oras-imported row; ignore the auto-imported
    # file:// row(s) the fixture's image_root seeds.
    rows = [
        row
        for row in app_client.get("/catalog/entries", cookies=AUTH).json()
        if row["src"].startswith("oras://")
    ]
    assert len(rows) == 1
    assert rows[0]["disk_image_sha"] == "cd" * 32
    assert len(rows[0]["bty_image_ref"]) == 64
    assert rows[0]["size_bytes"] == 7654321


def test_ui_images_renders_catalog_entries_in_added_at_order(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The /ui/images page reads ``catalog_entries`` via
    ``_load_db_catalog_split``; that query must use the same
    ``ORDER BY added_at`` as the public ``GET /catalog/entries``
    listing so a page refresh doesn't reorder rows. SQLite's
    default row order is unspecified -- without ``ORDER BY``,
    a refresh can shuffle the URL-only entries even though
    nothing changed."""

    def fake_urlopen(*_a: object, **_kw: object) -> _MockResp:
        return _MockResp(b"", headers={"Content-Length": "0"})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    # Insert three URL-only entries with distinct names. The
    # ``added_at`` column is set server-side via ``_now_iso()``,
    # so insertion order = added_at order, and an ORDER BY
    # ensures the displayed order matches.
    for url in (
        "https://example.invalid/alpha.img.gz",
        "https://example.invalid/bravo.img.gz",
        "https://example.invalid/charlie.img.gz",
    ):
        r = app_client.post("/catalog/entries", json={"image_url": url}, cookies=AUTH)
        assert r.status_code == 201

    r = app_client.get("/ui/images", cookies=AUTH)
    assert r.status_code == 200
    body = r.text
    # The three names appear in the rendered table in insertion
    # order. ``find()`` returns the byte offset; each later name
    # must appear at a higher offset than the prior.
    pos_alpha = body.find("alpha.img.gz")
    pos_bravo = body.find("bravo.img.gz")
    pos_charlie = body.find("charlie.img.gz")
    assert 0 < pos_alpha < pos_bravo < pos_charlie, (
        f"catalog rows out of order: alpha={pos_alpha} bravo={pos_bravo} charlie={pos_charlie}"
    )


class _MockResp:
    """Tiny urllib.request.urlopen response stand-in for tests."""

    def __init__(self, body: bytes, headers: dict[str, str] | None = None) -> None:
        self._body = body
        self.headers = headers or {}

    def __enter__(self) -> _MockResp:
        return self

    def __exit__(self, *_a: object) -> None:
        pass

    def read(self, *_a: object) -> bytes:
        return self._body

    def decode(self, *_a: object) -> str:
        return self._body.decode()


# ---------- release-fetch manager ------------------------------------------


def test_release_fetch_enqueue_returns_state(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``POST /boot/releases`` enqueues a fetch and returns the
    initial state. The actual fetch is mocked out so the test
    runs offline + fast."""

    # Patch the worker so it just records + returns success.
    def fake_fetch(boot_dir, *_a, **_kw):  # type: ignore[no-untyped-def]
        from bty.web._releases import FetchResult

        return FetchResult(base_url="https://test.invalid/x", artifacts=("a",), total_bytes=42)

    monkeypatch.setattr("bty.web._releases.fetch_release", fake_fetch)
    r = app_client.post(
        "/boot/releases",
        json={"tag": "latest"},
        cookies=AUTH,
    )
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["tag"] == "latest"
    assert body["status"] in ("queued", "running", "completed")


def test_release_fetch_list_returns_states(app_client: TestClient) -> None:
    r = app_client.get("/boot/releases", cookies=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert "fetches" in body
    assert "boot_root" in body
    assert "max_parallel" in body


def test_release_fetch_cancel_unknown_tag_404(app_client: TestClient) -> None:
    r = app_client.delete("/boot/releases/never-was", cookies=AUTH)
    assert r.status_code == 404


def test_release_fetch_invalid_tag_422(app_client: TestClient) -> None:
    """Tag must match the URL-segment-friendly pattern."""
    r = app_client.post(
        "/boot/releases",
        json={"tag": "with/slashes"},
        cookies=AUTH,
    )
    assert r.status_code == 422


def test_release_fetch_unknown_extra_field_422(app_client: TestClient) -> None:
    r = app_client.post(
        "/boot/releases",
        json={"tag": "latest", "stale_field": "x"},
        cookies=AUTH,
    )
    assert r.status_code == 422


def test_release_fetch_manager_backfills_from_events(tmp_path: Path) -> None:
    """The manager's in-memory ``_states`` dies on restart, which
    made the /ui/netboot "Active + recent fetches" table show "No
    fetches yet." even when artifacts were clearly present on
    disk. The fix backfills from netboot.artifacts.fetched /
    netboot.artifacts.fetch.failed events on ``start()``.

    Seeds two events on a fresh state.db, then starts a manager
    against it and asserts that the manager's state mirror the
    events (newest-per-tag wins for terminal outcome)."""
    import asyncio

    from bty.web import _db, _events_log, _release_mgr

    state_path = tmp_path / "state.db"
    boot_root = tmp_path / "boot"
    boot_root.mkdir()

    with _db.open_db(state_path) as conn:
        _events_log.record(
            conn,
            kind="netboot.artifacts.fetched",
            summary="release latest fetched",
            subject_kind="netboot",
            subject_id="latest",
            actor="operator",
            source_ip="127.0.0.1",
            details={
                "tag": "latest",
                "base_url": "https://example.invalid/latest",
                "total_bytes": 12345,
                "artifacts": ["a.efi"],
            },
        )
        _events_log.record(
            conn,
            kind="netboot.artifacts.fetch.failed",
            summary="release v0.1.2 failed",
            subject_kind="netboot",
            subject_id="v0.1.2",
            actor="operator",
            source_ip="127.0.0.1",
            details={"tag": "v0.1.2", "error": "404 Not Found"},
        )
        conn.commit()

    async def go() -> None:
        mgr = _release_mgr.ReleaseFetchManager()
        mgr.start(boot_root, state_path=state_path)
        try:
            states = await mgr.list()
            by_tag = {s.tag: s for s in states}
            assert "latest" in by_tag
            assert by_tag["latest"].status == "completed"
            assert by_tag["latest"].bytes_done == 12345
            assert by_tag["latest"].base_url == "https://example.invalid/latest"
            assert "v0.1.2" in by_tag
            assert by_tag["v0.1.2"].status == "failed"
            assert by_tag["v0.1.2"].error == "404 Not Found"
        finally:
            await mgr.stop()

    asyncio.run(go())


def test_release_fetch_manager_backfill_picks_most_recent_per_tag(
    tmp_path: Path,
) -> None:
    """Two events for the same tag (older failure + newer success):
    the newer one wins after backfill. Guards the "newest-first
    iteration with seen-set" invariant."""
    import asyncio

    from bty.web import _db, _events_log, _release_mgr

    state_path = tmp_path / "state.db"
    boot_root = tmp_path / "boot"
    boot_root.mkdir()
    with _db.open_db(state_path) as conn:
        # Older failure
        _events_log.record(
            conn,
            kind="netboot.artifacts.fetch.failed",
            summary="latest failed first attempt",
            subject_kind="netboot",
            subject_id="latest",
            details={"tag": "latest", "error": "old network blip"},
        )
        # Newer success
        _events_log.record(
            conn,
            kind="netboot.artifacts.fetched",
            summary="latest succeeded on retry",
            subject_kind="netboot",
            subject_id="latest",
            details={"tag": "latest", "base_url": "https://example/latest", "total_bytes": 99},
        )
        conn.commit()

    async def go() -> None:
        mgr = _release_mgr.ReleaseFetchManager()
        mgr.start(boot_root, state_path=state_path)
        try:
            states = await mgr.list()
            assert len(states) == 1
            assert states[0].status == "completed"
            assert states[0].bytes_done == 99
        finally:
            await mgr.stop()

    asyncio.run(go())


def test_release_fetch_manager_run_fetch_cancel_overrides_fetch_error(
    tmp_path: Path,
) -> None:
    """If urllib raises ``URLError`` (wrapped as ``FetchError``)
    while the cancel flag is already set, the manager must record
    the result as ``cancelled``, not ``failed`` -- the operator's
    intent was "stop", and a "failed: connection reset" badge for
    a deliberate cancel is misleading."""
    import asyncio
    import unittest.mock

    from bty.web import _release_mgr, _releases

    async def go() -> None:
        boot_root = tmp_path / "boot"
        mgr = _release_mgr.ReleaseFetchManager()
        mgr.start(boot_root)
        try:
            state = _release_mgr.ReleaseFetchState(tag="v1.0")
            state._cancel.set()

            def boom(*_a: object, **_kw: object) -> None:
                raise _releases.FetchError("connection reset")

            with unittest.mock.patch.object(_releases, "fetch_release", boom):
                await mgr._run_one(state)

            assert state.status == "cancelled"
            assert state.error is None
        finally:
            await mgr.stop()

    asyncio.run(go())


def test_release_fetch_manager_failure_logs_event(tmp_path: Path) -> None:
    """A genuinely-failed fetch (urllib error, not operator cancel)
    must land a ``netboot.artifacts.fetch.failed`` event in the audit
    log. Symmetric with the success path's ``netboot.artifacts.fetched``
    so the operator can see "this fetch tried + crashed" via
    /ui/events instead of polling /boot/releases."""
    import asyncio
    import unittest.mock

    from bty.web import _db, _release_mgr, _releases
    from bty.web._events_log import list_events

    state_db = tmp_path / "state.db"
    _db.init_db(state_db)
    boot_root = tmp_path / "boot"

    async def go() -> None:
        mgr = _release_mgr.ReleaseFetchManager()
        mgr.start(boot_root, state_path=state_db)
        try:
            state = _release_mgr.ReleaseFetchState(tag="v9.9.9")

            def boom(*_a: object, **_kw: object) -> None:
                raise _releases.FetchError("upstream 500")

            with unittest.mock.patch.object(_releases, "fetch_release", boom):
                await mgr._run_one(state)
            assert state.status == "failed"
        finally:
            await mgr.stop()

    asyncio.run(go())

    with _db.open_db(state_db) as conn:
        rows = list_events(conn, kind="netboot.artifacts.fetch.failed")
    assert len(rows) == 1
    row = rows[0]
    assert row.subject_kind == "netboot"
    assert row.subject_id == "v9.9.9"
    assert row.actor == "system"
    assert row.details is not None
    assert "upstream 500" in row.details["error"]


def test_release_fetch_manager_enqueue_rejects_malformed_tag() -> None:
    """``ReleaseFetchManager.enqueue`` validates the tag shape
    even for non-API callers (tests, future internal use). The
    HTTP layer's Pydantic model already covers the public path,
    but the manager must guard its own boundary so a slash-
    bearing tag can never reach the GitHub URL builder."""
    import asyncio

    from bty.web import _release_mgr

    mgr = _release_mgr.ReleaseFetchManager()

    async def go() -> None:
        # ``start`` would normally be called by the FastAPI lifespan;
        # the validator runs before the boot-root check so a
        # well-formed tag check requires no event-loop wiring.
        with pytest.raises(ValueError, match=r"invalid release tag"):
            await mgr.enqueue("../etc/passwd")
        with pytest.raises(ValueError, match=r"invalid release tag"):
            await mgr.enqueue("with/slash")
        with pytest.raises(ValueError, match=r"invalid release tag"):
            await mgr.enqueue("")

    asyncio.run(go())


def test_catalog_entries_add_rejects_url_without_host(
    app_client: TestClient,
) -> None:
    """``image_url`` regex requires a host segment. ``https://?``
    (empty host) and ``http:///path`` (host-less) must 422 at the
    Pydantic layer rather than landing as unflashable rows."""
    for bad in ("https://?", "http:///path", "https://"):
        r = app_client.post(
            "/catalog/entries",
            json={"image_url": bad},
            cookies=AUTH,
        )
        assert r.status_code == 422, f"expected 422 for {bad!r}, got {r.status_code}"


def test_catalog_entries_add_rejects_url_without_filename(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``image_url`` must end in a filename component. URLs that
    have a host but no path (``https://example.com``) or a
    trailing-slash path (``https://example.com/foo/``) used to
    fall through and store the entire URL as the entry's
    ``name`` -- the catalog table then rendered ``<code>https://
    example.com</code>`` as the display label, which was useless.
    Reject at validation time instead."""

    def fake_urlopen(*_a: object, **_kw: object) -> _MockResp:
        return _MockResp(b"", headers={"Content-Length": "0"})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    # ``Path("/foo/").name`` is ``"foo"`` (pathlib normalises the
    # trailing slash), so URLs like ``https://example.com/foo/``
    # have a basename and remain accepted. The reject-list here
    # is the genuinely-no-basename forms: bare host, bare host +
    # ``/``.
    for bad in ("https://example.com", "https://example.com/"):
        r = app_client.post(
            "/catalog/entries",
            json={"image_url": bad},
            cookies=AUTH,
        )
        assert r.status_code == 422, f"expected 422 for {bad!r}, got {r.status_code}"
        assert "filename component" in r.text


def test_ui_catalog_upload_requires_auth(app_client: TestClient) -> None:
    r = app_client.post(
        "/ui/catalog/upload",
        files={"file": ("catalog.toml", b"version = 1\n", "application/toml")},
        follow_redirects=False,
    )
    assert r.status_code == 401


def test_ui_catalog_upload_auto_imports_manifest_into_catalog_entries(
    app_client: TestClient,
) -> None:
    """A manifest uploaded via /ui/catalog/upload must auto-import
    its entries into ``catalog_entries`` so the /ui/machines/{mac}
    dropdown becomes populated without a separate /catalog/import
    round-trip. Pre-fix the entries showed up on /ui/images (the
    merge surfaced them) but were invisible in the machine binding
    picker."""
    body = (
        b"version = 1\n\n"
        b"[[images]]\n"
        b'name = "ubuntu-from-manifest"\n'
        b'src = "https://example.invalid/ubuntu-from-manifest.img.gz"\n'
        b'format = "img.gz"\n'
    )
    r = app_client.post(
        "/ui/catalog/upload",
        files={"file": ("catalog.toml", body, "application/toml")},
        cookies=AUTH,
        follow_redirects=False,
    )
    assert r.status_code == 303
    # /catalog/entries returns the catalog_entries DB rows. The
    # manifest entry should now be in there with the right name +
    # src + bty_image_ref.
    entries = app_client.get("/catalog/entries", cookies=AUTH).json()
    found = [e for e in entries if e["name"] == "ubuntu-from-manifest"]
    assert len(found) == 1
    assert found[0]["src"] == "https://example.invalid/ubuntu-from-manifest.img.gz"
    # bty_image_ref is a 64-hex sha derived from canonicalised src.
    assert len(found[0]["bty_image_ref"]) == 64


def test_ui_catalog_upload_imports_into_db_and_303s_on_success(
    app_client: TestClient,
) -> None:
    """Upload a valid catalog -> entries are imported into the
    ``catalog_entries`` DB, the bytes are written to ``manifest_path``,
    303 back to /ui/images without an error param."""
    body = b'version = 1\n\n[[images]]\nname = "demo"\nsrc = "https://example.com/demo.img.zst"\n'
    r = app_client.post(
        "/ui/catalog/upload",
        files={"file": ("catalog.toml", body, "application/toml")},
        cookies=AUTH,
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text
    assert r.headers["location"] == "/ui/images"
    # Entry now visible via the catalog list endpoint (which reads
    # from catalog_entries) -- evidence the import landed in the DB.
    listing = app_client.get("/catalog/entries", cookies=AUTH).json()
    assert any(e["src"] == "https://example.com/demo.img.zst" for e in listing)


def test_ui_catalog_upload_rejects_bad_manifest_keeps_existing(
    app_client: TestClient,
    tmp_path: Path,
) -> None:
    """A parse failure 303s back with ?error=... and does NOT
    clobber the on-disk manifest -- so a stray bad upload can't
    nuke a working catalog."""
    state_dir = tmp_path / "bty-state"
    good = b'version = 1\n\n[[images]]\nname = "demo"\nsrc = "https://example.com/demo.img.zst"\n'
    manifest = state_dir / "catalog.toml"
    manifest.write_bytes(good)
    r = app_client.post(
        "/ui/catalog/upload",
        files={"file": ("catalog.toml", b"this is not valid toml [[", "application/toml")},
        cookies=AUTH,
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"].startswith("/ui/images?error=")
    assert manifest.read_bytes() == good  # unchanged


def test_ui_catalog_fetch_release_requires_auth(app_client: TestClient) -> None:
    r = app_client.post("/ui/catalog/fetch-release", follow_redirects=False)
    assert r.status_code == 401


class _FakeUrlopenResp:
    """Stub for ``urllib.request.urlopen`` context-manager value.

    Used by every ``/ui/catalog/fetch-release`` test to swap out
    the real HTTP fetch. Returns the canned bytes via ``read()``
    -- which the production handler now calls with a size cap
    argument, so the stub honours it instead of dumping the
    whole body and forcing the caller to remember the cap.
    """

    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            return self._data
        return self._data[:size]

    def __enter__(self) -> _FakeUrlopenResp:
        return self

    def __exit__(self, *_a: object) -> None:
        return None


def test_ui_catalog_fetch_release_imports_into_db_and_303s(
    app_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fetch-release stubs urlopen, imports the bytes' entries into
    ``catalog_entries``, writes the bytes to ``manifest_path``, then
    303s back to /ui/images."""
    body = b'version = 1\n\n[[images]]\nname = "rel"\nsrc = "https://example.com/rel.img.zst"\n'

    import urllib.request as _urlreq

    monkeypatch.setattr(_urlreq, "urlopen", lambda *_a, **_kw: _FakeUrlopenResp(body))
    r = app_client.post(
        "/ui/catalog/fetch-release",
        cookies=AUTH,
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text
    assert r.headers["location"] == "/ui/images"
    listing = app_client.get("/catalog/entries", cookies=AUTH).json()
    assert any(e["src"] == "https://example.com/rel.img.zst" for e in listing)


# ---------- /ui/catalog/upload and /ui/catalog/fetch-release error matrix --


def test_ui_catalog_upload_no_file_field_303s_with_error(app_client: TestClient) -> None:
    """A multipart POST with no ``file`` field bounces back with a
    flash error instead of 500-ing or silently writing nothing."""
    # ``files`` empty + ``data`` populated forces httpx to send a
    # multipart body that has no file part.
    r = app_client.post(
        "/ui/catalog/upload",
        data={"unrelated": "x"},
        files={"otherfield": ("x.toml", b"version = 1\n", "application/toml")},
        cookies=AUTH,
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"].startswith("/ui/images?error=")
    assert "no%20file%20in%20upload" in r.headers["location"]


def test_ui_catalog_upload_empty_file_303s_with_error(app_client: TestClient) -> None:
    """A zero-byte upload is rejected with a flash error."""
    r = app_client.post(
        "/ui/catalog/upload",
        files={"file": ("catalog.toml", b"", "application/toml")},
        cookies=AUTH,
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"].startswith("/ui/images?error=")
    assert "empty" in r.headers["location"]


def test_ui_catalog_upload_oversized_303s_with_error(
    app_client: TestClient,
    tmp_path: Path,
) -> None:
    """A multi-MB upload (operator dropped a wrong file like an
    .iso into the catalog form) is rejected before we try parsing
    it. The on-disk manifest is unaffected even if one existed."""
    state_dir = tmp_path / "bty-state"
    existing = state_dir / "catalog.toml"
    good = b'version = 1\n\n[[images]]\nname = "demo"\nsrc = "https://example.com/demo.img.zst"\n'
    existing.write_bytes(good)
    # 2 MiB > 1 MiB cap. Use a memoryview so the test stays cheap.
    huge = b"# fake huge body\n" + b"x" * (2 * 1024 * 1024)
    r = app_client.post(
        "/ui/catalog/upload",
        files={"file": ("catalog.toml", huge, "application/toml")},
        cookies=AUTH,
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"].startswith("/ui/images?error=")
    assert "exceeded" in r.headers["location"]
    # Existing manifest is untouched (size check fires before
    # the on-disk rename step).
    assert existing.read_bytes() == good


def test_ui_catalog_upload_wrong_extension_303s_with_error(
    app_client: TestClient,
) -> None:
    """An operator-friendly hint: a .yaml / .json upload to the
    catalog manifest form gets a clear "expected .toml" message
    instead of a generic "TOML parse failed" buried inside the
    parse error path."""
    r = app_client.post(
        "/ui/catalog/upload",
        files={"file": ("manifest.yaml", b"version: 1\nimages: []\n", "text/yaml")},
        cookies=AUTH,
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"].startswith("/ui/images?error=")
    assert "unexpected%20file%20extension" in r.headers["location"]


def test_ui_catalog_upload_binary_content_303s_with_parse_error(
    app_client: TestClient,
    tmp_path: Path,
) -> None:
    """A .toml-named upload that is actually binary content
    bounces on the TOML parser. The on-disk manifest is
    preserved (write happens AFTER parse)."""
    state_dir = tmp_path / "bty-state"
    existing = state_dir / "catalog.toml"
    good = b'version = 1\n\n[[images]]\nname = "demo"\nsrc = "https://example.com/demo.img.zst"\n'
    existing.write_bytes(good)
    # 100 bytes of binary content -- below the size cap so it
    # makes it to the parse step.
    r = app_client.post(
        "/ui/catalog/upload",
        files={"file": ("manifest.toml", b"\x89PNG\r\n\x1a\n" + b"\x00" * 96, "application/toml")},
        cookies=AUTH,
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"].startswith("/ui/images?error=")
    assert "parse%20failed" in r.headers["location"]
    assert existing.read_bytes() == good


def test_ui_catalog_upload_wrong_schema_version_303s_with_error(
    app_client: TestClient,
) -> None:
    """A syntactically-valid TOML with the wrong schema version
    (or missing version) gets rejected at parse, surfacing the
    schema mismatch in the flash message."""
    r = app_client.post(
        "/ui/catalog/upload",
        files={
            "file": (
                "catalog.toml",
                b'version = 99\n\n[[images]]\nname = "demo"\nsrc = "https://example.com/x.img"\n',
                "application/toml",
            ),
        },
        cookies=AUTH,
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"].startswith("/ui/images?error=")
    assert "parse%20failed" in r.headers["location"]


def test_ui_catalog_fetch_release_url_error_303s_with_error(
    app_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A urlopen URLError (no network, DNS failure, etc.) lands on
    the flash slot, not a 500."""
    import urllib.error as _urlerr
    import urllib.request as _urlreq

    def _boom(*_a: object, **_kw: object) -> None:
        raise _urlerr.URLError("no route to host")

    monkeypatch.setattr(_urlreq, "urlopen", _boom)
    r = app_client.post(
        "/ui/catalog/fetch-release",
        cookies=AUTH,
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"].startswith("/ui/images?error=")
    assert "fetch%20failed" in r.headers["location"]


def test_ui_catalog_fetch_release_http_404_303s_with_error(
    app_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 404 from GitHub releases (e.g. the release tag has no
    catalog.toml asset uploaded) raises HTTPError (a URLError
    subclass) and lands on the flash slot."""
    import urllib.error as _urlerr
    import urllib.request as _urlreq

    def _http404(*_a: object, **_kw: object) -> None:
        raise _urlerr.HTTPError(
            url="https://example.invalid",
            code=404,
            msg="Not Found",
            hdrs=None,  # type: ignore[arg-type]
            fp=None,
        )

    monkeypatch.setattr(_urlreq, "urlopen", _http404)
    r = app_client.post(
        "/ui/catalog/fetch-release",
        cookies=AUTH,
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"].startswith("/ui/images?error=")


def test_ui_catalog_fetch_release_timeout_303s_with_error(
    app_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A network timeout lands on the flash slot."""
    import urllib.request as _urlreq

    def _timeout(*_a: object, **_kw: object) -> None:
        raise TimeoutError("read timed out")

    monkeypatch.setattr(_urlreq, "urlopen", _timeout)
    r = app_client.post(
        "/ui/catalog/fetch-release",
        cookies=AUTH,
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"].startswith("/ui/images?error=")


def test_ui_catalog_fetch_release_non_toml_body_303s_with_error(
    app_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If GitHub serves an HTML page (e.g. a 200 to a redirect
    landing on a marketing page) instead of TOML, parse fails
    and we surface that on the flash slot rather than persisting
    HTML to disk as the manifest."""
    import urllib.request as _urlreq

    html_404 = b"<!DOCTYPE html><html><body><h1>Not Found</h1></body></html>\n"
    monkeypatch.setattr(_urlreq, "urlopen", lambda *_a, **_kw: _FakeUrlopenResp(html_404))
    r = app_client.post(
        "/ui/catalog/fetch-release",
        cookies=AUTH,
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"].startswith("/ui/images?error=")
    assert "parse%20failed" in r.headers["location"]


def test_ui_catalog_fetch_release_empty_body_303s_with_error(
    app_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty (zero-byte) response from the release URL is
    rejected with a flash message rather than persisting an
    empty manifest."""
    import urllib.request as _urlreq

    monkeypatch.setattr(_urlreq, "urlopen", lambda *_a, **_kw: _FakeUrlopenResp(b""))
    r = app_client.post(
        "/ui/catalog/fetch-release",
        cookies=AUTH,
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"].startswith("/ui/images?error=")
    assert "empty" in r.headers["location"]


def test_ui_catalog_fetch_release_oversized_body_303s_with_error(
    app_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A multi-MB body from the release URL gets capped at the
    same 1 MiB the upload form uses."""
    import urllib.request as _urlreq

    huge = b"# fake huge body\n" + b"x" * (2 * 1024 * 1024)
    monkeypatch.setattr(_urlreq, "urlopen", lambda *_a, **_kw: _FakeUrlopenResp(huge))
    r = app_client.post(
        "/ui/catalog/fetch-release",
        cookies=AUTH,
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"].startswith("/ui/images?error=")
    assert "exceeded" in r.headers["location"]


def test_ui_catalog_fetch_release_is_idempotent_on_repeated_imports(
    app_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each entry's ``src`` is UNIQUE in ``catalog_entries``; a
    repeated fetch-release of the same manifest skips already-imported
    rows on the second pass. /ui/images should render each src once,
    regardless of how many times the manifest got re-imported.

    Historical context: an earlier model rendered manifest entries
    AND their auto-imported DB rows separately, double-counting each
    unsha src. The current model imports into DB once + reads only
    from DB, so the dedup invariant is enforced at the SQL layer
    (UNIQUE on src). This test pins the operator-visible result.
    """
    import urllib.request as _urlreq

    body = (
        b"version = 1\n"
        b"\n"
        b'[[images]]\nname = "alpha"\nsrc = "https://example.com/alpha.img.gz"\n'
        b"\n"
        b'[[images]]\nname = "beta"\nsrc = "https://example.com/beta.img.gz"\n'
        b"\n"
        b'[[images]]\nname = "gamma"\nsrc = "https://example.com/releases/latest/download/gamma.img.gz"\n'
    )
    monkeypatch.setattr(_urlreq, "urlopen", lambda *_a, **_kw: _FakeUrlopenResp(body))
    for _ in range(2):  # idempotency: second fetch must not duplicate
        r = app_client.post(
            "/ui/catalog/fetch-release",
            cookies=AUTH,
            follow_redirects=False,
        )
        assert r.status_code == 303, r.text
        assert r.headers["location"] == "/ui/images"

    page = app_client.get("/ui/images", cookies=AUTH)
    assert page.status_code == 200, page.text
    html = page.text
    for src in (
        "https://example.com/alpha.img.gz",
        "https://example.com/beta.img.gz",
        "https://example.com/releases/latest/download/gamma.img.gz",
    ):
        assert html.count(src) >= 1, f"missing src {src!r} on /ui/images"
        assert html.count(src) <= 5, (
            f"src {src!r} rendered {html.count(src)} times on /ui/images; "
            "expected at most 5 per entry (Source cell copy chip: "
            "data-copy + title + visible code = 3; plus Check button "
            "data-src + Delete button data-src = 2). Dedup invariant "
            "(UNIQUE on catalog_entries.src) was violated."
        )


def test_ui_machines_renders_timestamps_compactly(app_client: TestClient, tmp_path: Path) -> None:
    """The ``fmt_ts`` Jinja filter trims ISO 8601 timestamps to
    ``YYYY-MM-DD HH:MM:SS`` for display on /ui/machines and
    related pages. The raw value (with microseconds + ``+00:00``)
    is unreadable for an operator scanning a row -- the title=
    attribute keeps the full precision available on hover.

    Insert a machine row directly into the fixture's state.db
    (the only way to set a known timestamp), GET /ui/machines,
    and assert both the compact form (visible text) and the raw
    form (title= attribute for hover) render.
    """
    from bty.web import _db as _bty_db

    state_path = tmp_path / "state.db"
    with _bty_db.open_db(state_path) as conn:
        conn.execute(
            "INSERT INTO machines (mac, boot_mode, last_seen_at, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                "aa:bb:cc:dd:ee:ff",
                "bty-tui",
                "2026-05-17T20:21:09.155109+00:00",
                "2026-05-17T20:00:00+00:00",
                "2026-05-17T20:21:09.155109+00:00",
            ),
        )
        conn.commit()
    page = app_client.get("/ui/machines", cookies=AUTH)
    assert page.status_code == 200, page.text
    # Compact form rendered in the row body (no offset, no " UTC").
    assert "2026-05-17 20:21:09" in page.text
    assert "2026-05-17 20:21:09 UTC" not in page.text
    # Raw form kept in the title= attribute for hover precision.
    assert 'title="2026-05-17T20:21:09.155109+00:00"' in page.text


# --------------------------------------------------------------------------
# _safe_path: direct unit tests for the path-traversal guard
# --------------------------------------------------------------------------
#
# The HTTP layer's helper that resolves ``root / name`` and refuses
# any name that could escape the root. Indirectly covered by the
# upload / image / boot-artifact endpoint tests, but pinning the
# contract directly guards against subtle regressions when those
# callers change.


def test_safe_path_accepts_plain_name(tmp_path: Path) -> None:
    """A bare filename returns ``root / name`` resolved."""
    from bty.web._app import _safe_path

    result = _safe_path(tmp_path, "image.img.gz")
    assert result == (tmp_path / "image.img.gz").resolve()


@pytest.mark.parametrize(
    "bad",
    [
        "",  # empty
        ".",  # current dir
        "..",  # parent dir
        "with/slash",  # forward slash
        "with\\backslash",  # backslash (windows-style)
        "with\x00nul",  # NUL byte
        "../etc/passwd",  # classic traversal
        "/absolute",  # absolute -- starts with slash
    ],
)
def test_safe_path_rejects_traversal_inputs(tmp_path: Path, bad: str) -> None:
    """Each forbidden name shape raises 400 naming the bad input.
    Pinned individually so a future "drop the NUL check" edit fails
    on the specific case rather than masquerading as a generic
    upload-endpoint test failure."""
    from fastapi import HTTPException

    from bty.web._app import _safe_path

    with pytest.raises(HTTPException) as excinfo:
        _safe_path(tmp_path, bad)
    assert excinfo.value.status_code == 400
    # Detail names the offending input + the constraint (was a terse
    # "bad name"). Both reject paths start "invalid name <repr>:".
    assert "invalid name" in str(excinfo.value.detail)
    assert repr(bad) in str(excinfo.value.detail)


def test_safe_path_rejects_symlink_escape(tmp_path: Path) -> None:
    """A symlink whose name is innocuous but whose resolved target
    escapes the root must still 400. Final ``relative_to(root)`` is
    the backstop that catches resolved-out-of-root paths the
    syntactic check misses."""
    from fastapi import HTTPException

    from bty.web._app import _safe_path

    outside = tmp_path.parent / "elsewhere"
    outside.mkdir(exist_ok=True)
    (outside / "target.txt").write_text("escaped")
    root = tmp_path / "root"
    root.mkdir()
    (root / "innocent").symlink_to(outside / "target.txt")

    with pytest.raises(HTTPException) as excinfo:
        _safe_path(root, "innocent")
    assert excinfo.value.status_code == 400


def test_seed_boot_dir_copies_baked_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The container bakes its custom ipxe.efi under BTY_BOOT_SEED_DIR;
    startup copies it into an empty boot_root so GET /boot/ipxe.efi works
    out of the box."""
    from bty.web._app import _seed_boot_dir

    seed = tmp_path / "seed"
    seed.mkdir()
    (seed / "ipxe.efi").write_bytes(b"BAKED")
    boot = tmp_path / "boot"

    monkeypatch.setenv("BTY_BOOT_SEED_DIR", str(seed))
    _seed_boot_dir(boot)
    assert (boot / "ipxe.efi").read_bytes() == b"BAKED"


def test_seed_boot_dir_skips_dotfile_placeholder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A .gitkeep placeholder in an otherwise-empty seed dir (dev builds)
    is not published into boot_root."""
    from bty.web._app import _seed_boot_dir

    seed = tmp_path / "seed"
    seed.mkdir()
    (seed / ".gitkeep").write_bytes(b"")
    boot = tmp_path / "boot"

    monkeypatch.setenv("BTY_BOOT_SEED_DIR", str(seed))
    _seed_boot_dir(boot)
    assert not (boot / ".gitkeep").exists()


def test_seed_boot_dir_never_overwrites_operator_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An operator-placed bootfile in boot_root always wins over the baked one."""
    from bty.web._app import _seed_boot_dir

    seed = tmp_path / "seed"
    seed.mkdir()
    (seed / "ipxe.efi").write_bytes(b"BAKED")
    boot = tmp_path / "boot"
    boot.mkdir()
    (boot / "ipxe.efi").write_bytes(b"OPERATOR")

    monkeypatch.setenv("BTY_BOOT_SEED_DIR", str(seed))
    _seed_boot_dir(boot)
    assert (boot / "ipxe.efi").read_bytes() == b"OPERATOR"


def test_seed_boot_dir_noop_when_unset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Host / dev installs leave BTY_BOOT_SEED_DIR unset; seeding is a no-op
    and does not create boot_root."""
    from bty.web._app import _seed_boot_dir

    monkeypatch.delenv("BTY_BOOT_SEED_DIR", raising=False)
    boot = tmp_path / "boot"
    _seed_boot_dir(boot)
    assert not boot.exists()
