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
        "--catalog",
        type=str,
        default=None,
        help="Catalog source. Accepts a local TOML file path "
        "(``./catalog.toml`` or ``/var/lib/bty/catalog.toml``) or "
        "a URL (``http://server:8080/catalog.toml``, "
        "``https://example.com/catalog.toml``, "
        "``oras://ghcr.io/owner/repo:tag``). Fetched once at startup "
        "and held in memory; pressing ``r`` only re-scans the local "
        "image-root, not the remote catalog. Without --catalog, "
        "the TUI scans a local image-root directory only.",
    )
    parser.add_argument(
        "--mac",
        type=str,
        default=None,
        help="Self-MAC of this client (e.g. from the live env's "
        "``bty.mac=`` cmdline param). When set together with a "
        "``--catalog`` URL whose base looks like a bty-web instance, "
        "the TUI ``POST``s ``<base>/pxe/<mac>/done`` after a "
        "successful flash. Best-effort: a non-bty-web catalog "
        "source (static file, oras://) skips the POST.",
    )
    parser.add_argument(
        "--image-root",
        type=Path,
        default=None,
        help="Local directory to scan for images (overrides the "
        "``BTY_IMAGE_ROOT`` env var and the live env default of "
        "``/var/lib/bty/images``). Local images and the --catalog "
        "entries surface together in the TUI's catalog table.",
    )
    args = parser.parse_args(argv)

    # Lifecycle progress -- the launch path has two slow phases an
    # operator stares at without feedback otherwise:
    #
    #   1. ``from bty.tui._app import BtyTui`` (1-3s): pulls Textual
    #      + bty.catalog + bty.flash + bty.oras into the
    #      interpreter. On slower hardware (low-end mini-PCs, EPYC
    #      bringup boxes) this is several seconds of "blinking
    #      cursor".
    #   2. ``BtyTui(...).run()`` -> Textual init paints the first
    #      frame (5-20s on the live env's framebuffer console).
    #      Once Textual enters alt-screen mode our prior output is
    #      hidden until the app exits.
    #
    # Print progress to stderr BEFORE the import + the run. The
    # operator sees: wrapper banner (from bty-tui-on-tty1 shell
    # wrapper, on the live env) -> these progress lines -> Textual
    # paints. The blank-screen window narrows from "the whole
    # launch" to "after the last progress line + before Textual's
    # first frame".
    #
    # Also mirror to ``/run/bty-tui.status`` so an operator who
    # Alt-F2'd to tty2 can ``cat`` it without having to read tty1's
    # transient output. ``/run`` is tmpfs on the live env so this
    # is forgotten on reboot; cheap to write.
    def _progress(msg: str) -> None:
        line = f"bty-tui: {msg}"
        print(line, file=sys.stderr, flush=True)
        try:
            with open("/run/bty-tui.status", "a") as f:
                f.write(line + "\n")
        except OSError:
            pass

    _progress(f"v{bty.__version__} starting...")
    _progress("loading UI dependencies (Textual)...")
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
    _progress("dependencies loaded")
    if args.catalog:
        _progress(f"catalog source: {args.catalog}")
    _progress("starting interface (first paint may take a few seconds)...")

    BtyTui(image_root=args.image_root, catalog_source=args.catalog, mac=args.mac).run()
