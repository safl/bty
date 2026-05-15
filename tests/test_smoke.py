"""Smoke tests verifying the scaffold imports cleanly."""

import ast
import sys
import tomllib
from pathlib import Path

import pytest

import bty


def test_version_is_a_non_empty_string() -> None:
    """``bty.__version__`` is sourced from package metadata; assert it's set."""
    assert isinstance(bty.__version__, str)
    assert bty.__version__


def test_subpackages_import() -> None:
    import bty.cli
    import bty.tui
    import bty.web

    assert callable(bty.cli.main)
    assert callable(bty.tui.main)
    assert callable(bty.web.main)


def test_bty_tui_main_handles_missing_extras(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A CLI-only install (no ``[tui]`` extra) must produce a clear hint
    when ``bty-tui`` is invoked, not a raw ``ModuleNotFoundError``.

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


def test_bty_tui_main_version_flag(capsys: pytest.CaptureFixture[str]) -> None:
    """``bty-tui --version`` exits 0 with ``bty-tui <version>`` on stdout."""
    import bty.tui as tui_mod

    with pytest.raises(SystemExit) as excinfo:
        tui_mod.main(["--version"])

    assert excinfo.value.code == 0
    out = capsys.readouterr().out
    assert out.startswith("bty-tui ")
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


def test_server_cloudinit_base_is_valid_yaml() -> None:
    """``bty-media/auxiliary/cloudinit-base-server.user`` is read by
    cloud-init at bake time -- a YAML syntax error means the bake VM
    fails to start cloud-init and the operator gets to find out via
    a 30-minute CI run instead of in seconds locally.

    Pin parseability so any future runcmd / packages edit that
    breaks indentation or quoting fails fast. PyYAML is available
    via the dev group (transitive via uv); skip if it isn't.
    """
    try:
        import yaml
    except ImportError:
        pytest.skip("PyYAML not installed in this environment")

    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[1]
    base = repo_root / "bty-media" / "auxiliary" / "cloudinit-base-server.user"
    data = yaml.safe_load(base.read_text())
    assert isinstance(data, dict), (
        f"cloud-config must parse to a top-level mapping; got {type(data).__name__}"
    )
    # The two blocks the bake actually relies on -- if a future
    # edit accidentally drops them, the bake silently produces a
    # broken appliance. Pin minimal structure.
    assert "packages" in data and isinstance(data["packages"], list)
    assert "runcmd" in data and isinstance(data["runcmd"], list)


def test_etc_issue_uses_only_documented_agetty_escapes() -> None:
    """``/etc/issue`` is rendered by agetty at login-prompt time;
    every ``\\<char>`` sequence in the file is interpreted by
    agetty's escape parser. Figlet-style backslash ASCII art
    (``\\__|``, ``\\__,``) confuses the parser and emits VT100
    control bytes onto the serial console right before the
    login banner.

    Pin both the rootfs-shipped /etc/issue AND the heredoc
    bty-web-init writes on first boot so any "spice up the
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

    # All shipped /etc/issue files: the server bake's
    # pre-first-boot one, and the USB live-env's. The runtime
    # issue bty-web-init writes on first boot is checked
    # separately below.
    for relpath in (
        "bty-media/rootfs/server/etc/issue",
        "bty-media/live-build/config/includes.chroot/etc/issue",
    ):
        path = repo_root / relpath
        _check_issue_body(path.read_text(), str(path))

    # bty-web-init writes a runtime /etc/issue via heredoc; extract
    # the heredoc body and check it the same way.
    web_init = repo_root / "bty-media/rootfs/server/usr/local/sbin/bty-web-init"
    body = web_init.read_text()
    m = re.search(r"cat > /etc/issue <<'EOF'\n(.*?)\nEOF", body, flags=re.DOTALL)
    assert m is not None, "bty-web-init no longer writes /etc/issue via heredoc -- update this test"
    _check_issue_body(m.group(1), f"{web_init}:/etc/issue heredoc")


def test_server_cloudinit_does_not_install_plymouth() -> None:
    """The bty-server cloudinit base must not (re-)introduce
    plymouth: its quit/teardown leaks VT100 escape sequences onto
    ``console=ttyS0`` serial consoles, which is the operator's
    primary boot-watch surface for headless servers.

    Plymouth has been added and dropped multiple times when
    operators have asked for a boot splash; pin it out so the next
    "let's add the splash back" attempt gets caught by tests
    instead of by an operator watching a fresh appliance boot.

    Two layers of defense are pinned here:

    1. Plymouth is not in the ``packages:`` list (so cloud-init
       doesn't ADD it).
    2. ``apt-get -y purge plymouth`` is in ``runcmd:`` (so
       plymouth is REMOVED if the Debian-13 daily cloud-image
       baseline pre-installed it -- the packages-list removal
       alone is not enough).
    """
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[1]
    base = repo_root / "bty-media" / "auxiliary" / "cloudinit-base-server.user"
    body = base.read_text()
    # Layer 1: ``- plymouth`` / ``- plymouth-themes`` are the YAML
    # list-item forms in the ``packages:`` block;
    # ``plymouth-set-default-theme`` is the runcmd handle. Each is
    # a clear signal of plymouth being installed / configured.
    # Inline comments using the word are fine (the comment block
    # documents *why* plymouth isn't shipped).
    assert "\n  - plymouth\n" not in body
    assert "\n  - plymouth-themes\n" not in body
    assert "plymouth-set-default-theme" not in body
    # Layer 2: defensive purge in runcmd. Pin the literal command
    # so a refactor that "tidies" the runcmd block doesn't drop it.
    assert "apt-get -y purge plymouth" in body


def test_activate_pxe_helper_uses_same_fs_tempfile() -> None:
    """``bty-web-activate-pxe`` writes a config to /etc/dnsmasq.d/.
    The default ``mktemp`` would land in ``$TMPDIR`` = /tmp (tmpfs);
    a cross-filesystem ``mv`` to /etc/dnsmasq.d/ falls back to
    copy-then-unlink which fails with "unable to remove target:
    Read-only file system" under read-only-rootfs conditions. Pin
    the same-fs tempfile so the bug stays out."""
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[1]
    body = (repo_root / "bty-media/rootfs/server/usr/local/sbin/bty-web-activate-pxe").read_text()
    # ``mktemp -p /etc/dnsmasq.d ...`` keeps source + target on the
    # same filesystem, so the final ``mv`` is an atomic rename.
    assert "mktemp -p /etc/dnsmasq.d" in body


def test_server_cloudinit_ships_haveged() -> None:
    """Entropy starvation on N97-class hardware caused 20-minute
    systemd-journald start times even on RDRAND-capable CPUs (the
    kernel CSPRNG can briefly block on ``getrandom()`` before the
    trust-cpu logic settles). Ship ``haveged`` (CPU-jitter entropy
    daemon) + explicit ``random.trust_cpu=on
    random.trust_bootloader=on`` cmdline flags so boot doesn't
    depend on the kernel's compile-time defaults. Pin both layers."""
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[1]
    base = repo_root / "bty-media" / "auxiliary" / "cloudinit-base-server.user"
    body = base.read_text()
    # Layer 1: ``- haveged`` in the packages list.
    assert "\n  - haveged\n" in body
    # Layer 2: kernel cmdline trust hints.
    assert "random.trust_cpu=on" in body
    assert "random.trust_bootloader=on" in body
    # Bare-metal firmware + kernel + ``noresume`` for arbitrary
    # hardware. The kernel comes from trixie-backports (newer
    # in-tree drivers, e.g. r8169 RTL8125/8126 fixes, rtw89),
    # firmware via the ``firmware-linux-nonfree`` metapackage
    # which Depends on every individual firmware-* package. Pin
    # both layers so any "tidy the packages list" attempt
    # doesn't silently regress HW coverage. Pin the backports
    # source line too: without it the apt pin can't resolve to
    # the newer candidate version.
    assert "\n  - linux-image-amd64\n" in body
    assert "\n  - firmware-linux-nonfree\n" in body
    assert "trixie-backports" in body
    assert "noresume" in body
    # ``MODULES=most`` initramfs rebuild for broad bare-metal
    # driver coverage. The cloud image's default ``MODULES=dep``
    # initrd misses drivers for hardware that wasn't in the bake
    # VM (N97 slow boot symptom). Pin the rebuild so any "tidy
    # this up" attempt doesn't restore the regression.
    assert "MODULES=most" in body
    assert "update-initramfs -u" in body


def test_usb_iso_build_starter_bris_parse_as_toml() -> None:
    """The bake-time ``_STARTER_BRIS`` literal in ``usb_iso_build.py``
    writes one .bri file per entry into the freshly-mkfs'd BTY_IMAGES
    partition. If any entry's body isn't valid TOML (or doesn't pass
    ``bty.images.read_bri``'s validation), every USB stick built from
    the next release would ship with broken descriptors -- the live
    env would skip them silently from the catalog. Guard with an
    AST-extract + parse round-trip.

    The bake script can't be imported directly from tests (it's a
    cijoe task module, not a Python package), so we ast.parse the
    file, locate the ``_STARTER_BRIS = (...)`` assignment, and
    ``ast.literal_eval`` the tuple. This avoids running any of the
    surrounding cijoe-dependent code paths.
    """
    from bty import images

    script = Path(__file__).resolve().parents[1] / "cijoe" / "scripts" / "usb_iso_build.py"
    tree = ast.parse(script.read_text())
    starter = None
    for node in ast.walk(tree):
        target_name: str | None = None
        value_node = None
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
        ):
            target_name = node.targets[0].id
            value_node = node.value
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.value is not None
        ):
            target_name = node.target.id
            value_node = node.value
        if target_name == "_STARTER_BRIS" and value_node is not None:
            starter = ast.literal_eval(value_node)
            break
    assert starter is not None, "_STARTER_BRIS not found in usb_iso_build.py"
    assert len(starter) == 4, f"expected 4 starter .bri files, got {len(starter)}"

    names = {filename for filename, _ in starter}
    assert names == {
        "nosi-debian-sysdev-x86_64.bri",
        "nosi-ubuntu-sysdev-x86_64.bri",
        "nosi-fedora-sysdev-x86_64.bri",
        "bty-server-x86_64.bri",
    }

    # Each body must parse as TOML, declare a url, and round-trip
    # through read_bri without raising. nosi entries should use
    # ``oras://ghcr.io/``; the bty-server entry should use https.
    for filename, body in starter:
        parsed = tomllib.loads(body)
        assert "url" in parsed, f"{filename}: missing url"
        if filename.startswith("nosi-"):
            assert parsed["url"].startswith("oras://ghcr.io/safl/nosi/"), (
                f"{filename}: expected oras://ghcr.io/ URL, got {parsed['url']!r}"
            )
        else:
            assert parsed["url"].startswith("https://github.com/safl/bty/releases/"), (
                f"{filename}: expected GitHub release URL"
            )
        # Materialise to a tmp .bri to exercise read_bri's full
        # validation, including the size cap and schema checks.
        import tempfile

        with tempfile.NamedTemporaryFile("w", suffix=".bri", delete=False) as fh:
            fh.write(body)
            tmp = Path(fh.name)
        try:
            remote = images.read_bri(tmp)
            assert remote.url == parsed["url"]
        finally:
            tmp.unlink()


def test_generate_catalog_toml_round_trips_through_catalog_load(tmp_path: Path) -> None:
    """``scripts/generate_catalog_toml.py`` reads ``_STARTER_BRIS``
    (the same source-of-truth as the BTY_IMAGES bake) and emits a
    catalog manifest matching the schema ``bty.catalog.load_bytes``
    parses. The release workflow runs this script; if the output
    drifts from the schema, every operator pointing ``--catalog`` at
    the release asset stops working.

    Guard: invoke the generator into a tmp file, then round-trip the
    bytes through ``bty.catalog.load_bytes`` and assert all four
    entries land, all four use ``src`` (not the .bri-side ``url``
    key), and the oras:// entries don't carry a pre-pinned sha
    (rolling-tag invariant).
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
    assert len(catalog_obj.entries) == 4
    # Same four images as the BTY_IMAGES starter set: three nosi
    # sysdev images plus the bty-server appliance.
    nosi_entries = [e for e in catalog_obj.entries if "nosi" in e.name]
    server_entries = [e for e in catalog_obj.entries if "bty-server" in e.name]
    assert len(nosi_entries) == 3
    assert len(server_entries) == 1
    # Rolling-tag invariant: oras:// entries are sha-less. Pre-
    # pinning at generate time would freeze the catalog and defeat
    # the whole point of the rolling tags.
    for entry in nosi_entries:
        assert entry.src.startswith("oras://ghcr.io/safl/nosi/")
        assert entry.sha256 is None, (
            f"{entry.name} has a pre-pinned sha256; generator should leave "
            f"oras:// rolling tags unresolved so they stay current"
        )
    # bty-server entry uses a plain https URL (GitHub release
    # asset); generator preserves the same shape.
    assert server_entries[0].src.startswith("https://github.com/safl/bty/releases/")
