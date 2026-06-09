"""Smoke tests verifying the scaffold imports cleanly."""

import sys
from pathlib import Path

import pytest

import bty


def test_version_is_a_non_empty_string() -> None:
    """``bty.__version__`` is sourced from package metadata; assert it's set."""
    assert isinstance(bty.__version__, str)
    assert bty.__version__


def test_subpackages_import() -> None:
    import bty.tui
    import bty.web

    assert callable(bty.tui.main)
    assert callable(bty.web.main)


def test_bty_main_handles_missing_extras(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A bare ``pipx install bty-lab`` (no ``[tui]`` extra) must
    produce a clear hint when ``bty`` is invoked, not a raw
    ``ModuleNotFoundError``.

    Simulated by poisoning the deferred-import target so the ``from
    bty.tui._app import BtyTui`` inside ``main()`` fails.
    """
    monkeypatch.setitem(sys.modules, "bty.tui._app", None)

    import bty.tui as tui_mod

    with pytest.raises(SystemExit) as excinfo:
        # Pass empty argv so argparse doesn't pick up pytest's args.
        tui_mod.main([])

    assert excinfo.value.code == 1
    err = capsys.readouterr().err
    assert "bty-lab[tui]" in err


def test_bty_web_main_handles_missing_extras(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A bare ``pipx install bty-lab`` (no ``[web]`` extra) must
    produce a clear hint when ``bty-web`` is invoked, not a raw
    ``ModuleNotFoundError``. Symmetric with the ``bty`` test above.

    Simulated by poisoning the deferred-import targets so the
    ``import uvicorn`` / ``from bty.web._app import create_app``
    inside ``main()`` fails.
    """
    monkeypatch.setitem(sys.modules, "uvicorn", None)

    import bty.web as web_mod

    with pytest.raises(SystemExit) as excinfo:
        web_mod.main([])

    assert excinfo.value.code == 1
    err = capsys.readouterr().err
    assert "bty-lab[web]" in err


def test_bty_main_version_flag(capsys: pytest.CaptureFixture[str]) -> None:
    """``bty --version`` exits 0 with ``bty <version>`` on stdout."""
    import bty.tui as tui_mod

    with pytest.raises(SystemExit) as excinfo:
        tui_mod.main(["--version"])

    assert excinfo.value.code == 0
    out = capsys.readouterr().out
    assert out.startswith("bty ")
    assert bty.__version__ in out


def test_bty_web_main_version_flag(capsys: pytest.CaptureFixture[str]) -> None:
    """``bty-web --version`` exits 0 with ``bty-web <version>`` on stdout."""
    import bty.web as web_mod

    with pytest.raises(SystemExit) as excinfo:
        web_mod.main(["--version"])

    assert excinfo.value.code == 0
    out = capsys.readouterr().out
    assert out.startswith("bty-web ")
    assert bty.__version__ in out


def test_resolve_secret_key_cfg_wins(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A configured ``cfg.server.session_secret`` (via bty.toml or
    its env override ``BTY_SERVER_SESSION_SECRET``) takes precedence
    over any on-disk file."""
    from bty.web import _config, _resolve_secret_key

    secret_file = tmp_path / "session-secret"
    secret_file.write_text("from-disk\n", encoding="utf-8")
    monkeypatch.setenv("BTY_SERVER_SESSION_SECRET", "from-cfg")
    # Re-load the active config so the env override above takes effect.
    _config.set_active_config(_config.load_config([]))

    assert _resolve_secret_key(tmp_path) == "from-cfg"


def test_resolve_secret_key_reads_existing_file(tmp_path: Path) -> None:
    """Without env override, the persisted secret is reused so
    bty-web survives a restart without invalidating
    every operator's session cookie. The autouse conftest fixture
    installs an env-free default Config, so ``cfg.server.session_secret``
    is empty and the function falls through to the disk file."""
    from bty.web import _resolve_secret_key

    secret_file = tmp_path / "session-secret"
    # Trailing whitespace must be stripped -- the secret is written
    # as ``key + "\n"`` so file-round-trip cycles add one.
    secret_file.write_text("persisted-key\n", encoding="utf-8")

    assert _resolve_secret_key(tmp_path) == "persisted-key"


def test_resolve_secret_key_generates_and_persists(tmp_path: Path) -> None:
    """Fresh ``state_dir`` (no cfg override, no file): generate a key,
    write it with mode 0640, return it. Second call must read
    the same value back (idempotent across restarts)."""
    from bty.web import _resolve_secret_key

    fresh = tmp_path / "new-state"
    assert not fresh.exists()

    first = _resolve_secret_key(fresh)
    assert first  # non-empty
    secret_file = fresh / "session-secret"
    assert secret_file.exists()
    assert secret_file.stat().st_mode & 0o777 == 0o640

    second = _resolve_secret_key(fresh)
    assert second == first


def test_resolve_secret_key_rejects_empty_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """REGRESSION (v0.33.8): an empty ``BTY_SESSION_SECRET`` env var
    must be treated as unset, falling through to file / generation.
    SessionMiddleware silently accepts an empty HMAC key, which
    makes the resulting session cookies forgeable by anyone on the
    LAN segment -- so we never let one through, no matter where the
    empty value came from."""
    from bty.web import _resolve_secret_key

    monkeypatch.setenv("BTY_SESSION_SECRET", "")
    fresh = tmp_path / "new-state"
    key = _resolve_secret_key(fresh)
    assert key  # generated, non-empty
    assert key != "", "empty env must NOT pass through"
    # And the on-disk file must carry the generated key, not be empty.
    persisted = (fresh / "session-secret").read_text(encoding="utf-8").strip()
    assert persisted == key


def test_resolve_secret_key_rejects_whitespace_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Whitespace-only env values are treated as empty (same risk
    profile as an actual empty string -- ``.strip()`` returns
    empty, SessionMiddleware would accept it)."""
    from bty.web import _resolve_secret_key

    monkeypatch.setenv("BTY_SESSION_SECRET", "   \n\t  ")
    fresh = tmp_path / "new-state"
    key = _resolve_secret_key(fresh)
    assert key.strip() != ""


def test_resolve_secret_key_rejects_empty_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """REGRESSION (v0.33.8): an empty session-secret file (a
    half-written file from a crashed first boot, an operator
    ``touch``, ...) must NOT be used as the HMAC key. Same forgeable-
    cookie risk as the empty-env case. The function must regenerate
    + atomically rewrite, leaving a NON-empty file behind."""
    from bty.web import _resolve_secret_key

    monkeypatch.delenv("BTY_SESSION_SECRET", raising=False)
    secret_file = tmp_path / "session-secret"
    secret_file.write_text("", encoding="utf-8")

    key = _resolve_secret_key(tmp_path)
    assert key  # non-empty
    # The empty file was replaced with the generated key.
    persisted = secret_file.read_text(encoding="utf-8").strip()
    assert persisted == key
    assert persisted != ""


def test_resolve_secret_key_rejects_whitespace_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A file containing only whitespace is functionally empty for
    HMAC purposes; treat it as missing and regenerate."""
    from bty.web import _resolve_secret_key

    monkeypatch.delenv("BTY_SESSION_SECRET", raising=False)
    secret_file = tmp_path / "session-secret"
    secret_file.write_text("\n\n   \t\n", encoding="utf-8")

    key = _resolve_secret_key(tmp_path)
    assert key.strip() != ""
    assert key == secret_file.read_text(encoding="utf-8").strip()


def test_resolve_secret_key_persist_is_atomic(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The generate-and-persist path must write through a same-dir
    tempfile + rename so a crash mid-write can't leave a truncated
    secret on disk. We can't easily inject a crash; instead we
    assert no ``.tmp`` debris is left after a successful generate."""
    from bty.web import _resolve_secret_key

    monkeypatch.delenv("BTY_SESSION_SECRET", raising=False)
    fresh = tmp_path / "new-state"
    _resolve_secret_key(fresh)

    leftovers = [p for p in fresh.iterdir() if p.name.endswith(".tmp")]
    assert not leftovers, f"atomic-write tempfiles must not be left behind: {leftovers!r}"


def test_etc_issue_uses_only_documented_agetty_escapes() -> None:
    """``/etc/issue`` is rendered by agetty at login-prompt time;
    every ``\\<char>`` sequence in the file is interpreted by
    agetty's escape parser. Figlet-style backslash ASCII art
    (``\\__|``, ``\\__,``) confuses the parser and emits VT100
    control bytes onto the serial console right before the
    login banner.

    Pin the USB live-env /etc/issue so any "spice up the
    banner" attempt with backslash-laden ASCII gets caught here
    instead of by an operator watching CI logs.

    Allowed escapes: the agetty(8)-documented set
    (``\\b \\d \\e \\l \\m \\n \\o \\r \\s \\t \\u \\U \\v
    \\4 \\6 \\S``) plus ``\\\\`` (literal backslash). Anything
    else outside of comments fails the test.
    """
    import re
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[1]
    # ``\S{...}`` is multi-char; treat as one allowed token.
    allowed = set("bdelmnorstuUv46S\\")

    def _check_issue_body(body: str, source: str) -> None:
        # ``\<char>`` after any non-backslash; strict: every
        # escape in the rendered issue body must be allowed.
        for match in re.finditer(r"(?<!\\)\\(.)", body):
            ch = match.group(1)
            if ch not in allowed:
                raise AssertionError(
                    f"{source}: disallowed agetty escape ``\\{ch}`` "
                    f"-- use a plain ASCII alternative (this regression "
                    f"emits VT100 escapes onto ``console=ttyS0``)"
                )

    # The USB live-env's shipped /etc/issue.
    path = repo_root / "bty-media/live-build/config/includes.chroot/etc/issue"
    _check_issue_body(path.read_text(), str(path))


def test_starter_catalog_template_renders_valid_catalog() -> None:
    """The release-published catalog (``releases/latest/download/catalog.toml``)
    is generated from ``scripts/starter_catalog.toml.in`` with ``{version}``
    substituted at release time. A malformed template would ship a catalog
    that ``bty --catalog`` couldn't parse -- guard by rendering with a
    dummy version + round-tripping through ``bty.catalog``.

    The starter catalog was previously baked as .bri files on the USB
    stick; v0.25.5+ ships it as a release artifact instead so there is
    one catalog format, one mental model.
    """
    from bty import catalog

    template = (
        Path(__file__).resolve().parents[1] / "scripts" / "starter_catalog.toml.in"
    ).read_text()
    rendered = template.format(version="0.0.0")
    cat = catalog.load_bytes(rendered.encode("utf-8"))
    assert len(cat) >= 1
    for entry in cat:
        assert entry.src.startswith(("oras://", "http://", "https://")), (
            f"catalog files must contain only remote srcs (the receiver can't "
            f"resolve file:// off the publisher's host); got {entry.src!r}"
        )


def test_generate_catalog_toml_round_trips_through_catalog_load(tmp_path: Path) -> None:
    """``scripts/generate_catalog_toml.py`` reads the starter catalog template
    (the same source-of-truth as the BTY_IMAGES bake) and emits a
    catalog manifest matching the schema ``bty.catalog.load_bytes``
    parses. The release workflow runs this script; if the output
    drifts from the schema, every operator pointing ``--catalog`` at
    the release asset stops working.

    Guard: invoke the generator into a tmp file, then round-trip the
    bytes through ``bty.catalog.load_bytes`` and assert all entries
    land, all use ``src`` (the catalog manifest schema's field key),
    and the oras:// entries don't carry a pre-pinned sha (rolling-tag
    invariant).
    """
    import subprocess as _sp

    from bty import catalog as _catalog

    repo_root = Path(__file__).resolve().parents[1]
    output = tmp_path / "catalog.toml"
    rc = _sp.run(
        [
            sys.executable,
            str(repo_root / "scripts" / "generate_catalog_toml.py"),
            str(output),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert rc.returncode == 0, f"generator failed: {rc.stderr}"
    assert output.is_file(), "generator did not write the output file"

    catalog_obj = _catalog.load_bytes(output.read_bytes(), source=str(output))
    assert catalog_obj.version == 1
    assert len(catalog_obj.entries) == 7
    # The starter set: seven nosi flashable images (Debian / Ubuntu /
    # Fedora / FreeBSD headless + Fedora desktop).
    nosi_entries = [e for e in catalog_obj.entries if "nosi" in e.name]
    assert len(nosi_entries) == 7
    # Rolling-tag invariant: oras:// entries are sha-less. Pre-
    # pinning at generate time would freeze the catalog and defeat
    # the whole point of the rolling tags.
    for entry in nosi_entries:
        assert entry.src.startswith("oras://ghcr.io/safl/nosi/")
        assert entry.sha256 is None, (
            f"{entry.name} has a pre-pinned sha256; generator should leave "
            f"oras:// rolling tags unresolved so they stay current"
        )


def test_mascot_logo_is_in_sync_across_assets() -> None:
    """The bty mascot artwork is shipped in two places (since
    v0.22.1's plymouth retirement):

    * ``docs/src/_static/bty-mascot.png`` -- Sphinx docs site / PDF.
    * ``src/bty/web/_static/bty-mascot.png`` -- /ui/* pages.

    They must be byte-identical so an operator never sees a stale
    version in one place and the current artwork in another. The
    plymouth path is gone with plymouth itself.
    """
    import hashlib

    repo_root = Path(__file__).resolve().parents[1]
    canonical = repo_root / "docs" / "src" / "_static" / "bty-mascot.png"
    web_static = repo_root / "src" / "bty" / "web" / "_static" / "bty-mascot.png"

    digests = {
        path: hashlib.sha256(path.read_bytes()).hexdigest() for path in (canonical, web_static)
    }
    distinct = set(digests.values())
    assert len(distinct) == 1, (
        f"bty mascot drifted between asset locations: {digests!r}. "
        f"Sync by copying {canonical} over the others."
    )


def test_plymouth_is_not_baked_into_the_live_env() -> None:
    """Plymouth was retired in v0.22.1: the kernel-stage graphical
    splash wedged plymouth-quit-wait.service on several Intel iGPUs
    (Minisforum MS-01, AMD EPYC bring-up box) and the mascot-splash
    value didn't justify the multi-layer workaround stack.

    Invariant: no plymouth packages live in any
    ``bty-base.list.chroot*`` package list; the plymouth theme dir
    and hook are gone.
    """
    repo_root = Path(__file__).resolve().parents[1]
    pkg_lists_dir = repo_root / "bty-media" / "live-build" / "config" / "package-lists"
    forbidden = {"plymouth", "plymouth-themes"}
    leaked: dict[str, set[str]] = {}
    for path in pkg_lists_dir.glob("*.list.chroot*"):
        lines = {
            line.strip()
            for line in path.read_text().splitlines()
            if line.strip() and not line.strip().startswith("#")
        }
        overlap = forbidden & lines
        if overlap:
            leaked[path.name] = overlap
    assert not leaked, (
        f"plymouth packages leaked into live-env package lists: {leaked}. "
        f"Plymouth was retired in v0.22.1; remove the entries."
    )

    # Theme dir + hook must not exist.
    theme_dir = (
        repo_root
        / "bty-media"
        / "live-build"
        / "config"
        / "includes.chroot"
        / "usr"
        / "share"
        / "plymouth"
    )
    hook = repo_root / "bty-media" / "live-build" / "config" / "hooks" / "normal"
    plymouth_hook = list(hook.glob("*-bty-plymouth.hook.chroot")) if hook.exists() else []
    assert not theme_dir.exists(), f"stale plymouth theme dir: {theme_dir}"
    assert not plymouth_hook, f"stale plymouth hook(s): {plymouth_hook}"


def test_nouveau_blacklisted_across_the_live_images() -> None:
    """Nouveau (in-tree Nvidia driver) stalls early boot 10-60s on
    Maxwell/Pascal/Turing cards probing for firmware bty does not
    need. Blacklist invariant: the bty live env must ship the
    modprobe.d config so any Nvidia-equipped target PXE-boots
    or USB-boots without the nouveau stall.

    The live env (bty-usb + bty-netboot) drops to /etc/modprobe.d/
    via the live-build includes.chroot tree.

    Plus belt-and-braces kernel cmdline. modprobe.d only catches
    later module loads; initramfs-resolved modules can sneak in
    before /etc/ is mounted. ``modprobe.blacklist=nouveau`` on
    the kernel cmdline closes that window.
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
    assert live_conf.is_file(), f"missing nouveau blacklist at {live_conf}"
    body = live_conf.read_text()
    assert "blacklist nouveau" in body, f"{live_conf} missing 'blacklist nouveau' directive"
    assert "install nouveau /bin/true" in body, (
        f"{live_conf} missing 'install nouveau /bin/true' belt-and-braces"
    )

    # Kernel cmdline coverage: both iPXE templates and the live-build
    # auto/config. Don't pin the exact ordering -- just that
    # ``modprobe.blacklist=nouveau`` appears in each so a future
    # template edit can't silently drop it.
    ipxe_tui = repo_root / "src" / "bty" / "web" / "_templates" / "ipxe_tui.j2"
    ipxe_flash = repo_root / "src" / "bty" / "web" / "_templates" / "ipxe_flash.j2"
    auto_config = repo_root / "bty-media" / "live-build" / "auto" / "config"
    for path in (ipxe_tui, ipxe_flash, auto_config):
        body = path.read_text()
        assert "modprobe.blacklist=nouveau" in body, (
            f"{path} missing 'modprobe.blacklist=nouveau' on the kernel cmdline"
        )
