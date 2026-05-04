"""Smoke tests verifying the scaffold imports cleanly."""

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
