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
        tui_mod.main()

    assert excinfo.value.code == 1
    err = capsys.readouterr().err
    assert "bty-lab[tui]" in err
