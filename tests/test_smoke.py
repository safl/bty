"""Smoke tests verifying the scaffold imports cleanly."""

import sys

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
    agetty's escape parser. v0.7.4 caught a regression where
    figlet ASCII art (``\\__|``, ``\\__,``) confused the parser
    and emitted VT100 control bytes onto the serial console
    right before the login banner.

    Pin both the rootfs-shipped /etc/issue AND the heredoc
    bty-web-init writes on first boot so a future "let's spice
    up the banner" attempt with backslash-laden ASCII gets
    caught here instead of by an operator watching CI logs.

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
        # ``\<char>`` after any non-backslash; strict — every
        # escape in the rendered issue body must be allowed.
        for match in re.finditer(r"(?<!\\)\\(.)", body):
            ch = match.group(1)
            if ch not in allowed:
                raise AssertionError(
                    f"{source}: disallowed agetty escape ``\\{ch}`` "
                    f"-- use a plain ASCII alternative (this regression "
                    f"emits VT100 escapes onto ``console=ttyS0``)"
                )

    issue_path = repo_root / "bty-media" / "rootfs" / "server" / "etc" / "issue"
    _check_issue_body(issue_path.read_text(), str(issue_path))

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

    Plymouth has been added-then-dropped twice already (v0.4.x
    added, v0.5.12 dropped, post-v0.5.14 restored, v0.7.2 dropped
    again after the serial-console regression resurfaced). Pin it
    so a third "let's add the splash back" cycle gets caught by
    tests instead of by an operator watching a fresh appliance
    boot.

    Two layers of defense are pinned here:

    1. Plymouth is not in the ``packages:`` list (so cloud-init
       doesn't ADD it).
    2. ``apt-get -y purge plymouth`` is in ``runcmd:`` (so
       plymouth is REMOVED if the Debian-13 daily cloud-image
       baseline pre-installed it -- which is what happened in
       v0.7.2's bake despite the packages-list removal).
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
