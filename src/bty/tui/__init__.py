"""bty.tui - terminal UI on top of bty.

This module is intentionally lightweight: it imports nothing from
:mod:`textual` at module level so a CLI-only install
(``pipx install bty-lab`` without the ``[tui]`` extra) can still
``import bty.tui`` for introspection without crashing. The actual
textual app lives in :mod:`bty.tui._app`, which is loaded only when
``bty-tui`` is invoked.
"""

from __future__ import annotations

import argparse
import sys

import bty


def main(argv: list[str] | None = None) -> None:
    """Console-script entry point for ``bty-tui``.

    Defers loading the textual app until invocation time so a missing
    ``[tui]`` extra produces a clear "reinstall with extras" message
    rather than a raw ``ModuleNotFoundError``.
    """
    parser = argparse.ArgumentParser(
        prog="bty-tui",
        description="bty-tui - terminal UI for image inspection and flashing",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"bty-tui {bty.__version__}",
    )
    parser.parse_args(argv)

    try:
        from bty.tui._app import BtyTui
    except ImportError as exc:
        print(
            f"bty-tui {bty.__version__}: required dependency is not installed "
            f"({exc.name or exc}); reinstall with "
            '`pipx install "bty-lab[tui]"`',
            file=sys.stderr,
        )
        sys.exit(1)

    BtyTui().run()
