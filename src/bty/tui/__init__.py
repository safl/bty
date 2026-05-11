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
from pathlib import Path

import bty


def main(argv: list[str] | None = None, *, prog: str = "bty-tui") -> None:
    """Console-script entry point for ``bty-tui`` (or ``bty tui``).

    Defers loading the textual app until invocation time so a missing
    ``[tui]`` extra produces a clear "reinstall with extras" message
    rather than a raw ``ModuleNotFoundError``. ``prog`` controls how
    the program identifies itself in ``--help`` and ``--version`` so
    the same code path serves both the standalone ``bty-tui`` console
    script and ``bty tui``'s argparse bypass (cli.py passes
    ``prog="bty tui"``).
    """
    parser = argparse.ArgumentParser(
        prog=prog,
        description=f"{prog} - terminal UI for image inspection and flashing",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"{prog} {bty.__version__}",
    )
    parser.add_argument(
        "--server",
        type=str,
        default=None,
        help="bty-web server URL (e.g. ``http://server:8080``). When set, "
        "the TUI fetches the image catalog from the server's "
        "``GET /images`` and streams images straight from the server "
        "to the target disk. Without it, the TUI scans a local "
        "image-root directory.",
    )
    parser.add_argument(
        "--mac",
        type=str,
        default=None,
        help="Self-MAC of this client (e.g. from the live env's "
        "``bty.mac=`` cmdline param). When set together with "
        "``--server``, the TUI ``POST``s ``<server>/pxe/<mac>/done`` "
        "after a successful flash so the server's ``last_flashed_at`` "
        "is updated.",
    )
    parser.add_argument(
        "--image-root",
        type=Path,
        default=None,
        help="Local directory to scan for images (overrides the "
        "``BTY_IMAGE_ROOT`` env var and the live env default of "
        "``/var/lib/bty/images``). Ignored when ``--server`` is set "
        "(the catalog comes from the server in remote mode).",
    )
    args = parser.parse_args(argv)

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

    BtyTui(image_root=args.image_root, server_url=args.server, mac=args.mac).run()
