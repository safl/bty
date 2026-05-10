"""Tests for ``bty.web``.

Use FastAPI's ``TestClient`` against an app constructed via
:func:`bty.web._app.create_app` with a ``tmp_path``-backed SQLite.
No monkeypatching of module-level globals; each test gets its own
isolated app + db. The ``app_client`` fixture drives ``POST /ui/login``
with PAM monkeypatched to always succeed, captures the resulting
session cookie, and exposes it via ``AUTH`` for tests that explicitly
attach it.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from bty.web._app import create_app

TEST_SERVICE_USER = "bty-test"
TEST_SECRET_KEY = "test-secret-not-for-prod-use"

# Mutated by the ``app_client`` fixture: tests authenticate via
# ``cookies=AUTH`` (a dict like ``{"bty-token": "..."}``); requests
# without the cookie hit the real auth dep and 401.
AUTH: dict[str, str] = {}


@pytest.fixture
def app_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """Yield a TestClient against an isolated bty-web app.

    PAM is monkeypatched to always succeed; the fixture POSTs
    ``/ui/login`` once to mint a real session cookie, captures it for
    ``cookies=AUTH``, then clears the client's sticky cookies so each
    test opts in to authentication explicitly via ``cookies=AUTH`` (or
    omits it to test the unauthed path).
    """
    state = tmp_path / "state.db"
    image_root = tmp_path / "images"
    image_root.mkdir()
    boot_root = tmp_path / "boot"
    boot_root.mkdir()
    # Seed a fake live-env triplet so /boot/{name} tests can hit real files.
    (boot_root / "bty-netboot-x86_64.vmlinuz").write_bytes(b"fake-kernel")
    (boot_root / "bty-netboot-x86_64.initrd").write_bytes(b"fake-initrd")
    (boot_root / "bty-netboot-x86_64.squashfs").write_bytes(b"fake-squashfs")
    # Seed an image too so /images/{name} tests work.
    (image_root / "demo.qcow2").write_bytes(b"fake-image")
    app = create_app(
        state_path=state,
        service_user=TEST_SERVICE_USER,
        secret_key=TEST_SECRET_KEY,
        image_root=image_root,
        boot_root=boot_root,
    )

    import pamela

    monkeypatch.setattr(pamela, "authenticate", lambda *a, **kw: True)

    with TestClient(app) as client:
        r = client.post(
            "/ui/login",
            data={"password": "pytest-password"},
            follow_redirects=False,
        )
        assert r.status_code == 303, r.text
        cookie_value = r.cookies.get("bty-token")
        assert cookie_value is not None
        AUTH.clear()
        AUTH["bty-token"] = cookie_value
        # Drop sticky cookies so unauthed-path tests aren't accidentally authed.
        client.cookies.clear()
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
    """An unknown MAC auto-discovers with ``boot_policy=tui`` and is
    served the interactive-live-env iPXE chain. This is "bty-on-a-USB
    but over the network": first PXE contact lands the operator at
    bty-tui without any prior server-side configuration."""
    r = app_client.get("/pxe/aa:bb:cc:dd:ee:ff")
    assert r.status_code == 200
    body = r.text
    assert "bty.mode=interactive" in body
    assert "aa:bb:cc:dd:ee:ff" in body
    assert "kernel" in body  # chains into the live env


def test_pxe_invalid_mac_returns_400(app_client: TestClient) -> None:
    r = app_client.get("/pxe/not-a-mac")
    assert r.status_code == 400


def test_pxe_bootstrap_returns_self_referential_chain(app_client: TestClient) -> None:
    """The static iPXE script that dnsmasq points iPXE clients at on
    second-stage DHCP. Must reference back to whichever Host the
    client used to reach the server, and use iPXE's runtime MAC
    substitution."""
    r = app_client.get("/pxe-bootstrap.ipxe", headers={"Host": "192.0.2.1:8080"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    body = r.text
    assert body.startswith("#!ipxe"), body
    # Self-referential chain: the URL uses the Host header.
    assert "chain http://192.0.2.1:8080/pxe/${net0/mac:hexhyp}" in body
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
        "image_sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
        "provisioning_mode": "cijoe-task",
        "hostname": "bty-test-01",
    }

    # Create / upsert
    r = app_client.put(f"/machines/{mac}", json=body, cookies=AUTH)
    assert r.status_code == 200
    created = r.json()
    assert created["mac"] == mac
    assert (
        created["image_sha256"]
        == "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
    )
    assert created["provisioning_mode"] == "cijoe-task"
    assert created["hostname"] == "bty-test-01"

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
        json={"provisioning_mode": "none"},
        cookies=AUTH,
    )
    assert r.status_code == 200
    assert r.json()["mac"] == "aa:bb:cc:dd:ee:ff"


def test_machine_upsert_rejects_invalid_provisioning_mode(app_client: TestClient) -> None:
    r = app_client.put(
        "/machines/aa:bb:cc:dd:ee:ff",
        json={"provisioning_mode": "garbage"},
        cookies=AUTH,
    )
    assert r.status_code == 422  # FastAPI body validation


def test_pxe_for_known_mac_uses_assignment_template(app_client: TestClient) -> None:
    mac = "aa:bb:cc:dd:ee:ff"
    app_client.put(
        f"/machines/{mac}",
        json={
            "image_sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
            "provisioning_mode": "none",
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
    ``boot_policy`` is ``tui``: the unknown MAC chains into the live env in
    interactive mode (bty-tui), letting the operator pick + flash an image
    by hand without prior server-side configuration."""
    mac = "11:22:33:44:55:66"

    # Pre-condition: not in the DB.
    pre = app_client.get(f"/machines/{mac}", cookies=AUTH)
    assert pre.status_code == 404

    # PXE client (no auth) hits the endpoint.
    r = app_client.get(f"/pxe/{mac}")
    assert r.status_code == 200
    assert "bty.mode=interactive" in r.text  # tui template

    # Now visible to the operator.
    found = app_client.get(f"/machines/{mac}", cookies=AUTH)
    assert found.status_code == 200
    body = found.json()
    assert body["mac"] == mac
    assert body["image_sha256"] is None  # discovered, not yet assigned
    assert body["provisioning_mode"] == "none"
    assert body["boot_policy"] == "tui"  # auto-discovery default
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
            "image_sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
            "provisioning_mode": "cijoe-task",
        },
        cookies=AUTH,
    )
    before = app_client.get(f"/machines/{mac}", cookies=AUTH).json()
    assert (
        before["image_sha256"] == "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
    )
    assert before["discovered_at"] is None  # PUT-created

    app_client.get(f"/pxe/{mac}")
    after = app_client.get(f"/machines/{mac}", cookies=AUTH).json()
    assert (
        after["image_sha256"] == "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
    )  # untouched
    assert after["provisioning_mode"] == "cijoe-task"
    assert after["last_seen_at"] is not None
    # discovered_at is set on first /pxe contact even for PUT-created rows
    assert after["discovered_at"] is not None


# ---------- image / boot upload --------------------------------------------


def test_put_image_uploads_to_image_root(app_client: TestClient) -> None:
    """``PUT /images/{name}`` lands the body bytes at
    ``image_root/<name>`` and the file is round-trippable via the
    open ``GET /images/{name}``."""
    body = b"\x01\x02\x03" * 1024
    r = app_client.put("/images/upload.qcow2", content=body, cookies=AUTH)
    assert r.status_code == 200, r.text
    payload = r.json()
    assert payload["name"] == "upload.qcow2"
    assert payload["size_bytes"] == len(body)
    # Same bytes flow back via the open serve route.
    served = app_client.get("/images/upload.qcow2")
    assert served.status_code == 200
    assert served.content == body


def test_put_image_overwrites_existing(app_client: TestClient) -> None:
    first = app_client.put("/images/x.qcow2", content=b"old", cookies=AUTH)
    assert first.status_code == 200
    second = app_client.put("/images/x.qcow2", content=b"newer-bytes", cookies=AUTH)
    assert second.status_code == 200
    assert second.json()["size_bytes"] == len(b"newer-bytes")
    assert app_client.get("/images/x.qcow2").content == b"newer-bytes"


def test_put_image_rejects_path_traversal(app_client: TestClient) -> None:
    """``..`` and slashes mustn't escape the image root. FastAPI's
    path converter already strips raw ``/`` from ``{name}``, but
    URL-encoded variants and ``..`` need an explicit reject."""
    r = app_client.put("/images/..%2Fescape.qcow2", content=b"x", cookies=AUTH)
    # Three valid rejects: 400 (explicit traversal-reject), 404 (no
    # such file), 405 (URL-decoded path becomes ``..%2F...`` which
    # routes onto the GET /images/{key}/{name:path} pattern and
    # PUT isn't allowed there). All three deny the upload; the
    # vulnerability would be a 200 + actual write outside image_root.
    assert r.status_code in {400, 404, 405}


def test_put_image_requires_auth(app_client: TestClient) -> None:
    r = app_client.put("/images/x.qcow2", content=b"x")
    assert r.status_code == 401


def test_put_image_rejects_oversized_upload(
    app_client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_stream_upload`` caps the body at ``BTY_MAX_UPLOAD_BYTES``
    (default 200 GiB; tunable via env). Without the cap a runaway
    script or hostile request that streams forever would fill the
    image-root partition. The cap kills the upload mid-stream, the
    .partial cleanup branch unlinks the half-written file, and the
    response is 413."""
    # Set a tiny cap so the test doesn't actually need to push GiBs.
    monkeypatch.setenv("BTY_MAX_UPLOAD_BYTES", "16")

    # 64-byte payload, well past the 16-byte cap.
    payload = b"a" * 64
    r = app_client.put("/images/oversized.img", content=payload, cookies=AUTH)
    assert r.status_code == 413
    # Partial cleanup: no oversized.img or oversized.img.partial
    # left in the image-root. ``demo.qcow2`` from the fixture is
    # expected; its ``.sha256`` sidecar may also be present
    # (auto-import races the test). Anything ``oversized*`` would
    # be the bug.
    image_root = tmp_path / "images"
    leftovers = sorted(p.name for p in image_root.iterdir() if p.name.startswith("oversized"))
    assert leftovers == [], f"upload cap left behind: {leftovers}"


def test_put_image_triggers_hash_so_entry_appears_in_listing(
    app_client: TestClient,
    tmp_path: Path,
) -> None:
    """A successful PUT /images/{name} enqueues a hash job so the
    image surfaces in /images on the next request without waiting
    for the next server restart's auto-import sweep. Without this,
    operators uploading via the API would see the file land but
    bty-tui --server clients would not see it as flashable until
    bty-web bounced.
    """
    import hashlib
    import time

    payload = b"upload-and-hash"
    expected_sha = hashlib.sha256(payload).hexdigest()
    r = app_client.put("/images/uploaded.img", content=payload, cookies=AUTH)
    assert r.status_code == 200
    # The hash runs in a worker; poll briefly.
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        r2 = app_client.get("/images")
        names = {row["name"] for row in r2.json()}
        if "uploaded.img" in names:
            break
        time.sleep(0.05)
    rows = r2.json()
    by_name = {row["name"]: row for row in rows}
    assert "uploaded.img" in by_name, "upload didn't trigger an auto-hash"
    assert by_name["uploaded.img"]["url"].endswith(f"/images/{expected_sha}/uploaded.img")


def test_put_boot_uploads_to_boot_root(app_client: TestClient) -> None:
    """``PUT /boot/{name}`` symmetric to /images/{name} but lands
    under boot_root - this is how the live trio gets onto the
    appliance via the API instead of scp / fetch-from-release."""
    body = b"vmlinuz-bytes-here"
    r = app_client.put("/boot/bty-netboot-x86_64.vmlinuz", content=body, cookies=AUTH)
    assert r.status_code == 200
    served = app_client.get("/boot/bty-netboot-x86_64.vmlinuz")
    assert served.status_code == 200
    assert served.content == body


def test_put_boot_requires_auth(app_client: TestClient) -> None:
    r = app_client.put("/boot/anything", content=b"x")
    assert r.status_code == 401


# ---------- images ----------------------------------------------------------


def test_list_images_returns_seeded_fixture(app_client: TestClient) -> None:
    """The fixture seeds ``demo.qcow2`` so the file-serving routes
    have something to return; ``GET /images`` exposes it via the
    image catalog."""
    r = app_client.get("/images", cookies=AUTH)
    assert r.status_code == 200
    rows = r.json()
    assert {row["name"] for row in rows} == {"demo.qcow2"}


def test_list_images_is_open_for_pxe_clients(app_client: TestClient) -> None:
    """``GET /images`` is an open route: the bty-tui-on-PXE flow needs
    to enumerate the catalog from inside the live env without first
    bootstrapping a session. Same trust model as ``GET /images/{name}``
    (already open) and the other ``/pxe/`` routes."""
    r = app_client.get("/images")  # no Authorization header
    assert r.status_code == 200


def test_auto_import_hashes_unhashed_dir_scan_files(tmp_path: Path) -> None:
    """bty-web's lifespan walks ``BTY_IMAGE_ROOT`` at startup and
    enqueues a hash job for every file without a ``.sha256``
    sidecar. After the hashing settles, the file is listable via
    ``/images`` with a server URL.

    Asserts the auto-import path fires; uses tiny payloads + a
    short polling loop for the sidecar to avoid flake on slow CI.
    """
    import hashlib
    import time

    image_root = tmp_path / "images"
    image_root.mkdir()
    payload = b"auto-import me"
    expected_sha = hashlib.sha256(payload).hexdigest()
    img_path = image_root / "fresh.img"
    img_path.write_bytes(payload)
    sidecar = image_root / "fresh.img.sha256"
    assert not sidecar.exists()

    state = tmp_path / "state.db"
    app = create_app(
        state_path=state,
        service_user=TEST_SERVICE_USER,
        secret_key=TEST_SECRET_KEY,
        image_root=image_root,
    )
    with TestClient(app) as client:
        # The lifespan's auto-import enqueues the hash job; the
        # HashManager processes it in a worker thread. Wait briefly
        # for the sidecar to land (tiny file -> ms-scale).
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and not sidecar.exists():
            time.sleep(0.05)
        assert sidecar.exists(), "auto-import didn't write the sidecar"
        # Sidecar carries the right digest.
        assert sidecar.read_text().strip().split()[0] == expected_sha
        # /images now lists the entry with a server URL.
        r = client.get("/images")
        rows = r.json()
        names = {row["name"] for row in rows}
        assert "fresh.img" in names
        entry = next(row for row in rows if row["name"] == "fresh.img")
        assert entry["url"].endswith(f"/images/{expected_sha}/fresh.img")


def test_list_images_returns_files_under_image_root(
    tmp_path: Path,
) -> None:
    """``/images`` returns one entry per SHA-keyed image with a ``url``
    field. Files with sidecars surface immediately as server URLs;
    the bytes are served via ``/images/<sha>`` regardless of the
    on-disk filename."""
    import hashlib

    image_root = tmp_path / "images"
    image_root.mkdir()
    alpha_payload = b"\0" * 256
    beta_payload = b"\0" * 512
    (image_root / "alpha.qcow2").write_bytes(alpha_payload)
    (image_root / "beta.img").write_bytes(beta_payload)
    # Pre-create sidecars so the entries are immediately listable
    # rather than queued for auto-import.
    alpha_sha = hashlib.sha256(alpha_payload).hexdigest()
    beta_sha = hashlib.sha256(beta_payload).hexdigest()
    (image_root / "alpha.qcow2.sha256").write_text(f"{alpha_sha}  alpha.qcow2\n")
    (image_root / "beta.img.sha256").write_text(f"{beta_sha}  beta.img\n")

    state = tmp_path / "state.db"
    app = create_app(
        state_path=state,
        service_user=TEST_SERVICE_USER,
        secret_key=TEST_SECRET_KEY,
        image_root=image_root,
    )
    with TestClient(app) as client:
        # ``/images`` is open (the TUI-on-PXE flow needs to enumerate
        # without auth), so no session-cookie setup needed.
        r = client.get("/images")

    assert r.status_code == 200
    rows = r.json()
    names = {row["name"] for row in rows}
    assert names == {"alpha.qcow2", "beta.img"}
    # Each entry carries a ``url`` that the client (TUI / CLI)
    # flashes from. For dir-scan images the URL points at the
    # bty-web server's ``/images/<sha>`` endpoint.
    by_name = {row["name"]: row for row in rows}
    # URL shape is ``/images/<sha>/<filename>``: the sha binds the
    # content, the filename is decorative so format-by-extension
    # keeps working on the client.
    assert by_name["alpha.qcow2"]["url"].endswith(f"/images/{alpha_sha}/alpha.qcow2")
    assert by_name["beta.img"]["url"].endswith(f"/images/{beta_sha}/beta.img")
    assert by_name["alpha.qcow2"]["cached"] is True


def test_list_images_does_not_surface_bri_descriptors(tmp_path: Path) -> None:
    """``.bri`` is the bty-usb / bty-tui ad-hoc local-catalog
    format; bty-web is the SHA-keyed managed-catalog model. A
    ``.bri`` dropped into the server's image root must NOT
    appear in ``GET /images`` -- it can't bind to a machine
    (no SHA), so surfacing it would invite the operator to
    bind something they then can't flash."""
    image_root = tmp_path / "images"
    image_root.mkdir()
    (image_root / "demo.bri").write_text(
        'url = "https://example.invalid/demo.img.gz"\nname = "Demo"\n'
    )

    state = tmp_path / "state.db"
    app = create_app(
        state_path=state,
        service_user=TEST_SERVICE_USER,
        secret_key=TEST_SECRET_KEY,
        image_root=image_root,
    )
    with TestClient(app) as client:
        r = client.get("/images")

    assert r.status_code == 200
    rows = r.json()
    assert all(row["name"] != "Demo" for row in rows), (
        f"unexpected .bri row in bty-web /images output: {rows}"
    )


# ---------- create_app sanity ----------------------------------------------


# ---------- boot policy + flash chain --------------------------


def test_machine_default_boot_policy_is_local(app_client: TestClient) -> None:
    """A fresh PUT without an explicit boot_policy gets ``local`` -
    operators opt INTO reflashing on every boot."""
    r = app_client.put(
        "/machines/aa:bb:cc:dd:ee:ff",
        json={
            "image_sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
            "provisioning_mode": "none",
        },
        cookies=AUTH,
    )
    assert r.status_code == 200
    assert r.json()["boot_policy"] == "local"
    assert r.json()["last_flashed_at"] is None


def test_machine_upsert_rejects_malformed_sha256(app_client: TestClient) -> None:
    """``image_sha256`` must be 64 lower-case hex chars. A typo
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
            json={"image_sha256": bad, "boot_policy": "flash"},
            cookies=AUTH,
        )
        assert r.status_code == 422, f"expected 422 for {bad!r}, got {r.status_code}"


def test_machine_upsert_rejects_pathological_cijoe_task_ref(app_client: TestClient) -> None:
    """``cijoe_task_ref`` rejects NUL / newline / empty inputs at
    the Pydantic layer so they can't reach the subprocess launch
    or the audit log. Regular operator paths (absolute, relative,
    with spaces) stay accepted."""
    valid_sha = "0" * 64
    for bad in ("", "with\x00nul", "with\nnewline", "with\rcr"):
        r = app_client.put(
            "/machines/aa:bb:cc:dd:ee:ff",
            json={
                "image_sha256": valid_sha,
                "cijoe_task_ref": bad,
            },
            cookies=AUTH,
        )
        assert r.status_code == 422, f"expected 422 for {bad!r}, got {r.status_code}"

    # Sanity: real operator-shaped paths still pass.
    for ok in (
        "/var/lib/bty/tasks/post-flash.yaml",
        "tasks/relative-path.yaml",
        "/path with spaces/foo.yaml",
    ):
        r = app_client.put(
            "/machines/aa:bb:cc:dd:ee:ff",
            json={
                "image_sha256": valid_sha,
                "cijoe_task_ref": ok,
            },
            cookies=AUTH,
        )
        assert r.status_code == 200, f"expected 200 for {ok!r}, got {r.status_code} {r.text}"


def test_machine_upsert_rejects_empty_hostname(app_client: TestClient) -> None:
    """``hostname = ""`` would land in state.db blank and surface
    in the dashboard / banner as a meaningless empty cell. Reject
    explicit empty strings (a missing field still gets ``None``)."""
    r = app_client.put(
        "/machines/aa:bb:cc:dd:ee:ff",
        json={
            "image_sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
            "hostname": "",
        },
        cookies=AUTH,
    )
    assert r.status_code == 422


def test_machine_upsert_rejects_invalid_hostname_shapes(app_client: TestClient) -> None:
    """The pre-v0.7.36 hostname pattern (``[a-zA-Z0-9.-]+``) accepted
    invalid shapes like ``-foo`` (leading hyphen), ``foo-`` (trailing),
    ``..``, ``.foo``, and bare ``-``. Tightened to RFC-1123-ish:
    each label is alnum, hyphen-internal-only, dot-separated. These
    used to land in state.db where they confused the agetty
    \\S{name} renderer at console banner time; now rejected at PUT."""
    valid_sha = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
    for bad in (
        "-foo",  # leading hyphen
        "foo-",  # trailing hyphen
        ".foo",  # leading dot
        "foo.",  # trailing dot
        "foo..bar",  # consecutive dots
        "-",  # bare hyphen
        "..",  # bare dots
        "host_with_underscore",  # underscore not in pattern
    ):
        r = app_client.put(
            "/machines/aa:bb:cc:dd:ee:ff",
            json={"image_sha256": valid_sha, "hostname": bad},
            cookies=AUTH,
        )
        assert r.status_code == 422, f"expected 422 for {bad!r}, got {r.status_code}"


def test_machine_upsert_accepts_real_hostname_shapes(app_client: TestClient) -> None:
    """The tightened pattern still accepts shapes operators
    actually use: short alnum, hyphenated, FQDN, single-label."""
    valid_sha = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
    for ok in (
        "host",
        "host01",
        "rack-01",
        "node-1.lab.example.org",
        "single",
        "a",  # one-char label
    ):
        r = app_client.put(
            "/machines/aa:bb:cc:dd:ee:ff",
            json={"image_sha256": valid_sha, "hostname": ok},
            cookies=AUTH,
        )
        assert r.status_code == 200, f"expected 200 for {ok!r}, got {r.status_code} {r.text}"


def test_machine_upsert_rejects_unknown_fields(app_client: TestClient) -> None:
    """``MachineUpsert(extra="forbid")`` -- a stale client (or
    operator typo) sending the pre-M22 ``image`` field instead of
    ``image_sha256`` must 422 loudly. The previous default
    silently accepted unknown keys + landed an assignment with
    ``image_sha256=NULL``, which then surfaced as "no bty
    assignment" at PXE-chain time. This regression test pins the
    strict-extra contract so the failure surfaces at PUT time."""
    r = app_client.put(
        "/machines/aa:bb:cc:dd:ee:ff",
        json={
            "image": "stale-pre-m22-filename.qcow2",
            "boot_policy": "flash",
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


def test_machine_upsert_accepts_boot_policy_flash(app_client: TestClient) -> None:
    r = app_client.put(
        "/machines/aa:bb:cc:dd:ee:ff",
        json={
            "image_sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
            "provisioning_mode": "none",
            "boot_policy": "flash",
        },
        cookies=AUTH,
    )
    assert r.status_code == 200
    assert r.json()["boot_policy"] == "flash"


def test_machine_upsert_rejects_unknown_boot_policy(app_client: TestClient) -> None:
    r = app_client.put(
        "/machines/aa:bb:cc:dd:ee:ff",
        json={
            "image_sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
            "boot_policy": "yolo",
        },
        cookies=AUTH,
    )
    assert r.status_code == 422


def test_pxe_local_policy_assigned_machine_returns_local_template(
    app_client: TestClient,
) -> None:
    """boot_policy=local + image assigned: still sanboot. Reflashing is
    opt-in via boot_policy=flash, not implicit on assignment."""
    app_client.put(
        "/machines/aa:bb:cc:dd:ee:ff",
        json={"image_sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"},
        cookies=AUTH,
    )
    r = app_client.get("/pxe/aa:bb:cc:dd:ee:ff")
    assert r.status_code == 200
    body = r.text
    # ipxe.j2 (placeholder local template) - explicitly NOT the flash chain
    assert "kernel" not in body
    assert "bty.image_url" not in body


def test_pxe_flash_policy_returns_chain_with_args(app_client: TestClient) -> None:
    """boot_policy=flash + image: chain into kernel/initrd with the
    four bty.* cmdline params the live env reads."""
    app_client.put(
        "/machines/aa:bb:cc:dd:ee:ff",
        json={
            "image_sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
            "provisioning_mode": "cijoe-task",
            "boot_policy": "flash",
        },
        cookies=AUTH,
    )
    r = app_client.get("/pxe/aa:bb:cc:dd:ee:ff", headers={"Host": "bty.local:8080"})
    assert r.status_code == 200
    body = r.text
    assert body.startswith("#!ipxe"), body
    # Template uses an iPXE variable for the base URL so the script
    # reads cleanly; the variable is set from the request's Host.
    assert "set bty-base http://bty.local:8080" in body
    assert "kernel ${bty-base}/boot/bty-netboot-x86_64.vmlinuz" in body
    assert "initrd ${bty-base}/boot/bty-netboot-x86_64.initrd" in body
    # live-boot needs ``fetch=`` to know where to grab the squashfs.
    assert "fetch=${bty-base}/boot/bty-netboot-x86_64.squashfs" in body
    # Console mirror to ttyS0 so headless / IPMI / test serial works.
    assert "console=ttyS0,115200" in body
    # Cmdline params: live env's bty-flash-on-boot reads these.
    assert "bty.server=${bty-base}" in body
    assert "bty.mac=aa:bb:cc:dd:ee:ff" in body
    # URL shape is ``/images/<sha>/<name>``: the SHA binds the
    # bytes; the trailing /<name> preserves format-by-extension on
    # the live-env side. Without /<name> the live env's local
    # cache file gets named after the bare SHA and
    # ``bty.images.detect_format`` returns None for it.
    assert (
        "bty.image_url=${bty-base}/images/0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef/"
        in body
    )
    # ``bty.provisioning`` is no longer emitted on the cmdline --
    # post-flash provisioning is server-driven via cijoe-task,
    # not live-env-driven.
    assert "bty.provisioning" not in body


def test_pxe_tui_policy_returns_interactive_chain(app_client: TestClient) -> None:
    """boot_policy=tui: chain into the live env with bty.mode=interactive
    so the live env launches bty-tui on tty1 instead of auto-flashing.
    No image / no provisioning cmdline params - the operator picks at
    run time."""
    app_client.put(
        "/machines/aa:bb:cc:dd:ee:ff",
        json={"boot_policy": "tui"},
        cookies=AUTH,
    )
    r = app_client.get("/pxe/aa:bb:cc:dd:ee:ff", headers={"Host": "bty.local:8080"})
    assert r.status_code == 200
    body = r.text
    assert body.startswith("#!ipxe"), body
    assert "set bty-base http://bty.local:8080" in body
    assert "kernel ${bty-base}/boot/bty-netboot-x86_64.vmlinuz" in body
    assert "initrd ${bty-base}/boot/bty-netboot-x86_64.initrd" in body
    assert "bty.mode=interactive" in body
    assert "bty.server=${bty-base}" in body
    assert "bty.mac=aa:bb:cc:dd:ee:ff" in body
    # Interactive mode must NOT pre-decide image / provisioning - those
    # come from the operator's TUI selection.
    assert "bty.image_url" not in body
    assert "bty.provisioning" not in body


def test_machine_upsert_accepts_boot_policy_tui(app_client: TestClient) -> None:
    """``boot_policy='tui'`` is accepted by Pydantic validation alongside
    ``local`` and ``flash``."""
    r = app_client.put(
        "/machines/aa:bb:cc:dd:ee:ff",
        json={"boot_policy": "tui"},
        cookies=AUTH,
    )
    assert r.status_code == 200
    assert r.json()["boot_policy"] == "tui"


def test_pxe_done_updates_last_flashed_at(app_client: TestClient) -> None:
    app_client.put(
        "/machines/aa:bb:cc:dd:ee:ff",
        json={
            "image_sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
            "boot_policy": "flash",
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
    # boot_policy=flash across reflashes.
    assert after["boot_policy"] == "flash"


def test_pxe_done_404_for_unknown_mac(app_client: TestClient) -> None:
    r = app_client.post("/pxe/00:11:22:33:44:55/done")
    assert r.status_code == 404


# ---------- online cijoe auto-trigger (milestone 15) ----------------------


def test_pxe_done_triggers_online_task_when_configured(app_client: TestClient) -> None:
    """``provisioning_mode='cijoe-task'`` + task ref + last_seen_ip
    means the completion signal kicks off a task run via
    TaskManager. The runner's ``kick_off`` should be called with
    the assigned task + the IP the live env was last seen from."""
    from unittest.mock import patch

    # Seed via API (sets cijoe_task_ref + provisioning_mode).
    app_client.put(
        "/machines/aa:bb:cc:dd:ee:ff",
        json={
            "image_sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
            "provisioning_mode": "cijoe-task",
            "cijoe_task_ref": "/var/lib/bty/tasks/post-flash.yaml",
        },
        cookies=AUTH,
    )
    # PXE contact populates last_seen_ip.
    app_client.get("/pxe/aa:bb:cc:dd:ee:ff")

    with patch(
        "bty.web._task.TaskManager.kick_off",
    ) as mock_kick:
        r = app_client.post("/pxe/aa:bb:cc:dd:ee:ff/done")

    assert r.status_code == 204
    mock_kick.assert_called_once()
    kwargs = mock_kick.call_args.kwargs
    assert kwargs["mac"] == "aa:bb:cc:dd:ee:ff"
    assert kwargs["task_ref"] == "/var/lib/bty/tasks/post-flash.yaml"
    # last_seen_ip from the GET above; TestClient uses 'testclient'
    # as the client host.
    assert kwargs["target_ip"] == "testclient"


def test_pxe_done_does_not_trigger_when_provisioning_mode_is_other(app_client: TestClient) -> None:
    from unittest.mock import patch

    app_client.put(
        "/machines/aa:bb:cc:dd:ee:ff",
        json={
            "image_sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
            "provisioning_mode": "none",
            "cijoe_task_ref": "/var/lib/bty/tasks/post-flash.yaml",
        },
        cookies=AUTH,
    )
    app_client.get("/pxe/aa:bb:cc:dd:ee:ff")
    with patch("bty.web._task.TaskManager.kick_off") as mock_kick:
        r = app_client.post("/pxe/aa:bb:cc:dd:ee:ff/done")
    assert r.status_code == 204
    mock_kick.assert_not_called()


def test_pxe_done_does_not_trigger_when_task_ref_missing(app_client: TestClient) -> None:
    from unittest.mock import patch

    app_client.put(
        "/machines/aa:bb:cc:dd:ee:ff",
        json={
            "image_sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
            "provisioning_mode": "cijoe-task",
            # no cijoe_task_ref
        },
        cookies=AUTH,
    )
    app_client.get("/pxe/aa:bb:cc:dd:ee:ff")
    with patch("bty.web._task.TaskManager.kick_off") as mock_kick:
        r = app_client.post("/pxe/aa:bb:cc:dd:ee:ff/done")
    assert r.status_code == 204
    mock_kick.assert_not_called()


def test_pxe_done_does_not_trigger_when_last_seen_ip_missing(app_client: TestClient) -> None:
    """A PUT-only machine (operator created the record but the box
    has never PXE-contacted) has no ``last_seen_ip`` to SSH at, so
    cijoe-task must not kick off on the spurious ``/pxe/{mac}/done``
    that arrives when the box eventually does flash through bty-web
    -- but the kick-off guard is a clean three-condition check
    (provisioning_mode == cijoe-task AND cijoe_task_ref AND
    last_seen_ip), and this test pins the third condition."""
    from unittest.mock import patch

    app_client.put(
        "/machines/aa:bb:cc:dd:ee:ff",
        json={
            "image_sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
            "provisioning_mode": "cijoe-task",
            "cijoe_task_ref": "/var/lib/bty/tasks/post-flash.yaml",
        },
        cookies=AUTH,
    )
    # Crucially: NO ``GET /pxe/{mac}`` here. Without that contact,
    # ``last_seen_ip`` stays NULL.
    with patch("bty.web._task.TaskManager.kick_off") as mock_kick:
        r = app_client.post("/pxe/aa:bb:cc:dd:ee:ff/done")
    assert r.status_code == 204
    mock_kick.assert_not_called()


def test_machine_response_includes_task_columns(app_client: TestClient) -> None:
    """Schema migration: new columns exposed via the wire model."""
    app_client.put(
        "/machines/aa:bb:cc:dd:ee:ff",
        json={"image_sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"},
        cookies=AUTH,
    )
    body = app_client.get("/machines/aa:bb:cc:dd:ee:ff", cookies=AUTH).json()
    assert body["last_task_run_at"] is None
    assert body["last_task_status"] is None
    assert body["last_task_output_path"] is None


# ---------- /tasks API (v0.7.37 cancelable runs) ---------------------------


def test_tasks_list_requires_auth(app_client: TestClient) -> None:
    """``/tasks`` is operator-only (mirrors /catalog/* / /boot/releases)."""
    r = app_client.get("/tasks")
    assert r.status_code == 401


def test_tasks_list_returns_empty_initially(app_client: TestClient) -> None:
    r = app_client.get("/tasks", cookies=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body == {"tasks": []}


def test_tasks_cancel_unknown_mac_returns_404(app_client: TestClient) -> None:
    r = app_client.delete("/tasks/aa:bb:cc:dd:ee:ff", cookies=AUTH)
    assert r.status_code == 404


def test_tasks_cancel_invalid_mac_returns_400(app_client: TestClient) -> None:
    """``_normalise_mac`` rejects malformed MACs at the boundary."""
    r = app_client.delete("/tasks/not-a-mac", cookies=AUTH)
    assert r.status_code == 400


# Note: surfacing of in-flight task state via ``GET /tasks`` is
# covered by ``test_web_task.py::test_list_returns_snapshot``,
# which exercises the manager directly without needing a real
# cijoe subprocess + a TestClient that fakes a routable client IP.
# The integration shape (``GET`` returns 200 with a ``tasks`` key)
# is already covered by ``test_tasks_list_returns_empty_initially``
# and ``test_tasks_list_requires_auth`` above.


# ---------- /events API (v0.7.38 audit log) -----------------------------


def test_events_list_requires_auth(app_client: TestClient) -> None:
    r = app_client.get("/events")
    assert r.status_code == 401


def test_events_list_no_operator_or_pxe_activity_initially(app_client: TestClient) -> None:
    """Before any operator / PXE activity, the only rows the audit
    log has are auto-import side-effects (the lifespan hashes
    seeded images and emits ``image.hashed``). The test fixture
    seeds ``demo.qcow2`` so that one is expected; everything
    else should be absent."""
    r = app_client.get("/events", cookies=AUTH)
    assert r.status_code == 200
    events = r.json()["events"]
    # No operator-driven or pxe-client-driven rows yet.
    assert all(e["actor"] not in {"operator", "pxe-client"} for e in events)


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
            "image_sha256": "0" * 64,
            "boot_policy": "flash",
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


def test_events_include_image_hashed_from_auto_import(app_client: TestClient) -> None:
    """The lifespan startup auto-imports image_root files without
    sidecars; the HashManager logs ``image.hashed`` as ``actor=
    'system'`` once each completes. The fixture seeds
    ``demo.qcow2`` so a row should be present by the time the
    test runs.

    Filter by kind to dodge the bare-list ordering -- relying on
    "the first event" would be brittle if the lifespan grew more
    auto-import work.
    """
    r = app_client.get("/events", params={"kind": "image.hashed"}, cookies=AUTH)
    assert r.status_code == 200
    events = r.json()["events"]
    assert events, "expected an image.hashed row from auto-import"
    row = events[0]
    assert row["actor"] == "system"
    assert row["subject_kind"] == "image"
    assert row["subject_id"] == "demo.qcow2"
    # Sha lands in details.
    assert row["details"] is not None
    assert isinstance(row["details"]["sha256"], str)
    assert len(row["details"]["sha256"]) == 64


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
    app_client.put(f"/machines/{mac}", json={"boot_policy": "local"}, cookies=AUTH)
    r = app_client.get("/events", cookies=AUTH)
    assert r.status_code == 200
    by_kind = {e["kind"]: e for e in r.json()["events"]}
    # Both pxe-client and operator events carry the same
    # testclient host (the ASGI default for httpx TestClient).
    assert by_kind["machine.discovered"]["source_ip"] == "testclient"
    upsert = by_kind.get("machine.created") or by_kind.get("machine.upserted")
    assert upsert is not None
    assert upsert["source_ip"] == "testclient"


def test_events_filter_by_actor(app_client: TestClient) -> None:
    """``GET /events?actor=operator`` returns only operator-driven
    rows; ``actor=pxe-client`` only PXE check-ins. Powers the
    /ui/events actor dropdown for triaging "show me what
    operators did" vs "show me what targets phoned home"."""
    mac = "aa:bb:cc:dd:ee:fb"
    app_client.get(f"/pxe/{mac}")  # pxe-client: machine.discovered
    app_client.put(  # operator: machine.upserted
        f"/machines/{mac}",
        json={"boot_policy": "local"},
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
    ``catalog.entry.add_failed`` event lands in the audit log
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
    r = app_client.get("/events", params={"kind": "catalog.entry.add_failed"}, cookies=AUTH)
    events = r.json()["events"]
    assert len(events) == 1
    row = events[0]
    assert row["actor"] == "operator"
    assert row["subject_kind"] == "catalog"
    assert row["subject_id"] == "https://example.com/foo.img.gz"
    assert row["details"] is not None
    assert "upstream gave 404" in row["details"]["error"]


def test_image_upload_oversized_logs_failure_event(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An upload that exceeds ``BTY_MAX_UPLOAD_BYTES`` lands an
    ``image.upload_failed`` event so the audit trail is symmetric
    with the success path's ``image.uploaded``. Force the cap
    very low so the test fixture's ~10-byte payload trips it."""
    monkeypatch.setenv("BTY_MAX_UPLOAD_BYTES", "5")
    r = app_client.put(
        "/images/big.qcow2",
        content=b"this is more than 5 bytes",
        cookies=AUTH,
    )
    assert r.status_code == 413
    r = app_client.get("/events", params={"kind": "image.upload_failed"}, cookies=AUTH)
    events = r.json()["events"]
    assert len(events) == 1
    row = events[0]
    assert row["actor"] == "operator"
    assert row["subject_kind"] == "image"
    assert row["subject_id"] == "big.qcow2"
    assert row["details"]["status_code"] == 413


def test_image_upload_oserror_logs_failure_event(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An OSError mid-upload (disk full, read-only fs, etc.) also
    lands an ``image.upload_failed`` event so the audit trail
    isn't only HTTPException-shaped failures.

    Starlette's TestClient re-raises server exceptions by default
    (``raise_server_exceptions=True``); we accept the OSError
    propagating in-test and assert the event was recorded
    *before* the re-raise."""
    from bty.web import _app

    async def boom(*_a: object, **_kw: object) -> dict[str, object]:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(_app, "_stream_upload", boom)
    with pytest.raises(OSError, match="No space left on device"):
        app_client.put(
            "/images/whatever.qcow2",
            content=b"...",
            cookies=AUTH,
        )
    r = app_client.get("/events", params={"kind": "image.upload_failed"}, cookies=AUTH)
    events = r.json()["events"]
    assert len(events) == 1
    row = events[0]
    assert row["details"]["status_code"] == 500
    assert "No space left on device" in row["details"]["error"]


def test_settings_pxe_activate_failure_logs_event(app_client: TestClient) -> None:
    """A failed PXE activation must land a
    ``settings.pxe.activate_failed`` event so the audit trail is
    symmetric with the success path. Posting an invalid interface
    name (regex-rejected before the helper is even shelled out)
    is the deterministic failure trigger that doesn't depend on
    sudo / helper presence."""
    r = app_client.post(
        "/ui/settings/pxe-activate",
        data={"interface": "!!!", "subnet": "192.168.1.0/24"},
        cookies=AUTH,
        follow_redirects=False,
    )
    # Settings page re-renders with the red flash.
    assert r.status_code == 200
    r = app_client.get(
        "/events",
        params={"kind": "settings.pxe.activate_failed"},
        cookies=AUTH,
    )
    assert r.status_code == 200
    events = r.json()["events"]
    assert len(events) == 1
    row = events[0]
    assert row["actor"] == "operator"
    assert row["subject_kind"] == "settings"
    assert row["subject_id"] == "pxe"
    assert row["details"] is not None
    assert "invalid interface name" in row["details"]["error"]


def test_events_filter_by_subject_id(app_client: TestClient) -> None:
    """The per-MAC embedded card on /ui/machines/{mac} drives this
    filter -- only events for the given MAC come back."""
    app_client.get("/pxe/aa:bb:cc:dd:ee:01")
    app_client.get("/pxe/aa:bb:cc:dd:ee:02")
    r = app_client.get(
        "/events",
        params={"subject_kind": "machine", "subject_id": "aa:bb:cc:dd:ee:01"},
        cookies=AUTH,
    )
    assert r.status_code == 200
    events = r.json()["events"]
    assert len(events) == 1
    assert events[0]["subject_id"] == "aa:bb:cc:dd:ee:01"


def test_ui_events_page_renders(app_client: TestClient) -> None:
    """The /ui/events page renders without 500-ing. Filter the view
    down to a kind that has no rows yet to exercise the empty-state
    'no events match' branch (auto-import emits ``image.hashed`` so
    the unfiltered list isn't empty)."""
    r = app_client.get("/ui/events", params={"kind": "machine.deleted"}, cookies=AUTH)
    assert r.status_code == 200
    body = r.text
    # Title + filter form land in the markup.
    assert "Event log" in body
    assert "/ui/events" in body
    # Empty-state alert.
    assert "No events match" in body


def test_ui_events_page_renders_filtered(app_client: TestClient) -> None:
    """A populated page shows the row + the kind badge."""
    app_client.get("/pxe/aa:bb:cc:dd:ee:ff")
    r = app_client.get(
        "/ui/events",
        params={"kind": "machine.discovered"},
        cookies=AUTH,
    )
    assert r.status_code == 200
    body = r.text
    assert "machine.discovered" in body
    assert "aa:bb:cc:dd:ee:ff" in body


def test_ui_events_page_image_subject_links_to_filter(app_client: TestClient) -> None:
    """Non-machine subjects (image / catalog / boot / settings)
    have no detail page, so the subject_id cell pivots into the
    timeline filtered by that subject. Regression-class: an
    earlier version rendered them as plain ``<code>`` text with
    no pivot, leaving operators with no way to see "everything
    that touched this image"."""
    # Auto-import seeds an image.hashed event with subject_kind=image
    # and subject_id="demo.qcow2" via the lifespan startup.
    r = app_client.get("/ui/events", params={"kind": "image.hashed"}, cookies=AUTH)
    assert r.status_code == 200
    body = r.text
    assert "demo.qcow2" in body
    # Pivot URL: subject_kind + subject_id both URL-encoded.
    # ``&amp;`` between params (HTML-compliant escape).
    assert "/ui/events?subject_kind=image&amp;subject_id=demo.qcow2" in body


def test_ui_events_page_footer_shows_filtered_when_filter_active(
    app_client: TestClient,
) -> None:
    """When any filter param is active the footer appends
    ``(filtered)`` so the operator can tell whether ``Showing N
    events`` is the full set or a slice. Important on long
    timelines where a small N is ambiguous without context."""
    # Trigger both an operator event and a pxe-client event so each
    # filtered slice has at least one row (the footer only renders
    # when ``events`` is truthy).
    app_client.get("/pxe/aa:bb:cc:dd:ee:f9")
    app_client.put(
        "/machines/aa:bb:cc:dd:ee:f9",
        json={"boot_policy": "local"},
        cookies=AUTH,
    )
    # Unfiltered view: no "(filtered)" suffix.
    r = app_client.get("/ui/events", cookies=AUTH)
    assert "Showing" in r.text
    assert "(filtered)" not in r.text
    # Filtered view: suffix appears.
    r = app_client.get("/ui/events", params={"actor": "operator"}, cookies=AUTH)
    body = r.text
    assert "Showing" in body
    assert "(filtered)" in body


def test_ui_events_page_renders_failure_with_danger_badge(app_client: TestClient) -> None:
    """Failure-kind events (anything ending ``.failed`` or
    ``_failed``) render with the ``bg-danger`` Bootstrap badge so
    they pop in a long log instead of blending in with their
    success siblings (``image.hashed`` vs ``image.hash_failed``,
    same family / different colour). Guards the
    failed-kind branch in the events / per-machine templates
    against a future refactor of the badge map."""
    # Trigger a settings.pxe.activate_failed event with a regex-
    # rejected interface name (deterministic, no helper needed).
    app_client.post(
        "/ui/settings/pxe-activate",
        data={"interface": "!!!", "subnet": "10.0.0.0/24"},
        cookies=AUTH,
        follow_redirects=False,
    )
    r = app_client.get(
        "/ui/events",
        params={"kind": "settings.pxe.activate_failed"},
        cookies=AUTH,
    )
    assert r.status_code == 200
    body = r.text
    assert "settings.pxe.activate_failed" in body
    # Danger badge appears in the rendered row.
    assert "bg-danger" in body


def test_ui_events_page_shows_source_ip_column(app_client: TestClient) -> None:
    """The ``Source IP`` column is in the table header and populated
    cells render as click-pivot links to ``/ui/events?source_ip=...``
    so the operator can drill into a single client's activity."""
    app_client.get("/pxe/aa:bb:cc:dd:ee:fc")
    r = app_client.get("/ui/events", cookies=AUTH)
    assert r.status_code == 200
    body = r.text
    # Column header.
    assert "Source IP" in body
    # Click-pivot link with the test client's host.
    assert "/ui/events?source_ip=testclient" in body


# ---------- /boot and /images file serving --------------------


def test_boot_artifact_serves_file(app_client: TestClient) -> None:
    r = app_client.get("/boot/bty-netboot-x86_64.vmlinuz")
    assert r.status_code == 200
    assert r.content == b"fake-kernel"


def test_boot_artifact_404_for_missing(app_client: TestClient) -> None:
    r = app_client.get("/boot/does-not-exist.bin")
    assert r.status_code == 404


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


def test_serve_image_returns_file_bytes(app_client: TestClient) -> None:
    r = app_client.get("/images/demo.qcow2")
    assert r.status_code == 200
    assert r.content == b"fake-image"


def test_serve_image_404_for_missing(app_client: TestClient) -> None:
    r = app_client.get("/images/does-not-exist.qcow2")
    assert r.status_code == 404


def test_serve_image_resolves_by_sha_dir_scan(tmp_path: Path) -> None:
    """``GET /images/<sha>`` resolves to the dir-scan file whose
    ``.sha256`` sidecar holds that digest. Without this, the
    server-side URLs the /images listing emits would 404 for
    every bty-tui --server flash."""
    import hashlib

    image_root = tmp_path / "images"
    image_root.mkdir()
    payload = b"fetch-by-sha-dir-scan"
    sha = hashlib.sha256(payload).hexdigest()
    (image_root / "demo.img").write_bytes(payload)
    (image_root / "demo.img.sha256").write_text(f"{sha}  demo.img\n")

    state = tmp_path / "state.db"
    app = create_app(
        state_path=state,
        service_user=TEST_SERVICE_USER,
        secret_key=TEST_SECRET_KEY,
        image_root=image_root,
    )
    with TestClient(app) as client:
        r = client.get(f"/images/{sha}")
        assert r.status_code == 200
        assert r.content == payload


def test_serve_image_resolves_by_sha_cache(tmp_path: Path) -> None:
    """``GET /images/<sha>`` resolves to the catalog cache when
    the SHA is present there (manifest blobs that were fetched)."""
    import hashlib

    image_root = tmp_path / "images"
    image_root.mkdir()
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    cache_dir = state_dir / "cache"
    cache_dir.mkdir()
    payload = b"fetch-by-sha-cache"
    sha = hashlib.sha256(payload).hexdigest()
    (cache_dir / sha).write_bytes(payload)

    state = state_dir / "state.db"
    import os

    os.environ["BTY_STATE_DIR"] = str(state_dir)
    try:
        app = create_app(
            state_path=state,
            service_user=TEST_SERVICE_USER,
            secret_key=TEST_SECRET_KEY,
            image_root=image_root,
        )
        with TestClient(app) as client:
            r = client.get(f"/images/{sha}")
            assert r.status_code == 200
            assert r.content == payload
    finally:
        os.environ.pop("BTY_STATE_DIR", None)


def test_serve_image_404_for_unknown_sha(app_client: TestClient) -> None:
    """A 64-hex-char key that doesn't match any cached or
    dir-scan SHA returns 404 cleanly (not a server error)."""
    r = app_client.get("/images/" + "0" * 64)
    assert r.status_code == 404


def test_serve_image_with_name_resolves_by_sha(tmp_path: Path) -> None:
    """``GET /images/<sha>/<filename>`` resolves by SHA; the
    ``<filename>`` is informational only -- it's there so URL-
    filename-extension format detection (used by
    ``bty.flash.probe_image_url`` and ``bty-flash-on-boot``)
    sees ``foo.img.zst`` rather than a bare 64-hex digest. This
    is what ``GET /images`` actually advertises now (was
    ``/images/<sha>`` flat, which 404'd format detection)."""
    import hashlib

    image_root = tmp_path / "images"
    image_root.mkdir()
    payload = b"sha-with-name"
    sha = hashlib.sha256(payload).hexdigest()
    (image_root / "demo.img").write_bytes(payload)
    (image_root / "demo.img.sha256").write_text(f"{sha}  demo.img\n")

    state = tmp_path / "state.db"
    app = create_app(
        state_path=state,
        service_user=TEST_SERVICE_USER,
        secret_key=TEST_SECRET_KEY,
        image_root=image_root,
    )
    with TestClient(app) as client:
        # Trailing decorative name -- server ignores it.
        r = client.get(f"/images/{sha}/whatever-filename.img.zst")
        assert r.status_code == 200
        assert r.content == payload
        # Same SHA, no trailing name -- still works (back-compat
        # for older clients that hit the bare-SHA URL).
        r2 = client.get(f"/images/{sha}")
        assert r2.status_code == 200
        assert r2.content == payload


# ---------- /catalog endpoints (M22) ---------------------------------------


def test_catalog_downloads_requires_auth(app_client: TestClient) -> None:
    r = app_client.get("/catalog/downloads")
    assert r.status_code == 401


def test_catalog_downloads_no_manifest_returns_empty(app_client: TestClient) -> None:
    """The fixture's app has no ``catalog.toml`` -- the endpoint
    returns ``{"manifest": null, "downloads": []}`` rather than
    404, so the UI's polling loop has something stable to render.
    """
    r = app_client.get("/catalog/downloads", cookies=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body == {"manifest": None, "downloads": []}


def test_catalog_downloads_post_without_manifest_404(app_client: TestClient) -> None:
    """POSTing an enqueue against a server without a manifest is a
    404 with a clear message -- the operator hasn't authored a
    catalog yet."""
    r = app_client.post(
        "/catalog/downloads",
        json={"name": "anything"},
        cookies=AUTH,
    )
    assert r.status_code == 404
    assert "no catalog manifest" in r.json()["detail"]


def test_catalog_hashes_requires_auth(app_client: TestClient) -> None:
    r = app_client.get("/catalog/hashes")
    assert r.status_code == 401


def test_catalog_hashes_listing_includes_max_parallel(
    app_client: TestClient,
) -> None:
    """``GET /catalog/hashes`` always returns ``image_root`` +
    ``max_parallel`` + ``hashes``. Lets the UI render the
    bty-web hash-pane caption without a separate config endpoint.
    """
    r = app_client.get("/catalog/hashes", cookies=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert "image_root" in body
    assert body["max_parallel"] >= 1
    assert isinstance(body["hashes"], list)


def test_catalog_hashes_post_unknown_file_404(app_client: TestClient) -> None:
    r = app_client.post(
        "/catalog/hashes",
        json={"name": "no-such-file.img"},
        cookies=AUTH,
    )
    assert r.status_code == 404
    assert "no image file" in r.json()["detail"]


def test_catalog_hashes_cancel_unknown_404(app_client: TestClient) -> None:
    r = app_client.delete("/catalog/hashes/never-was", cookies=AUTH)
    assert r.status_code == 404


# ---------- operator-curated catalog entries (M23) -------------------------


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
    assert body["sha256"] == sha
    assert body["src"] == "https://example.invalid/ubuntu-22.04.img.gz"
    assert body["name"] == "ubuntu-22.04.img.gz"
    assert body["format"] == "img.gz"
    assert body["size_bytes"] == 12345


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
    assert r.json()["sha256"] is None

    r2 = app_client.get("/images")
    rows = r2.json()
    by_name = {row["name"]: row for row in rows}
    assert "foo.img.gz" in by_name
    assert by_name["foo.img.gz"]["url"] == "https://example.invalid/foo.img.gz"
    assert by_name["foo.img.gz"]["ref"] is None  # sha256 unknown


def test_catalog_entries_add_rejects_non_https(app_client: TestClient) -> None:
    """``image_url`` / ``sha_url`` must be http(s); a typo with
    a different scheme should 422 at the Pydantic layer rather
    than land an unflashable entry."""
    r = app_client.post(
        "/catalog/entries",
        json={"image_url": "ftp://example.invalid/foo.img.gz"},
        cookies=AUTH,
    )
    assert r.status_code == 422


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

    r = app_client.get("/catalog/entries", cookies=AUTH)
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 1
    assert rows[0]["src"] == url

    r = app_client.delete("/catalog/entries", params={"src": url}, cookies=AUTH)
    assert r.status_code == 204

    r = app_client.get("/catalog/entries", cookies=AUTH)
    assert r.json() == []


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


# ---------- release-fetch manager (M24) ------------------------------------


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
    must land a ``boot.release.fetch_failed`` event in the audit
    log. Symmetric with the success path's ``boot.release.fetched``
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
        rows = list_events(conn, kind="boot.release.fetch_failed")
    assert len(rows) == 1
    row = rows[0]
    assert row.subject_kind == "boot"
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
    (empty host) and ``http:///path`` (host-less) must 422 at
    the Pydantic layer; the previous lax ``https?://.+`` would
    have accepted both."""
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


def test_catalog_enqueue_request_rejects_traversal_name(app_client: TestClient) -> None:
    """``CatalogEnqueueRequest.name`` (used by both
    ``POST /catalog/downloads`` and ``POST /catalog/hashes``)
    rejects path-traversal characters at the Pydantic layer.
    With the manager-side check shipped in v0.7.26, a bad name
    used to surface as a 500 from ``ValueError``; now both
    layers return a clean 422."""
    for bad in ("../etc/passwd", "foo/bar", "name\\with\\backslash", "with\0nul"):
        r = app_client.post(
            "/catalog/hashes",
            json={"name": bad},
            cookies=AUTH,
        )
        assert r.status_code == 422, f"expected 422 for {bad!r}, got {r.status_code}"
        r = app_client.post(
            "/catalog/downloads",
            json={"name": bad},
            cookies=AUTH,
        )
        assert r.status_code == 422, f"expected 422 for {bad!r}, got {r.status_code}"
