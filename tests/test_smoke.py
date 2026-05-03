"""Smoke tests verifying the scaffold imports cleanly."""

import bty


def test_version() -> None:
    assert bty.__version__ == "0.1.0.dev0"


def test_subpackages_import() -> None:
    import bty.cli
    import bty.tui
    import bty.web

    assert callable(bty.cli.main)
    assert callable(bty.tui.main)
    assert callable(bty.web.main)
