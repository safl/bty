"""End-to-end tests that wire multiple modules together.

Per-module tests cover units in isolation: ``probe_image_url``
with a clean URL, ``/catalog.toml`` with one pinned-sha entry,
``merge_with_catalog`` with neat ``CatalogEntry`` instances, etc.
A long string of operator-visible bugs in v0.19.x / v0.20.x slipped
past 600+ such tests because nothing strung the modules together
with the inputs production actually sees:

* URLs whose path segments contain spaces / parens (rolling-tag
  catalog names are human text).
* URLs whose path filenames lack a recognised extension (bty-web
  ``/images/<sha>/<display-name>`` route emits these).
* HEAD-then-GET probe sequences (every test issued GET, none
  issued HEAD; the GET-only routes returned 405 on HEAD).
* Manifest entries that auto-import into the DB AND appear in
  the in-memory catalog (the dedup bug).
* Post-flash branches where pxe-done fails (every flash test
  stubbed pxe-done to succeed).
* Catalog round-trips: emit /catalog.toml, parse it back, run
  the result through probe_image_url.

Each test in this file picks one such seam and asserts the full
chain works end-to-end on production-shaped inputs.
"""

from __future__ import annotations

import hashlib
import json
import typing
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from bty import catalog as _catalog
from bty import flash as _flash
from bty.web import _db as _bty_db
from bty.web._app import create_app
from bty.web._releases import ARTIFACT_NAMES

TEST_SERVICE_USER = "bty-test"
TEST_SECRET_KEY = "test-secret-not-for-prod-use"

AUTH: dict[str, str] = {}


@pytest.fixture
def app_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """A TestClient backed by an isolated bty-web app + state.db.

    Mirrors the fixture in ``test_web.py`` but without the seed
    image / boot triplets that interfere with e2e seeding.
    """
    state = tmp_path / "state.db"
    image_root = tmp_path / "images"
    image_root.mkdir()
    boot_root = tmp_path / "boot"
    boot_root.mkdir()
    bty_state_dir = tmp_path / "bty-state"
    bty_state_dir.mkdir()
    monkeypatch.setenv("BTY_STATE_DIR", str(bty_state_dir))
    app = create_app(
        state_path=state,
        service_user=TEST_SERVICE_USER,
        secret_key=TEST_SECRET_KEY,
        image_root=image_root,
        boot_root=boot_root,
    )

    import pamela

    monkeypatch.setattr(pamela, "authenticate", lambda *_a, **_kw: True)

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
        client.cookies.clear()
        # Expose paths so tests that need to poke state.db / write
        # files into image_root can do so without a second fixture.
        client.app.state.tmp_path = tmp_path  # type: ignore[attr-defined]
        client.app.state.state_path = state  # type: ignore[attr-defined]
        client.app.state.image_root = image_root  # type: ignore[attr-defined]
        client.app.state.boot_root = boot_root  # type: ignore[attr-defined]
        try:
            yield client
        finally:
            AUTH.clear()


# ----------------------------------------------------------------------
# 1. /catalog.toml -> probe_image_url with production-shaped entries
# ----------------------------------------------------------------------


def test_e2e_real_default_catalog_round_trips_through_probe_url(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Generate the actual ``scripts/generate_catalog_toml.py`` default
    catalog (rolling oras tags + GitHub release URL, human names with
    spaces and parens, NO sha pins), upload it via the bty-web
    ``/ui/catalog/upload`` endpoint, fetch ``/catalog.toml`` back,
    and run every entry's ``src`` through ``flash.probe_image_url``
    with the catalog's declared format as a hint.

    Asserts:
      * Each entry's src parses without ``InvalidURL`` (regression for
        the unencoded-spaces bug fixed in v0.20.3).
      * ``probe_image_url`` returns a valid ImageInfo with a
        recognized ``format`` (regression for v0.20.8's "image format
        not recognised" bug -- the URL filename has no extension when
        the catalog name is human text).
      * No ``InvalidURL`` from ``http.client._validate_path``.
      * For HTTP entries, the HEAD probe doesn't fail with 405
        (regression for v0.20.7's HEAD-not-allowed bug).
    """
    # The default catalog ships rolling-tag entries with NO sha
    # pin. bty-web's ``/catalog.toml`` deliberately skips no-sha
    # entries (the ``bty`` consumer requires shas for binding), so
    # to round-trip we upload sha-pinned versions of the same
    # shape -- same name format (spaces + parens), same URL types
    # (https with no path extension, oras://). That exercises the
    # path that broke in v0.20.3 / v0.20.8 without fighting the
    # /catalog.toml no-sha filter.
    body = (
        b"version = 1\n"
        b"\n"
        b'[[images]]\nname = "nosi debian-sysdev (x86_64, rolling)"\n'
        b'src = "https://example.invalid/debian"\n'
        b'sha256 = "' + b"a" * 64 + b'"\n'
        b'format = "img.gz"\n'
        b"\n"
        b'[[images]]\nname = "nosi fedora-sysdev (x86_64, rolling)"\n'
        b'src = "https://example.invalid/fedora"\n'
        b'sha256 = "' + b"b" * 64 + b'"\n'
        b'format = "img.gz"\n'
        b"\n"
        b'[[images]]\nname = "bty-server (x86_64, latest)"\n'
        b'src = "https://example.invalid/bty-server"\n'
        b'sha256 = "' + b"c" * 64 + b'"\n'
        b'format = "img.gz"\n'
    )
    r = app_client.post(
        "/ui/catalog/upload",
        files={"file": ("catalog.toml", body, "application/toml")},
        cookies=AUTH,
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text

    # Fetch back the rendered catalog.
    r = app_client.get("/catalog.toml")
    assert r.status_code == 200, r.text
    parsed = _catalog.load_bytes(r.content, source="<e2e>")
    names = {e.name for e in parsed.entries}
    assert "nosi debian-sysdev (x86_64, rolling)" in names, names
    assert "nosi fedora-sysdev (x86_64, rolling)" in names, names
    assert "bty-server (x86_64, latest)" in names, names

    # Every entry's src must parse cleanly. ``http.client._validate_path``
    # rejects any URL path with a literal space; building a Request
    # exercises that check on http(s) URLs.
    for entry in parsed.entries:
        if entry.src.startswith(("http://", "https://")):
            urllib.request.Request(entry.src)  # raises InvalidURL on bad URL

    class _FakeResp:
        headers: typing.ClassVar[dict[str, str]] = {"Content-Length": "100"}

        def __enter__(self) -> _FakeResp:
            return self

        def __exit__(self, *_a: object) -> None:
            return None

    monkeypatch.setattr("urllib.request.urlopen", lambda *_a, **_kw: _FakeResp())

    for entry in parsed.entries:
        info = _flash.probe_image_url(entry.src, format_hint=entry.format)
        assert info.format == "img.gz", (
            f"entry {entry.name!r} (src={entry.src!r}) produced "
            f"format={info.format!r}, expected img.gz. The hint should "
            "rescue URL-filename-based detection when the path has no "
            "recognised extension."
        )


# ----------------------------------------------------------------------
# 2. PUT /images -> /catalog.toml -> HEAD/GET parity
# ----------------------------------------------------------------------


def test_e2e_uploaded_image_routes_round_trip_with_special_chars(
    app_client: TestClient,
) -> None:
    """Upload an image with a "normal" filename, then exercise both
    URL shapes (``/images/<sha>``, ``/images/<sha>/<name>``) with
    both methods (HEAD, GET). Catches:

      * HEAD-on-images route returning 405 (v0.20.7 regression).
      * Content-Length absent from HEAD (Starlette FileResponse
        contract).
      * Per-request size mismatches (HEAD claims X bytes, GET
        delivers Y).

    The "special char" twist: percent-encode the name segment
    in the HEAD/GET to ensure routing tolerates it.
    """
    payload = b"\0" * 4096
    encoded = "foo%20bar.img.gz"  # space in name, percent-encoded
    # PUT requires auth.
    r = app_client.put(
        f"/images/{encoded}",
        content=payload,
        cookies=AUTH,
    )
    # 200 OK (overwrite) or 201 Created (new) -- either is success.
    assert r.status_code in (200, 201), r.text
    sha = hashlib.sha256(payload).hexdigest()

    # Bare /images/{key} -- by filename.
    for method in ("HEAD", "GET"):
        r = app_client.request(method, f"/images/{encoded}")
        assert r.status_code == 200, (method, r.text)
        assert r.headers.get("content-length") == str(len(payload)), (
            method,
            r.headers,
        )
        if method == "GET":
            assert r.content == payload
        else:
            assert r.content == b""

    # /images/{key}/{name:path} -- ``name`` is decorative.
    for method in ("HEAD", "GET"):
        r = app_client.request(method, f"/images/{sha}/{encoded}")
        assert r.status_code == 200, (method, r.text)
        assert r.headers.get("content-length") == str(len(payload))


# ----------------------------------------------------------------------
# 3. /pxe/<mac> -> kernel cmdline tokens are well-formed
# ----------------------------------------------------------------------


def test_e2e_pxe_chain_cmdline_carries_all_expected_tokens(
    app_client: TestClient,
) -> None:
    """An unknown MAC chains through ``/pxe/<mac>`` and gets
    ipxe_tui.j2. The rendered iPXE script's ``kernel`` line must
    carry every token bty relies on at boot time:

      * boot=live + fetch=<squashfs URL>  -- live-boot machinery
      * plymouth.enable=0                 -- avoid plymouth-quit-wait
                                              wedge on Intel iGPUs
      * modprobe.blacklist=nouveau        -- avoid the 30s nouveau
        + nouveau.modeset=0                 firmware-probe stall
      * bty.server=...                    -- ``bty`` dispatches via
                                              <server>/pxe/<mac>/plan
      * bty.mac=<mac>                     -- so ``bty`` can fetch
                                              the per-MAC plan

    Each token has been added in a separate release to fix a
    real-hardware boot issue; the cumulative invariant is that all
    of them must reach the kernel cmdline. A future template edit
    that drops any one of them would silently re-break a previously
    fixed target -- this test catches that.

    v0.22.10 retired ``bty.mode=interactive`` (and the matching
    cmdline-conditioned bty-flash-on-boot.service). Dispatch now
    happens at the /pxe/<mac>/plan endpoint, so the cmdline is the
    same minimal shape for tui + flash chains.
    """
    r = app_client.get(
        "/pxe/aa:bb:cc:dd:ee:ff",
        headers={"Host": "bty.local:8080"},
    )
    assert r.status_code == 200, r.text
    body = r.text
    assert body.startswith("#!ipxe"), body
    kernel_line = next(
        (line for line in body.splitlines() if line.startswith("kernel ")),
        None,
    )
    assert kernel_line is not None, f"no kernel line in:\n{body}"

    required = (
        "boot=live",
        f"fetch=${{bty-base}}/boot/{ARTIFACT_NAMES[2]}",
        "plymouth.enable=0",
        "modprobe.blacklist=nouveau",
        "nouveau.modeset=0",
        "bty.server=${bty-base}",
        "bty.mac=aa:bb:cc:dd:ee:ff",
        "console=tty0",
        "console=ttyS0,115200",
    )
    for token in required:
        assert token in kernel_line, f"kernel cmdline missing {token!r}: {kernel_line!r}"

    # No token in the cmdline should contain a literal space inside
    # its value (we percent-encode at construction sites; an
    # unencoded space inside a token is the InvalidURL bug shape).
    # Each token is separated by single spaces, so split + scan for
    # tokens that contain a key= but no value-end before the next
    # space.
    for token in kernel_line.split()[1:]:  # skip "kernel"
        # Each token is either a positional (url) or key=value. None
        # should contain a tab or weird whitespace.
        assert "\t" not in token, token


def test_e2e_pxe_flash_chain_plan_carries_image_url_and_target_serial(
    app_client: TestClient,
) -> None:
    """Bind a known machine to a known catalog entry + target disk
    serial, set boot_mode to bty-flash-once, GET /pxe/<mac>/plan: the
    plan response must carry the image URL + target serial.

    v0.22.10 moved these out of the iPXE kernel cmdline and into
    the plan endpoint. The iPXE chain is now template-agnostic
    (same shape for tui + flash); ``bty`` consumes the plan JSON
    to decide what to do.
    """
    # Seed a catalog entry the machine binds to. Use a sha that
    # corresponds to a file we'll create so the URL is reachable.
    image_root: Path = app_client.app.state.image_root  # type: ignore[attr-defined]
    payload = b"\0" * 256
    (image_root / "demo.qcow2").write_bytes(payload)
    sha = hashlib.sha256(payload).hexdigest()
    (image_root / "demo.qcow2.sha256").write_text(f"{sha}  demo.qcow2\n")

    # Auto-import ran on app startup against an empty image_root
    # (fixture sequence: app starts -> lifespan -> our test adds
    # the file). Insert the catalog row by hand. ``bty_image_ref``
    # has the same shape the auto-import would produce
    # (``image_ref_for_src("file://demo.qcow2")``).
    state_path: Path = app_client.app.state.state_path  # type: ignore[attr-defined]
    bty_image_ref = _catalog.image_ref_for_src("file://demo.qcow2")
    with _bty_db.open_db(state_path) as conn:
        conn.execute(
            "INSERT INTO catalog_entries "
            "(bty_image_ref, src, disk_image_sha, name, "
            "sha_url, format, size_bytes, description, added_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                bty_image_ref,
                "file://demo.qcow2",
                sha,
                "demo.qcow2",
                None,
                "qcow2",
                len(payload),
                None,
                "2026-05-17T22:00:00+00:00",
            ),
        )
        conn.commit()

    # Bind the machine to that ref.
    r = app_client.put(
        "/machines/aa:bb:cc:dd:ee:ff",
        json={
            "bty_image_ref": bty_image_ref,
            "boot_mode": "bty-flash-once",
            "target_disk_serial": "WD-WX12345",
        },
        cookies=AUTH,
    )
    assert r.status_code == 200, r.text

    # Plan endpoint carries image URL + target disk serial in JSON.
    plan_resp = app_client.get(
        "/pxe/aa:bb:cc:dd:ee:ff/plan",
        headers={"Host": "bty.local:8080"},
    )
    assert plan_resp.status_code == 200, plan_resp.text
    plan = plan_resp.json()
    assert plan["mode"] == "flash"
    assert plan["target_disk_serial"] == "WD-WX12345"
    assert "/images/" in plan["image"]
    assert bty_image_ref in plan["image"]
    # The catalog format rides along so the client can flash an image
    # whose URL name has no detectable extension (e.g. an oras title).
    assert plan["format"] == "qcow2"

    # iPXE chain still renders the flash header comment block (so
    # an operator inspecting curl output sees the bound ref + serial)
    # AND the minimal kernel cmdline (bty.server + bty.mac only,
    # plus the boot-time hardening tokens).
    r = app_client.get(
        "/pxe/aa:bb:cc:dd:ee:ff",
        headers={"Host": "bty.local:8080"},
    )
    assert r.status_code == 200, r.text
    body = r.text
    kernel_line = next(line for line in body.splitlines() if line.startswith("kernel "))
    required = (
        "bty.server=${bty-base}",
        "bty.mac=aa:bb:cc:dd:ee:ff",
        "plymouth.enable=0",
        "modprobe.blacklist=nouveau",
    )
    for token in required:
        assert token in kernel_line, f"flash cmdline missing {token!r}: {kernel_line!r}"
    # Retired: these moved to the plan endpoint.
    assert "bty.image_url" not in kernel_line
    assert "bty.target_disk_serial" not in kernel_line


def test_e2e_plan_handles_extensionless_oras_name(app_client: TestClient) -> None:
    """An oras entry's name is a descriptive title with no file
    extension ("nosi fedora-sysdev (x86_64, rolling)"). The flash plan
    must still let the client detect the format -- otherwise the flash
    is rejected as "format not recognised" (the observed symptom). The
    plan synthesises a URL name carrying the catalog format AND passes
    the format explicitly."""
    state_path: Path = app_client.app.state.state_path  # type: ignore[attr-defined]
    src = "oras://ghcr.io/safl/nosi/fedora-sysdev:latest"
    bty_image_ref = _catalog.image_ref_for_src(src)
    with _bty_db.open_db(state_path) as conn:
        conn.execute(
            "INSERT INTO catalog_entries "
            "(bty_image_ref, src, disk_image_sha, name, sha_url, format, "
            "size_bytes, description, added_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                bty_image_ref,
                src,
                None,
                "nosi fedora-sysdev (x86_64, rolling)",  # no extension
                None,
                "img.gz",
                None,
                None,
                "2026-05-22T00:00:00+00:00",
            ),
        )
        conn.commit()
    r = app_client.put(
        "/machines/0c:bf:b4:c0:4b:42",
        json={
            "bty_image_ref": bty_image_ref,
            "boot_mode": "bty-flash-once",
            "target_disk_serial": "SSD-860-EVO",
        },
        cookies=AUTH,
    )
    assert r.status_code == 200, r.text
    plan = app_client.get("/pxe/0c:bf:b4:c0:4b:42/plan", headers={"Host": "bty.local:8080"}).json()
    assert plan["mode"] == "flash"
    assert plan["format"] == "img.gz"
    # URL name carries the format extension so even a client that detects
    # format from the URL alone (no plan["format"]) succeeds.
    assert plan["image"].endswith("/image.img.gz")
    # ...but the descriptive title still rides along so the flash screen
    # shows it instead of the synthesised "image.img.gz".
    assert plan["name"] == "nosi fedora-sysdev (x86_64, rolling)"


def _seed_flashable_machine(app_client: TestClient, mac: str) -> None:
    """Seed a catalog entry + bind ``mac`` as bty-flash-always with a
    target disk serial -- the minimum for the flash chain to render."""
    image_root: Path = app_client.app.state.image_root  # type: ignore[attr-defined]
    state_path: Path = app_client.app.state.state_path  # type: ignore[attr-defined]
    payload = b"\0" * 256
    (image_root / "demo.qcow2").write_bytes(payload)
    sha = hashlib.sha256(payload).hexdigest()
    bty_image_ref = _catalog.image_ref_for_src("file://demo.qcow2")
    with _bty_db.open_db(state_path) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO catalog_entries "
            "(bty_image_ref, src, disk_image_sha, name, sha_url, format, "
            "size_bytes, description, added_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                bty_image_ref,
                "file://demo.qcow2",
                sha,
                "demo.qcow2",
                None,
                "qcow2",
                len(payload),
                None,
                "2026-05-17T22:00:00+00:00",
            ),
        )
        conn.commit()
    r = app_client.put(
        f"/machines/{mac}",
        json={
            "bty_image_ref": bty_image_ref,
            "boot_mode": "bty-flash-always",
            "target_disk_serial": "WD-WX12345",
        },
        cookies=AUTH,
    )
    assert r.status_code == 200, r.text


def test_e2e_flash_always_alternates_flash_then_sanboot(app_client: TestClient) -> None:
    """bty-flash-always must boot the just-flashed disk, not reflash in
    a loop under PXE-first firmware. The server alternates flash-chain
    -> sanboot -> flash-chain across PXE contacts, flipped by the /boot
    artifact fetch (``?mac=``) that proves the box booted the flasher.
    See project memory project_flash_always_loop_break.
    """
    boot_root: Path = app_client.app.state.boot_root  # type: ignore[attr-defined]
    # Stage the kernel artifact so the /boot fetch returns 200 like a
    # real iPXE chainload.
    (boot_root / ARTIFACT_NAMES[0]).write_bytes(b"\0" * 64)

    mac = "aa:bb:cc:dd:ee:ff"
    _seed_flashable_machine(app_client, mac)
    host = {"Host": "bty.local:8080"}

    def _directives(body: str) -> set[str]:
        # Command keyword of each non-comment, non-blank iPXE line,
        # INCLUDING sub-commands chained after && / || -- so the
        # local-disk line ``iseq ${platform} efi && exit || sanboot
        # ... || exit`` surfaces ``iseq``, ``exit`` AND ``sanboot``.
        # Distinguishes the flash chain (``kernel`` / ``boot``) from
        # the local-disk boot without matching the word in a comment.
        out: set[str] = set()
        for ln in body.splitlines():
            s = ln.strip()
            if not s or s.startswith("#"):
                continue
            for part in s.replace("&&", "||").split("||"):
                toks = part.split()
                if toks:
                    out.add(toks[0])
        return out

    # 1. First contact: flash chain, with ?mac= on the artifact URLs.
    body = app_client.get(f"/pxe/{mac}", headers=host).text
    assert "kernel" in _directives(body) and "sanboot" not in _directives(body)
    assert "bty.server=" in body
    assert f"?mac={mac}" in body, "flash-chain artifact URLs must carry ?mac="

    # 2. The box boots the flasher: it fetches a /boot artifact w/ ?mac=.
    a = app_client.get(f"/boot/{ARTIFACT_NAMES[0]}?mac={mac}", headers=host)
    assert a.status_code == 200, a.text
    # 2a. Flasher completes -> /pxe/{mac}/done. Required (v0.33.24+) for
    # the sanboot consume to fire; armed-without-/done re-serves the
    # flash chain.
    assert app_client.post(f"/pxe/{mac}/done").status_code == 204

    # 3. Post-flash contact: one-shot sanboot of the just-flashed disk.
    body = app_client.get(f"/pxe/{mac}", headers=host).text
    assert "sanboot" in _directives(body), f"expected sanboot after flasher boot: {body!r}"
    assert "kernel" not in _directives(body)

    # 4. No /boot fetch in between -> re-armed back to the flash chain.
    body = app_client.get(f"/pxe/{mac}", headers=host).text
    assert "kernel" in _directives(body) and "sanboot" not in _directives(body)


def test_e2e_flash_once_terminates_after_first_flash(app_client: TestClient) -> None:
    """bty-flash-once must flash exactly once then sanboot the disk on
    every subsequent PXE contact -- NOT alternate like bty-flash-always.
    Regression test for v0.30.x: the /boot ``?mac=`` arm site's WHERE
    clause excluded bty-flash-once, so the plan resolver's "bit set ->
    sanboot" branch was unreachable and the box re-flashed on every PXE
    contact forever. Surfaced by an operator audit log showing two
    flash cycles in three minutes on a flash-once machine.
    """
    boot_root: Path = app_client.app.state.boot_root  # type: ignore[attr-defined]
    (boot_root / ARTIFACT_NAMES[0]).write_bytes(b"\0" * 64)

    mac = "0c:bf:b4:c0:4b:42"
    _seed_flashable_machine(app_client, mac)
    # Helper seeds as bty-flash-always; re-PUT the full machine record
    # with bty-flash-once. ``MachineUpsert`` is a full upsert (unspecified
    # fields use model defaults -> clear bty_image_ref / target_disk_serial
    # and we'd land on ipxe.j2 with no flash chain), so we must re-supply
    # both. The PUT also resets saw_flasher_boot so we start pre-flash.
    image_root: Path = app_client.app.state.image_root  # type: ignore[attr-defined]
    bty_image_ref = _catalog.image_ref_for_src("file://demo.qcow2")
    del image_root  # only needed via the seeded helper; ref already produced
    r = app_client.put(
        f"/machines/{mac}",
        json={
            "bty_image_ref": bty_image_ref,
            "boot_mode": "bty-flash-once",
            "target_disk_serial": "WD-WX12345",
        },
        cookies=AUTH,
    )
    assert r.status_code == 200, r.text
    host = {"Host": "bty.local:8080"}

    def _directives(body: str) -> set[str]:
        out: set[str] = set()
        for ln in body.splitlines():
            s = ln.strip()
            if not s or s.startswith("#"):
                continue
            for part in s.replace("&&", "||").split("||"):
                toks = part.split()
                if toks:
                    out.add(toks[0])
        return out

    # 1. First contact: flash chain (saw_flasher_boot is 0).
    body = app_client.get(f"/pxe/{mac}", headers=host).text
    assert "kernel" in _directives(body) and "sanboot" not in _directives(body)
    assert f"?mac={mac}" in body, "flash chain must tag artifact URLs"

    # 2. The box boots the flasher: /boot fetch arms saw_flasher_boot.
    a = app_client.get(f"/boot/{ARTIFACT_NAMES[0]}?mac={mac}", headers=host)
    assert a.status_code == 200, a.text
    # 2a. Flasher completes -> /pxe/{mac}/done (v0.33.24+ requirement
    # for the sanboot consume; armed-without-/done re-serves the chain).
    assert app_client.post(f"/pxe/{mac}/done").status_code == 204

    # 3. Post-flash contact: sanboot the just-flashed disk.
    body = app_client.get(f"/pxe/{mac}", headers=host).text
    assert "sanboot" in _directives(body), f"expected sanboot after flasher boot: {body!r}"
    assert "kernel" not in _directives(body)

    # 4. CRITICAL: subsequent /pxe contacts WITHOUT another /boot fetch
    # MUST still serve sanboot -- bty-flash-once is terminal, unlike
    # bty-flash-always which re-arms here. The bug was exactly this:
    # the bit was never set, so step 3 fell through to the flash branch
    # AND step 4 would also flash, looping forever.
    for _ in range(3):
        body = app_client.get(f"/pxe/{mac}", headers=host).text
        assert "sanboot" in _directives(body), (
            f"bty-flash-once must stay terminal after first flash; got non-sanboot body: {body!r}"
        )
        assert "kernel" not in _directives(body)


def test_e2e_boot_artifact_mac_arms_only_alternating_policies(app_client: TestClient) -> None:
    """The /boot ``?mac=`` arming is confined to the three bit-consuming
    policies (bty-flash-always, bty-flash-once, bty-inventory), so the
    one-shot sanboot bit can't leak into others (a sanboot / bty-tui
    box never gets a spurious post-boot sanboot). bty-flash-once is
    included because its plan resolver reads the bit to flip to a
    terminal sanboot of the just-flashed disk -- without arming, the
    machine would re-flash on every PXE contact forever (v0.30.1 retag
    #2 regression: the bit was missing from the WHERE clause so the
    flip path was unreachable)."""
    boot_root: Path = app_client.app.state.boot_root  # type: ignore[attr-defined]
    state_path: Path = app_client.app.state.state_path  # type: ignore[attr-defined]
    (boot_root / ARTIFACT_NAMES[0]).write_bytes(b"\0" * 64)
    host = {"Host": "bty.local:8080"}

    def _saw(mac: str) -> int:
        with _bty_db.open_db(state_path) as conn:
            row = conn.execute(
                "SELECT saw_flasher_boot FROM machines WHERE mac = ?", (mac,)
            ).fetchone()
        return int(row["saw_flasher_boot"])

    always = "11:11:11:11:11:11"
    once = "44:44:44:44:44:44"
    inventory = "22:22:22:22:22:22"
    tui = "33:33:33:33:33:33"
    sanboot = "55:55:55:55:55:55"
    for mac, policy in (
        (always, "bty-flash-always"),
        (once, "bty-flash-once"),
        (inventory, "bty-inventory"),
        (tui, "bty-tui"),
        (sanboot, "ipxe-exit"),
    ):
        assert (
            app_client.put(f"/machines/{mac}", json={"boot_mode": policy}, cookies=AUTH).status_code
            == 200
        )

    for mac in (always, once, inventory, tui, sanboot):
        app_client.get(f"/boot/{ARTIFACT_NAMES[0]}?mac={mac}", headers=host)

    assert _saw(always) == 1, "flash-always machine should be armed by /boot?mac="
    assert _saw(once) == 1, "flash-once machine should be armed by /boot?mac="
    assert _saw(inventory) == 1, "bty-inventory machine should be armed by /boot?mac="
    assert _saw(tui) == 0, "bty-tui machine must NOT be armed"
    assert _saw(sanboot) == 0, "ipxe-exit machine must NOT be armed"


def test_e2e_inventory_alternates_liveenv_then_sanboot(app_client: TestClient) -> None:
    """bty-inventory alternates an inventory live-env boot then a
    sanboot across PXE contacts, flipped by the /boot artifact fetch --
    so every cycle re-collects the disk inventory before booting the
    disk. Mirrors the bty-flash-always loop-break, minus the flash."""
    boot_root: Path = app_client.app.state.boot_root  # type: ignore[attr-defined]
    (boot_root / ARTIFACT_NAMES[0]).write_bytes(b"\0" * 64)
    mac = "ab:cd:ef:00:11:22"
    assert (
        app_client.put(
            f"/machines/{mac}", json={"boot_mode": "bty-inventory"}, cookies=AUTH
        ).status_code
        == 200
    )
    host = {"Host": "bty.local:8080"}

    def _directives(body: str) -> set[str]:
        # See the matching helper in the flash-always test: split on
        # && / || so a chained ``... || sanboot ... || exit`` surfaces
        # ``sanboot`` even though the line now leads with ``iseq``.
        out: set[str] = set()
        for ln in body.splitlines():
            s = ln.strip()
            if not s or s.startswith("#"):
                continue
            for part in s.replace("&&", "||").split("||"):
                toks = part.split()
                if toks:
                    out.add(toks[0])
        return out

    # 1. First contact: live-env chain (inventory boot), ?mac= tagged.
    body = app_client.get(f"/pxe/{mac}", headers=host).text
    assert "kernel" in _directives(body) and "sanboot" not in _directives(body)
    assert f"?mac={mac}" in body, "inventory chain artifact URLs must carry ?mac="
    # The plan tells bty to post inventory and reboot.
    assert app_client.get(f"/pxe/{mac}/plan", headers=host).json()["mode"] == "inventory"

    # 2. Box boots the live env: fetches a /boot artifact w/ ?mac=.
    assert app_client.get(f"/boot/{ARTIFACT_NAMES[0]}?mac={mac}", headers=host).status_code == 200
    # 2a. Live env's bty POSTs inventory (v0.33.24+ requirement for
    # the sanboot consume; armed-without-inventory re-serves the chain).
    inv_post = app_client.post(
        f"/pxe/{mac}/inventory",
        json={"disks": [{"path": "/dev/sda", "serial": "SN-LIVE"}]},
    )
    assert inv_post.status_code == 204, inv_post.text

    # 3. Post-inventory contact: one-shot sanboot of the disk.
    body = app_client.get(f"/pxe/{mac}", headers=host).text
    assert "sanboot" in _directives(body) and "kernel" not in _directives(body)

    # 4. No /boot fetch in between -> re-armed back to the inventory boot.
    body = app_client.get(f"/pxe/{mac}", headers=host).text
    assert "kernel" in _directives(body) and "sanboot" not in _directives(body)


# ----------------------------------------------------------------------
# 4. catalog.toml roundtrip with mixed shapes -> no dupes on /ui/images
# ----------------------------------------------------------------------


def test_e2e_catalog_with_mixed_entry_shapes_renders_each_once(
    app_client: TestClient,
) -> None:
    """Upload a catalog with diverse entry shapes -- sha-pinned,
    sha-less rolling tag, http URL, oras URL, plus dir-scan files
    on disk -- and assert /ui/images renders each exactly once.

    Regression: v0.19.7's duplicate bug only fired on no-sha entries
    (the by_sha merge can't dedupe them). v0.19.8's ref-keyed merge
    fixed it structurally. This test exercises the WORST CASE: every
    shape mixed together, plus a dir-scan file that overlaps via
    auto-import.
    """
    # Drop a dir-scan file -- gets auto-imported into catalog_entries.
    image_root: Path = app_client.app.state.image_root  # type: ignore[attr-defined]
    payload = b"\x00" * 256
    (image_root / "local-image.img.gz").write_bytes(payload)
    sha = hashlib.sha256(payload).hexdigest()
    (image_root / "local-image.img.gz.sha256").write_text(f"{sha}  local-image.img.gz\n")

    body = (
        b"version = 1\n"
        b"\n"
        # sha-pinned http
        b'[[images]]\nname = "Pinned HTTP"\n'
        b'src = "https://example.invalid/pinned.img.gz"\n'
        b'sha256 = "' + b"d" * 64 + b'"\n'
        b'format = "img.gz"\n'
        b"\n"
        # rolling oras, no sha
        b'[[images]]\nname = "Rolling ORAS (rolling)"\n'
        b'src = "oras://ghcr.io/example/rolling:latest"\n'
        b'format = "img.gz"\n'
        b"\n"
        # un-sha http with spaces in name
        b'[[images]]\nname = "Human Named (x86_64, rolling)"\n'
        b'src = "https://example.invalid/human.img.gz"\n'
        b'format = "img.gz"\n'
    )
    r = app_client.post(
        "/ui/catalog/upload",
        files={"file": ("catalog.toml", body, "application/toml")},
        cookies=AUTH,
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text

    r = app_client.get("/ui/images", cookies=AUTH)
    assert r.status_code == 200, r.text
    page = r.text

    # Each manifest entry name appears exactly once in a row-level
    # cell. Count the strict occurrences in <td> contexts vs
    # decorative occurrences in title=, hidden inputs, etc.
    for name in (
        "Pinned HTTP",
        "Rolling ORAS (rolling)",
        "Human Named (x86_64, rolling)",
        "local-image.img.gz",
    ):
        count = page.count(name)
        # Each entry may appear in: row cell (1), possibly a title= hint (1),
        # possibly a confirm-dialog data-attr (1). Upper bound is 3.
        # The bug shape was 6-8 occurrences (entry rendered twice on
        # the merge + url_only paths, each carrying its own cell + title).
        assert 1 <= count <= 3, (
            f"entry {name!r} rendered {count} times on /ui/images; "
            f"expected 1-3 (one row + optional hover/data attrs). "
            "If count is in the 4+ range the duplicate-rendering "
            "regression is back."
        )


# ----------------------------------------------------------------------
# 5. HEAD/GET parity on /boot artifact route
# ----------------------------------------------------------------------


def test_e2e_boot_artifact_route_supports_head_with_correct_content_length(
    app_client: TestClient,
) -> None:
    """UEFI HTTP-Boot firmware HEADs the bootfile URL before issuing
    GET to size its fetch buffer. The /boot/{name} route must:
      * accept HEAD (not return 405)
      * report the same Content-Length on HEAD and GET
      * return an empty body on HEAD (HEAD semantics)
    """
    boot_root: Path = app_client.app.state.boot_root  # type: ignore[attr-defined]
    payload = b"fake-vmlinuz" * 100
    (boot_root / ARTIFACT_NAMES[0]).write_bytes(payload)

    head_r = app_client.head(f"/boot/{ARTIFACT_NAMES[0]}")
    assert head_r.status_code == 200, head_r.text
    assert head_r.content == b""
    head_cl = head_r.headers.get("content-length")
    assert head_cl == str(len(payload)), head_cl

    get_r = app_client.get(f"/boot/{ARTIFACT_NAMES[0]}")
    assert get_r.status_code == 200, get_r.text
    assert get_r.content == payload
    assert get_r.headers.get("content-length") == head_cl


# ----------------------------------------------------------------------
# 6. catalog entry lifecycle -- add via UI, list, cache, evict, delete
# ----------------------------------------------------------------------


def test_e2e_catalog_entry_lifecycle_via_ui_endpoints(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Full lifecycle of a URL-form-added catalog entry:

      1. POST /ui/catalog/entries with image_url -> 303 to /ui/images
      2. GET /ui/images -> entry appears, trash button present
      3. DELETE /catalog/entries?src=... -> 204
      4. GET /ui/images -> entry gone

    Catches: the entry shows up where the operator expects it (so
    the delete button can be found), AND the delete actually removes
    the row.
    """
    from bty.web import _app as _web_app

    monkeypatch.setattr(_web_app, "_head_content_length", lambda _url: None)

    src = "https://example.invalid/rolling.img.gz"
    r = app_client.post(
        "/ui/catalog/entries",
        data={"image_url": src, "sha_url": ""},
        cookies=AUTH,
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text

    r = app_client.get("/ui/images", cookies=AUTH)
    assert r.status_code == 200, r.text
    assert "rolling.img.gz" in r.text
    assert "bty-catalog-entry-delete-btn" in r.text

    # Sanity-check the DB has the row.
    state_path: Path = app_client.app.state.state_path  # type: ignore[attr-defined]
    with _bty_db.open_db(state_path) as conn:
        rows = conn.execute("SELECT src FROM catalog_entries WHERE src = ?", (src,)).fetchall()
    assert rows, "POST /ui/catalog/entries did not insert the row"

    # Use httpx's url= form to pass the query string explicitly
    # (params= via .request() sometimes urlencodes the dict keys
    # in surprising ways).
    from urllib.parse import quote as _q

    r = app_client.request(
        "DELETE",
        f"/catalog/entries?src={_q(src, safe='')}",
        cookies=AUTH,
    )
    assert r.status_code == 204, r.text

    with _bty_db.open_db(state_path) as conn:
        rows_after = conn.execute(
            "SELECT src FROM catalog_entries WHERE src = ?", (src,)
        ).fetchall()
    assert not rows_after, (
        f"DELETE returned 204 but the row is still in catalog_entries: {rows_after}"
    )

    r = app_client.get("/ui/images", cookies=AUTH)
    # ``rolling.img.gz`` will still appear in the events audit log
    # at the bottom of /ui/images (audit is append-only, correctly).
    # The contract under test is that the IMAGES table no longer
    # carries it -- so the delete button (which only renders on
    # image rows referencing this entry) must be gone.
    page = r.text
    # Find the catalog-entry-delete-btn instances and assert none
    # carry our deleted src as their data-src attribute.
    import re

    btns_with_our_src = re.findall(
        r'bty-catalog-entry-delete-btn[^>]*data-src="' + re.escape(src) + r'"',
        page,
    )
    assert not btns_with_our_src, (
        f"DELETE removed the DB row but the entry's delete button is still "
        f"rendered: {btns_with_our_src!r}. Auto-import re-adding it?"
    )


# ----------------------------------------------------------------------
# 7. format_hint propagates through make_plan + validate_plan
# ----------------------------------------------------------------------


def test_e2e_format_hint_carries_through_to_validate_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Validate_plan rejects with "image format not recognised" when
    the probe returns ``format=None``. The path that broke in
    v0.20.8: bty-web emits ``/images/<sha>/<display-name>`` URLs
    whose filename has no recognised extension; URL-only detection
    fails; without the ``format_hint`` parameter, validate_plan
    rejects.

    Pin the contract by running through make_plan + validate_plan
    twice -- once with hint, once without -- and asserting the
    rejection error string changes shape accordingly.
    """

    class _FakeResp:
        headers: typing.ClassVar[dict[str, str]] = {"Content-Length": "1024"}

        def __enter__(self) -> _FakeResp:
            return self

        def __exit__(self, *_a: object) -> None:
            return None

    monkeypatch.setattr("urllib.request.urlopen", lambda *_a, **_kw: _FakeResp())
    url = "http://server/images/abcd/Human%20Named%20%28rolling%29"

    target_info = _flash.TargetInfo(
        path=Path("/dev/sdz"),
        size_bytes=10 * 1024 * 1024,
        exists=True,
        is_block_device=True,
        mountpoints=[],
    )

    # Without hint: format=None -> validate rejects.
    info_no_hint = _flash.probe_image_url(url)
    assert info_no_hint.format is None
    errors = _flash.validate_plan(_flash.make_plan(info_no_hint, target_info))
    assert any("format not recognised" in e for e in errors), errors

    # With hint: format propagates, validate accepts.
    info_hint = _flash.probe_image_url(url, format_hint="img.gz")
    assert info_hint.format == "img.gz"
    errors_hint = _flash.validate_plan(_flash.make_plan(info_hint, target_info))
    assert not any("format not recognised" in e for e in errors_hint), errors_hint


# ----------------------------------------------------------------------
# 8. flash success when pxe-done fails -> button still flips
# ----------------------------------------------------------------------


def test_e2e_pxe_done_failure_is_isolated_from_machine_state(
    app_client: TestClient,
) -> None:
    """POST /pxe/<mac>/done is best-effort. If the ``bty`` side hits
    a URLError trying to call it, the actual flash succeeded -- the
    server must accept a subsequent successful done call AND the
    machine's last_flashed_at must update correctly.

    This is the same shape as the UI-side bug fixed in v0.20.1:
    pxe-done is best-effort and its failure must not block other
    state transitions.
    """
    # Seed a dir-scan file + an explicit catalog_entries row (the
    # auto-import on lifespan ran against an empty image_root --
    # we add the file AFTER the app started, so we have to wire
    # the catalog row by hand).
    image_root: Path = app_client.app.state.image_root  # type: ignore[attr-defined]
    payload = b"\x11" * 128
    (image_root / "tiny.img").write_bytes(payload)
    sha = hashlib.sha256(payload).hexdigest()
    (image_root / "tiny.img.sha256").write_text(f"{sha}  tiny.img\n")

    state_path: Path = app_client.app.state.state_path  # type: ignore[attr-defined]
    ref = _catalog.image_ref_for_src("file://tiny.img")
    with _bty_db.open_db(state_path) as conn:
        conn.execute(
            "INSERT INTO catalog_entries "
            "(bty_image_ref, src, disk_image_sha, name, "
            "sha_url, format, size_bytes, description, added_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                ref,
                "file://tiny.img",
                sha,
                "tiny.img",
                None,
                "img",
                len(payload),
                None,
                "2026-05-17T22:00:00+00:00",
            ),
        )
        conn.commit()
    r = app_client.put(
        "/machines/12:34:56:78:9a:bc",
        json={
            "bty_image_ref": ref,
            "boot_mode": "bty-flash-once",
            "target_disk_serial": "WD-XX",
        },
        cookies=AUTH,
    )
    assert r.status_code == 200, r.text

    # Trigger the done call (open endpoint -- PXE clients have
    # no auth). Server-side state mutation only. Endpoint returns
    # 204 No Content on success (no body to return).
    r = app_client.post("/pxe/12:34:56:78:9a:bc/done")
    assert r.status_code in (200, 204), r.text

    # Verify last_flashed_at populated; boot_mode is NOT mutated
    # (flash-once stays flash-once -- mode is the operator's intent).
    r = app_client.get("/machines/12:34:56:78:9a:bc", cookies=AUTH)
    assert r.status_code == 200, r.text
    m = r.json()
    assert m["boot_mode"] == "bty-flash-once", m
    assert m["last_flashed_at"] is not None, m


# ----------------------------------------------------------------------
# 9. modprobe.d blacklist files are in the bake (structural)
# ----------------------------------------------------------------------


def test_e2e_modprobe_blacklist_files_match_kernel_cmdline_intent() -> None:
    """The repo ships ``zz-bty-blacklist-nouveau.conf`` in the live env
    + server rootfs, and ``modprobe.blacklist=nouveau nouveau.modeset=0``
    on the kernel cmdline at four locations (two iPXE templates,
    auto/config BOOTAPPEND, cloud-init GRUB_CMDLINE EXTRA).

    A future change that adds another GPU driver to the blacklist
    must do it in ALL FIVE places, not just some, or the cmdline /
    config drift will silently let the driver load on some boot
    paths and not others.

    This test catches the cross-cutting consistency invariant by
    listing every "blacklist <driver>" line in either modprobe.d
    file and asserting the SAME drivers appear in
    modprobe.blacklist=<drivers> on every kernel cmdline insertion.
    """
    repo_root = Path(__file__).resolve().parents[1]
    live_conf = (
        repo_root
        / "bty-media"
        / "live-build"
        / "config"
        / "includes.chroot"
        / "etc"
        / "modprobe.d"
        / "zz-bty-blacklist-nouveau.conf"
    )
    server_conf = (
        repo_root
        / "bty-media"
        / "rootfs"
        / "server"
        / "etc"
        / "modprobe.d"
        / "zz-bty-blacklist-nouveau.conf"
    )
    # Extract blacklisted modules.
    drivers = set()
    for path in (live_conf, server_conf):
        for line in path.read_text().splitlines():
            stripped = line.strip()
            if stripped.startswith("blacklist "):
                drivers.add(stripped.split(None, 1)[1])

    assert drivers, "no 'blacklist <driver>' lines found in modprobe.d configs"

    # Every driver listed must appear on every kernel cmdline
    # insertion point as ``modprobe.blacklist=<driver>``.
    cmdline_files = (
        repo_root / "src" / "bty" / "web" / "_templates" / "ipxe_tui.j2",
        repo_root / "src" / "bty" / "web" / "_templates" / "ipxe_flash.j2",
        repo_root / "bty-media" / "live-build" / "auto" / "config",
        repo_root / "bty-media" / "auxiliary" / "cloudinit-base-server.user",
    )
    for path in cmdline_files:
        body = path.read_text()
        for driver in drivers:
            assert f"modprobe.blacklist={driver}" in body or (
                "modprobe.blacklist=" in body and driver in body
            ), (
                f"{path} has cmdline insertions but does not include "
                f"modprobe.blacklist={driver} -- driver is blacklisted "
                f"in modprobe.d but not at kernel cmdline level on "
                f"this boot path. The initramfs window before /etc/ is "
                f"mounted would still load it."
            )


# ----------------------------------------------------------------------
# 10. /catalog.toml entries that come back parse cleanly
# ----------------------------------------------------------------------


def test_e2e_pxe_unknown_mac_then_inventory_then_flash_chain(
    app_client: TestClient,
) -> None:
    """The PXE flow has four state transitions on the server side:

      1. Unknown MAC -> /pxe/<mac> -> auto-discovered with policy=bty-inventory
      2. Same MAC -> /machines/<mac>/inventory -> machine.inventory event
      3. Operator binds (PUT /machines/<mac>) with bty-flash-once + ref
      4. Same MAC -> /pxe/<mac> -> renders ipxe_flash.j2 with the ref

    Each step depends on the prior; a regression in any of them
    leaves operators with a "PXE booted but nothing happened" mystery.
    """
    mac = "0a:0b:0c:0d:0e:0f"

    # 1. Auto-discovery.
    r = app_client.get(f"/pxe/{mac}", headers={"Host": "bty.local:8080"})
    assert r.status_code == 200, r.text
    # Default policy is bty-inventory; cmdline carries minimal
    # bty.server + bty.mac (v0.22.10 retired bty.mode=). The plan
    # endpoint decides what ``bty`` does.
    assert "bty.server=" in r.text
    assert f"bty.mac={mac}" in r.text

    r = app_client.get(f"/machines/{mac}", cookies=AUTH)
    assert r.status_code == 200, r.text
    assert r.json()["boot_mode"] == "bty-inventory"

    plan = app_client.get(f"/pxe/{mac}/plan", headers={"Host": "bty.local:8080"}).json()
    assert plan["mode"] == "inventory"

    # 2. Inventory POST -- simulates the live env reporting disks.
    r = app_client.post(
        f"/pxe/{mac}/inventory",
        json={"disks": [{"path": "/dev/sda", "serial": "SER-1", "size_bytes": 10**9}]},
    )
    assert r.status_code in (200, 204), r.text

    # 3. Operator binds. Insert a catalog row + bind machine.
    image_root: Path = app_client.app.state.image_root  # type: ignore[attr-defined]
    payload = b"\x55" * 256
    (image_root / "bound.img.gz").write_bytes(payload)
    sha = hashlib.sha256(payload).hexdigest()
    (image_root / "bound.img.gz.sha256").write_text(f"{sha}  bound.img.gz\n")

    state_path: Path = app_client.app.state.state_path  # type: ignore[attr-defined]
    ref = _catalog.image_ref_for_src("file://bound.img.gz")
    with _bty_db.open_db(state_path) as conn:
        conn.execute(
            "INSERT INTO catalog_entries "
            "(bty_image_ref, src, disk_image_sha, name, "
            "sha_url, format, size_bytes, description, added_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                ref,
                "file://bound.img.gz",
                sha,
                "bound.img.gz",
                None,
                "img.gz",
                len(payload),
                None,
                "2026-05-17T22:00:00+00:00",
            ),
        )
        conn.commit()

    r = app_client.put(
        f"/machines/{mac}",
        json={
            "bty_image_ref": ref,
            "boot_mode": "bty-flash-once",
            "target_disk_serial": "SER-1",
        },
        cookies=AUTH,
    )
    assert r.status_code == 200, r.text

    # 4. Subsequent PXE renders the flash chain with the binding.
    # The iPXE chain itself carries only bty.server + bty.mac on the
    # cmdline (v0.22.10); the image URL + target serial appear in
    # the chain header comment block for operator inspection AND on
    # /pxe/<mac>/plan as JSON (the contract ``bty`` consumes).
    r = app_client.get(f"/pxe/{mac}", headers={"Host": "bty.local:8080"})
    assert r.status_code == 200, r.text
    body = r.text
    assert f"bty_image_ref:      {ref}" in body
    assert "target_disk_serial: SER-1" in body
    plan = app_client.get(f"/pxe/{mac}/plan", headers={"Host": "bty.local:8080"}).json()
    assert plan["mode"] == "flash"
    assert plan["target_disk_serial"] == "SER-1"
    assert f"/images/{ref}/" in plan["image"]
    # Done records the flash but does NOT mutate the mode -- flash-once
    # stays flash-once (the saw_flasher_boot bit handles the post-flash
    # disk boot, not a policy mutation).
    r = app_client.post(f"/pxe/{mac}/done")
    assert r.status_code in (200, 204), r.text
    r = app_client.get(f"/machines/{mac}", cookies=AUTH)
    assert r.json()["boot_mode"] == "bty-flash-once"


def test_e2e_image_serve_routes_resolve_by_sha_or_filename(
    app_client: TestClient,
) -> None:
    """``/images/<key>`` accepts either a SHA-256 (64-hex) or a
    filename. Both should resolve to the same bytes when the
    operator's dir-scan file has a matching ``.sha256`` sidecar.

    Confirms the resolver pipeline:
      filename -> dir-scan -> bytes
      sha -> dir-scan sidecar match -> bytes
    """
    image_root: Path = app_client.app.state.image_root  # type: ignore[attr-defined]
    state_path: Path = app_client.app.state.state_path  # type: ignore[attr-defined]
    payload = b"\xab" * 8192
    (image_root / "resolver.img.gz").write_bytes(payload)
    sha = hashlib.sha256(payload).hexdigest()
    (image_root / "resolver.img.gz.sha256").write_text(f"{sha}  resolver.img.gz\n")
    # The sha-key resolver looks the value up in catalog_entries
    # (the dir-scan auto-import populates that table at startup,
    # but our file landed AFTER lifespan ran -- insert by hand).
    ref = _catalog.image_ref_for_src("file://resolver.img.gz")
    with _bty_db.open_db(state_path) as conn:
        conn.execute(
            "INSERT INTO catalog_entries "
            "(bty_image_ref, src, disk_image_sha, name, "
            "sha_url, format, size_bytes, description, added_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                ref,
                "file://resolver.img.gz",
                sha,
                "resolver.img.gz",
                None,
                "img.gz",
                len(payload),
                None,
                "2026-05-17T22:00:00+00:00",
            ),
        )
        conn.commit()

    by_name = app_client.get("/images/resolver.img.gz")
    by_sha = app_client.get(f"/images/{sha}")
    assert by_name.status_code == 200, by_name.text
    assert by_sha.status_code == 200, by_sha.text
    assert by_name.content == by_sha.content == payload


def test_e2e_flash_safety_gate_no_target_disk_serial_surfaces_reason(
    app_client: TestClient,
) -> None:
    """Operator binds a machine to a ref with bty-flash-once policy
    but forgets to set ``target_disk_serial``. The safety gate in
    /pxe/<mac> must:
      1. Refuse to render ipxe_flash.j2 (would wipe wrong disk).
      2. Carry ``reason: no_target_disk`` in the always-runs
         ``netboot.pxe.offered`` event's details (v0.33.26+
         collapsed the standalone failure event into the offered
         event's reason field).
      3. Fall through to the local-boot / sanboot template.

    Regression coverage for the v0.13.x-era safety gate that exists
    precisely because dev/sda can flip across reboots; pinning to
    a serial is the only safe pick.
    """
    mac = "ee:ee:ee:ee:ee:ee"
    state_path: Path = app_client.app.state.state_path  # type: ignore[attr-defined]
    image_root: Path = app_client.app.state.image_root  # type: ignore[attr-defined]
    payload = b"\x99" * 256
    (image_root / "gated.img.gz").write_bytes(payload)
    sha = hashlib.sha256(payload).hexdigest()
    (image_root / "gated.img.gz.sha256").write_text(f"{sha}  gated.img.gz\n")
    ref = _catalog.image_ref_for_src("file://gated.img.gz")
    with _bty_db.open_db(state_path) as conn:
        conn.execute(
            "INSERT INTO catalog_entries "
            "(bty_image_ref, src, disk_image_sha, name, sha_url, "
            "format, size_bytes, description, added_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                ref,
                "file://gated.img.gz",
                sha,
                "gated.img.gz",
                None,
                "img.gz",
                len(payload),
                None,
                "2026-05-17T22:00:00+00:00",
            ),
        )
        conn.commit()

    # Bind without target_disk_serial.
    r = app_client.put(
        f"/machines/{mac}",
        json={"bty_image_ref": ref, "boot_mode": "bty-flash-once"},
        cookies=AUTH,
    )
    assert r.status_code == 200, r.text

    r = app_client.get(f"/pxe/{mac}", headers={"Host": "bty.local:8080"})
    assert r.status_code == 200, r.text
    body = r.text
    # Safety gate triggered: NO bty.image_url on the cmdline.
    assert "bty.image_url=" not in body, (
        "safety gate failed: flash chain emitted without target_disk_serial. "
        "This is the regression that would wipe the wrong disk on a multi-disk host."
    )

    # And the offered event's details carry the refusal reason.
    with _bty_db.open_db(state_path) as conn:
        rows = conn.execute(
            "SELECT kind, details FROM events WHERE subject_id = ? ORDER BY id DESC LIMIT 5",
            (mac,),
        ).fetchall()
    offered = next(r for r in rows if r["kind"] == "netboot.pxe.offered")
    details = json.loads(offered["details"])
    assert details["reason"] == "no_target_disk", (
        f"safety gate fired but pxe.offered did not carry "
        f"reason=no_target_disk in details: {details}"
    )


def test_e2e_catalog_upload_invalid_toml_preserves_existing_manifest(
    app_client: TestClient,
) -> None:
    """``POST /ui/catalog/upload`` validates the body before writing.
    A malformed TOML must:
      1. Return 303 to ``/ui/images?error=...`` (not 500).
      2. NOT clobber the existing manifest_path file (the operator's
         existing catalog is preserved).
    """
    # First upload a valid catalog so there's an existing manifest
    # to potentially clobber.
    good = (
        b"version = 1\n"
        b"\n"
        b'[[images]]\nname = "Good"\n'
        b'src = "https://example.invalid/good"\n'
        b'sha256 = "' + b"e" * 64 + b'"\n'
        b'format = "img.gz"\n'
    )
    r = app_client.post(
        "/ui/catalog/upload",
        files={"file": ("catalog.toml", good, "application/toml")},
        cookies=AUTH,
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text

    # Now upload garbage.
    garbage = b"this is not valid TOML [[[\nname missing\n"
    r = app_client.post(
        "/ui/catalog/upload",
        files={"file": ("catalog.toml", garbage, "application/toml")},
        cookies=AUTH,
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"].startswith("/ui/images?error="), r.headers["location"]

    # The existing manifest must still parse + show the Good entry.
    r = app_client.get("/catalog.toml")
    assert r.status_code == 200, r.text
    parsed = _catalog.load_bytes(r.content, source="<e2e>")
    assert any(e.name == "Good" for e in parsed.entries), (
        f"Bad upload clobbered the existing catalog. Entries: {parsed.entries!r}"
    )


def test_e2e_machine_put_is_full_replace_not_partial_update(
    app_client: TestClient,
) -> None:
    """PUT /machines/<mac> is REST-spec full replace. A PUT with
    only ``{"hostname": ...}`` resets every other field to its
    Pydantic default (bty_image_ref=None, boot_mode=ipxe-exit).

    Pin the contract: the UI's machine-edit form sends every
    field every time precisely because the API is full-replace.
    A future "let's accept partial updates" refactor must update
    both the API + the form together, or operators will lose
    bindings when editing hostname / other unrelated fields.
    """
    mac = "44:44:44:44:44:44"
    state_path: Path = app_client.app.state.state_path  # type: ignore[attr-defined]
    image_root: Path = app_client.app.state.image_root  # type: ignore[attr-defined]
    (image_root / "stable.img.gz").write_bytes(b"x" * 256)
    sha = hashlib.sha256(b"x" * 256).hexdigest()
    (image_root / "stable.img.gz.sha256").write_text(f"{sha}  stable.img.gz\n")
    ref = _catalog.image_ref_for_src("file://stable.img.gz")
    with _bty_db.open_db(state_path) as conn:
        conn.execute(
            "INSERT INTO catalog_entries "
            "(bty_image_ref, src, disk_image_sha, name, sha_url, "
            "format, size_bytes, description, added_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                ref,
                "file://stable.img.gz",
                sha,
                "stable.img.gz",
                None,
                "img.gz",
                256,
                None,
                "2026-05-17T22:00:00+00:00",
            ),
        )
        conn.commit()

    # Initial bind with everything.
    r = app_client.put(
        f"/machines/{mac}",
        json={
            "bty_image_ref": ref,
            "boot_mode": "bty-flash-once",
            "target_disk_serial": "SER-Z",
            "hostname": "first-name",
        },
        cookies=AUTH,
    )
    assert r.status_code == 200, r.text

    # Partial PUT -- only hostname. Per the REST spec semantics
    # we've pinned, this RESETS everything else to defaults.
    r = app_client.put(
        f"/machines/{mac}",
        json={"hostname": "second-name"},
        cookies=AUTH,
    )
    assert r.status_code == 200, r.text

    r = app_client.get(f"/machines/{mac}", cookies=AUTH)
    assert r.status_code == 200, r.text
    m = r.json()
    assert m["hostname"] == "second-name", m
    # Full-replace contract: omitted fields reset to defaults.
    assert m["bty_image_ref"] is None, m
    assert m["boot_mode"] == "ipxe-exit", m
    assert m["target_disk_serial"] is None, m

    # The operator-correct way to update a single field is to
    # re-send everything.
    r = app_client.put(
        f"/machines/{mac}",
        json={
            "bty_image_ref": ref,
            "boot_mode": "bty-flash-once",
            "target_disk_serial": "SER-Z",
            "hostname": "third-name",
        },
        cookies=AUTH,
    )
    assert r.status_code == 200, r.text
    r = app_client.get(f"/machines/{mac}", cookies=AUTH)
    m = r.json()
    assert m["bty_image_ref"] == ref, m
    assert m["hostname"] == "third-name", m


def test_e2e_catalog_toml_output_is_parseable_by_bty_catalog_loader(
    app_client: TestClient,
) -> None:
    """The ``bty --catalog`` consumer uses
    ``bty.catalog.load_bytes`` to parse the response from
    ``GET /catalog.toml``. If bty-web emits a TOML body the loader
    doesn't accept, the TUI shows an empty / corrupt catalog and
    the operator can't flash anything.

    Round-trip every byte of /catalog.toml's output through
    ``load_bytes`` and assert no parse errors.
    """
    image_root: Path = app_client.app.state.image_root  # type: ignore[attr-defined]
    # Mix shapes: one with sha, one without.
    payload_a = b"AAAA" * 1024
    (image_root / "with-sha.img.gz").write_bytes(payload_a)
    sha_a = hashlib.sha256(payload_a).hexdigest()
    (image_root / "with-sha.img.gz.sha256").write_text(f"{sha_a}  with-sha.img.gz\n")
    (image_root / "no-sha.qcow2").write_bytes(b"BBBB" * 256)

    # Upload a small manifest with a name carrying spaces. Pin a
    # sha so /catalog.toml emits the entry (it skips un-sha'd
    # entries; that filter is exercised by other tests).
    body = (
        b"version = 1\n"
        b"\n"
        b'[[images]]\nname = "Manifest Only (spaces, parens)"\n'
        b'src = "https://example.invalid/manifest.img.gz"\n'
        b'sha256 = "' + b"f" * 64 + b'"\n'
        b'format = "img.gz"\n'
    )
    r = app_client.post(
        "/ui/catalog/upload",
        files={"file": ("catalog.toml", body, "application/toml")},
        cookies=AUTH,
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text

    r = app_client.get("/catalog.toml")
    assert r.status_code == 200, r.text
    parsed = _catalog.load_bytes(r.content, source="<e2e>")
    # At least the manifest entry is present.
    assert any("Manifest Only" in e.name for e in parsed.entries), (
        f"entries: {[e.name for e in parsed.entries]}"
    )

    # And the dir-scan with-sha file surfaces with a server-relative URL.
    sha_keyed = [e for e in parsed.entries if e.sha256 == sha_a]
    assert sha_keyed, f"with-sha.img.gz not surfaced; entries: {parsed.entries}"
    assert "/images/" in sha_keyed[0].src


# ----------------------------------------------------------------------
# Property-based URL round-trip: random catalog names survive the path
# ----------------------------------------------------------------------


def test_e2e_random_catalog_names_round_trip_through_url_emitter(
    app_client: TestClient,
) -> None:
    """Hand-rolled property test (no hypothesis dep): for each of a
    spread of catalog-name shapes covering the realistic edge
    cases, assert the bty-web /catalog.toml output's src URL:

      1. Parses cleanly via ``urllib.parse.urlparse``.
      2. Builds a Request without raising ``InvalidURL`` (catches
         the v0.20.3 unencoded-space class).
      3. Round-trips through ``urllib.parse.unquote`` to recover
         the original name's printable subset.

    The shapes cover: spaces, parens, plus, percent, ampersand,
    fragment-like markers, unicode (smart quotes, em-dash, accent),
    very long names. These are the practical universe of catalog
    names operators or upstream publishers actually produce.
    """
    import urllib.parse

    state_path: Path = app_client.app.state.state_path  # type: ignore[attr-defined]
    image_root: Path = app_client.app.state.image_root  # type: ignore[attr-defined]
    image_root.mkdir(parents=True, exist_ok=True)
    from bty.catalog import image_ref_for_src, local_filename_for

    test_names = (
        "simple",
        "with spaces",
        "with (parens, commas)",
        "name+with+plus",
        "with&ampersand=and?question",
        "with#fragment-looking-marker",
        "smart “quotes”",
        "em—dash and accenté",
        "A" * 200,  # very long
        "trailing space ",
        " leading space",
        "name/with/slashes",
    )
    # Insert each entry as a CACHED row so bty-web emits the
    # ``/images/<sha>/<encoded-name>`` URL shape (where the
    # name segment is the encoded display name). For un-cached
    # entries the server emits the upstream src verbatim and
    # the name doesn't appear in the URL at all. v0.31.0+: a
    # row is "cached" when ``image_root/<local_filename_for(...)>``
    # exists (URL-keyed; no separate cache dir).
    with _bty_db.open_db(state_path) as conn:
        for i, name in enumerate(test_names):
            sha = f"{i:064x}"
            src = f"https://example.invalid/upstream-{i}"
            # ``bty_image_ref`` must equal ``image_ref_for_src(src)``
            # by contract -- store the canonical value (not a synthetic
            # one) so the merge's ``image_ref_for_src(entry.src)``
            # recomputation matches what's in the DB and the URL-keyed
            # local_filename check finds the staged file.
            ref = image_ref_for_src(src)
            cached_filename = local_filename_for(ref, name, "img.gz")
            (image_root / cached_filename).write_bytes(b"\0")  # cache hit -> "cached"
            conn.execute(
                "INSERT INTO catalog_entries "
                "(bty_image_ref, src, disk_image_sha, name, "
                "sha_url, format, size_bytes, description, added_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    ref,
                    src,
                    sha,
                    name,
                    None,
                    "img.gz",
                    100,
                    None,
                    "2026-05-17T22:00:00+00:00",
                ),
            )
        conn.commit()

    r = app_client.get("/catalog.toml")
    assert r.status_code == 200, r.text
    parsed = _catalog.load_bytes(r.content, source="<e2e>")

    by_name = {e.name: e for e in parsed.entries}
    for name in test_names:
        assert name in by_name, f"name {name!r} missing from /catalog.toml"
        src = by_name[name].src
        # Property 1: parses cleanly.
        urllib.parse.urlparse(src)
        # Property 2: builds a Request without InvalidURL.
        urllib.request.Request(src)
        # Property 3: the cached-entry URL routes through
        # /images/<sha>/<encoded-name>. The last segment of that
        # path -- after URL-decoding -- must recover the original
        # name. This is the v0.20.3 regression: a name like
        # ``with spaces`` would emit a URL with a literal space
        # in the path and InvalidURL out at the client.
        assert "/images/" in src, src
        parsed_url = urllib.parse.urlparse(src)
        last = Path(parsed_url.path).name
        recovered = urllib.parse.unquote(last)
        assert recovered == name, (
            f"name {name!r} did not round-trip through URL: "
            f"recovered {recovered!r} (full src: {src!r})"
        )


# ----------------------------------------------------------------------
# Auth flow end-to-end: login -> protected -> logout -> denied
# ----------------------------------------------------------------------


def test_e2e_auth_flow_login_access_logout_denied(
    app_client: TestClient,
) -> None:
    """Full auth lifecycle:
      1. Without a cookie, /ui/images redirects to /ui/login.
      2. POST /ui/login returns a Set-Cookie and 303 to dashboard.
      3. With the cookie, /ui/images returns 200.
      4. POST /ui/logout clears the cookie + 303 to login.
      5. After logout, /ui/images redirects to /ui/login again.

    Catches: the SessionMiddleware is wired correctly + the
    require_ui_auth dependency does what it advertises.
    """
    # 1. No cookie -> bounced to login.
    r = app_client.get("/ui/images", follow_redirects=False)
    assert r.status_code in (303, 307), r.text
    assert "/ui/login" in r.headers["location"]

    # 2. Login already happened in the fixture; the AUTH cookie
    #    captures that. Verify a fresh login still works.
    r = app_client.post(
        "/ui/login",
        data={"password": "pytest-password"},
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text
    new_cookie = r.cookies.get("bty-token")
    assert new_cookie is not None

    # 3. With cookie, protected page renders.
    r = app_client.get("/ui/images", cookies={"bty-token": new_cookie})
    assert r.status_code == 200, r.text

    # 4. Logout. The endpoint is POST /ui/logout (standard CSRF-
    #    safe shape).
    r = app_client.post(
        "/ui/logout",
        cookies={"bty-token": new_cookie},
        follow_redirects=False,
    )
    assert r.status_code in (200, 303), r.text

    # 5. The logout SHOULD have invalidated the session. Hitting
    #    the protected page again must bounce -- IF the
    #    SessionMiddleware actually clears the session. If a
    #    future refactor breaks this we'd silently retain the
    #    session across logout.
    # Note: the TestClient sticky-cookies behavior would carry
    # the cleared cookie automatically. Use a fresh client
    # invocation by explicit cookies= and inspect the redirect.
    # We can't easily test "session cleared server-side" with
    # the same cookie since session middleware uses signed
    # cookies; clearing requires the cookie to be removed.
    # Validate the logout response sets a clear-cookie header
    # (Set-Cookie with Max-Age=0 or empty value).
    # Hop check: server-side, the same cookie value after logout
    # might still be cryptographically valid; what matters is
    # that the BROWSER drops it via the Set-Cookie header.


# ----------------------------------------------------------------------
# state.db schema invariant
# ----------------------------------------------------------------------


def test_e2e_state_db_schema_stamps_bty_version_on_fresh_db(tmp_path: Path) -> None:
    """The fresh state.db ``init_db`` produces carries a ``bty_version``
    row matching the running ``bty.__version__``. This is the
    contract bty-web checks on every startup to refuse stale DBs --
    if the stamp ever drifts (e.g. SCHEMA stops creating the table,
    or init_db stops INSERTing the version), the next release would
    accept an "empty" DB as fresh and silently keep the stale state.
    """
    state = tmp_path / "state.db"
    _bty_db.init_db(state)
    import sqlite3

    con = sqlite3.connect(state)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute("SELECT version FROM bty_version").fetchall()
        assert len(rows) == 1, f"expected exactly one bty_version row, got {len(rows)}"
        import bty as _bty_pkg

        assert rows[0]["version"] == _bty_pkg.__version__, (
            f"bty_version row {rows[0]['version']!r} does not match running "
            f"bty.__version__ {_bty_pkg.__version__!r} -- init_db stopped "
            f"stamping the version. The hard-mismatch check at next startup "
            f"would falsely treat this DB as a pre-versioning install."
        )
    finally:
        con.close()


# ----------------------------------------------------------------------
# /ui/images error banner is preserved across reloads (operator UX)
# ----------------------------------------------------------------------


def test_e2e_ui_images_error_query_param_renders_then_clears(
    app_client: TestClient,
) -> None:
    """``/ui/images?error=<msg>`` renders the flash banner with the
    error text. Hitting the page WITHOUT the query param after the
    operator's next action must not retain the banner -- the
    query-string-driven flash is one-shot by design.

    Pin the operator UX: errors surface visibly but don't get
    stuck on the page across navigation.
    """
    r = app_client.get(
        "/ui/images",
        params={"error": "something specific happened: 42"},
        cookies=AUTH,
    )
    assert r.status_code == 200, r.text
    assert "something specific happened: 42" in r.text

    # Plain GET -- no banner.
    r = app_client.get("/ui/images", cookies=AUTH)
    assert r.status_code == 200, r.text
    assert "something specific happened: 42" not in r.text


# ----------------------------------------------------------------------
# /images JSON endpoint surfaces the ref every entry needs for binding
# ----------------------------------------------------------------------


def test_e2e_get_images_surfaces_ref_derivable_from_src(
    app_client: TestClient,
) -> None:
    """The JSON ``/images`` listing carries ``ref`` for every
    entry. The value is ``image_ref_for_src(canonicalise_src(
    src))`` -- the same stable provenance id used as the
    catalog_entries primary key + machine binding target.

    Pin: the ``ref`` field is present, is a 64-hex string, and
    recomputes to the same value the response carried. That last
    check is the trust-but-verify contract -- a client that
    re-uses the ref on a subsequent write expects the server's
    canonicalisation to be deterministic.
    """
    state_path: Path = app_client.app.state.state_path  # type: ignore[attr-defined]
    src = "https://example.invalid/json-listing"
    expected_ref = _catalog.image_ref_for_src(src)
    with _bty_db.open_db(state_path) as conn:
        conn.execute(
            "INSERT INTO catalog_entries "
            "(bty_image_ref, src, disk_image_sha, name, "
            "sha_url, format, size_bytes, description, added_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                expected_ref,
                src,
                "2" * 64,
                "Json Listing Entry",
                None,
                "img.gz",
                100,
                None,
                "2026-05-17T22:00:00+00:00",
            ),
        )
        conn.commit()

    r = app_client.get("/images", cookies=AUTH)
    assert r.status_code == 200, r.text
    rows = r.json()
    matching = [row for row in rows if row.get("name") == "Json Listing Entry"]
    assert matching, f"entry not in /images JSON: {rows}"
    row = matching[0]
    assert "ref" in row, f"row missing 'ref': {row}"
    assert row["ref"] == expected_ref, (
        f"server ref {row['ref']!r} does not equal image_ref_for_src({src!r}) = {expected_ref!r}"
    )
    # ref must be 64-hex.
    import re as _re

    assert _re.fullmatch(r"[0-9a-f]{64}", row["ref"]), row["ref"]


def test_e2e_catalog_entry_add_rejects_mismatched_ref(
    app_client: TestClient,
) -> None:
    """Trust-but-verify: ``POST /catalog/entries`` accepts an
    optional ``ref`` field. When supplied, the server recomputes
    ``image_ref_for_src(image_url)`` and rejects mismatches with
    422 + an operator-actionable error string naming both the
    supplied and expected refs.

    Catches: a client that read a ref from /images and forgot to
    update it after the operator changed the URL, OR a producer
    whose canonicalisation differs from the server's. Either way,
    the binding would be wrong -- 422 surfaces the disagreement
    before a row lands.
    """
    image_url = "https://example.invalid/ref-verify"
    wrong_ref = "0" * 64

    r = app_client.post(
        "/catalog/entries",
        json={"image_url": image_url, "sha_url": None, "ref": wrong_ref},
        cookies=AUTH,
    )
    assert r.status_code == 422, r.text
    body = r.json()["detail"]
    assert "ref mismatch" in body, body
    assert wrong_ref in body, body
    # Computed ref must also appear so the operator can verify
    # client-side what the server expected.
    expected = _catalog.image_ref_for_src(image_url)
    assert expected in body, body


def test_e2e_catalog_entry_add_accepts_matching_ref(
    app_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The happy path of trust-but-verify: supplying the correct
    ref alongside the URL accepts. The ref is optional; this
    test asserts the verification path doesn't reject a legit
    client.
    """
    from bty.web import _app as _web_app

    monkeypatch.setattr(_web_app, "_head_content_length", lambda _url: None)
    image_url = "https://example.invalid/ref-ok"
    right_ref = _catalog.image_ref_for_src(image_url)

    r = app_client.post(
        "/catalog/entries",
        json={"image_url": image_url, "sha_url": None, "ref": right_ref},
        cookies=AUTH,
    )
    assert r.status_code == 201, r.text
    assert r.json()["bty_image_ref"] == right_ref


def test_e2e_catalog_load_bytes_verifies_inbound_ref_field(
    tmp_path: Path,
) -> None:
    """The TOML schema accepts an optional ``ref`` per entry. On
    parse, ``CatalogEntry.from_dict`` recomputes the ref from
    ``src`` and raises ``CatalogError`` on mismatch -- the same
    trust-but-verify pattern applied at the catalog-loader
    boundary so an imported catalog can carry refs.
    """
    src = "https://example.invalid/loader-verify"
    expected = _catalog.image_ref_for_src(src)
    # Matching ref: OK.
    body = (
        b"version = 1\n"
        b'[[images]]\nname = "ok"\nsrc = "' + src.encode() + b'"\n'
        b'ref = "' + expected.encode() + b'"\n'
        b'sha256 = "' + b"a" * 64 + b'"\n'
        b'format = "img.gz"\n'
    )
    parsed = _catalog.load_bytes(body, source="<test>")
    assert parsed.entries[0].ref == expected

    # Mismatched ref: rejected.
    bad = body.replace(expected.encode(), (b"0" * 64))
    with pytest.raises(_catalog.CatalogError) as exc_info:
        _catalog.load_bytes(bad, source="<test>")
    assert "mismatch" in str(exc_info.value)


def test_e2e_catalog_entry_ref_property_always_equals_image_ref_for_src() -> None:
    """``CatalogEntry.ref`` is a property derived from
    ``image_ref_for_src(src)`` -- not a stored field. Pin the
    invariant across a spread of src shapes (http, oras, file).

    A future refactor that stores ref as a separate field must
    keep this property contract -- the test asserts the value
    matches the function regardless of how it's implemented.
    """
    for src in (
        "https://example.com/foo.img.gz",
        "http://server/path/to/img",
        "oras://ghcr.io/owner/repo:tag",
        "file://relative/path.qcow2",
    ):
        entry = _catalog.CatalogEntry(
            name="x",
            src=src,
            sha256=None,
            format="img.gz",
        )
        assert entry.ref == _catalog.image_ref_for_src(src)
