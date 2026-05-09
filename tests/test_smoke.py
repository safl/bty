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
    """
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[1]
    base = repo_root / "bty-media" / "auxiliary" / "cloudinit-base-server.user"
    body = base.read_text()
    # ``- plymouth`` and ``- plymouth-themes`` are the YAML
    # list-item forms in the ``packages:`` block; ``plymouth-set-
    # default-theme`` is the runcmd handle. Each is a clear
    # signal of plymouth being installed / configured. Inline
    # comments using the word are fine (the comment block in the
    # file documents *why* plymouth isn't shipped).
    assert "\n  - plymouth\n" not in body
    assert "\n  - plymouth-themes\n" not in body
    assert "plymouth-set-default-theme" not in body
