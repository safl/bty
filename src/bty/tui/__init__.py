"""bty.tui - the bty terminal interface.

Module name is historical (Rich-based wizard module); the
console script is ``bty``. This module is intentionally
lightweight: it imports nothing from :mod:`rich` at module level
so an install without the ``[tui]`` extra can still ``import
bty.tui`` for introspection without crashing. The actual Rich-
based app lives in :mod:`bty.tui._app`, which is loaded only
when ``bty`` is invoked.
"""

from __future__ import annotations

import argparse
import sys

import bty

# Default ``--server`` value for the wizard. ``bty-server`` is the
# canonical LAN-DNS / mDNS hostname operators are encouraged to point
# at their appliance, so ``bty --mac X`` against a fresh box Just
# Works without any flags. Owned here (the [tui]-extra-free entry
# module) so the argparse default and ``BtyTui``'s constructor default
# can both depend on it without the import dragging in Rich.
DEFAULT_SERVER = "bty-server"


def main(argv: list[str] | None = None, *, prog: str = "bty") -> None:
    """Console-script entry point for ``bty``.

    Defers loading the Rich-based app until invocation time so a
    missing ``[tui]`` extra produces a clear "reinstall with extras"
    message rather than a raw ``ModuleNotFoundError``.
    """
    parser = argparse.ArgumentParser(
        prog=prog,
        description=(
            f"{prog}: flash images onto target disks, locally or via PXE. "
            f"Three modes:\n\n"
            f"  {prog}                          - interactive wizard\n"
            f"                                    (local image-root only)\n"
            f"  {prog} --catalog <URL>          - interactive wizard with\n"
            f"                                    the given catalog pre-loaded\n"
            f"                                    (equivalent to picking [c]\n"
            f"                                    on the source screen and\n"
            f"                                    typing the URL).\n"
            f"  {prog} --mac <MAC>              - server-driven mode:\n"
            f"                                    fetches a plan from\n"
            f"                                    --server's /pxe/<MAC>/plan\n"
            f"                                    and acts on it (auto-flash,\n"
            f"                                    interactive, or local-boot\n"
            f"                                    (whatever the server says).\n\n"
            "The operator-facing surface is intentionally narrow: in\n"
            "server-driven mode every knob (image, target disk, catalog\n"
            "overlay) comes from the bty-server's machine record, not the\n"
            "cmdline. --catalog is only useful for hand-driven runs."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"{prog} {bty.__version__}",
    )
    parser.add_argument(
        "--server",
        type=str,
        default=DEFAULT_SERVER,
        help=f"bty-server base URL or hostname. Default ``{DEFAULT_SERVER}`` "
        "(operator convenience: pair with a LAN DNS entry pointing at "
        "the appliance and ``bty --mac X`` just works). The netboot "
        "and USB-PXE paths pass this explicitly via ``bty.server=`` "
        "on the kernel cmdline. Bare hostnames are accepted; missing "
        "scheme defaults to ``http://``.",
    )
    parser.add_argument(
        "--mac",
        type=str,
        default=None,
        help="Self-MAC of this client (e.g. ``aa:bb:cc:dd:ee:ff``). "
        "When supplied, bty switches to server-driven mode: it "
        "GETs ``<server>/pxe/<mac>/plan`` and dispatches on the "
        "returned plan (auto-flash, interactive, or no-op). The "
        "live env passes this via ``bty.mac=`` on the kernel cmdline.",
    )
    parser.add_argument(
        "--catalog",
        type=str,
        default=None,
        help="Catalog URL or path to pre-load (http(s):// for HTTP, "
        "oras:// for OCI, or a local path). When given, the SELECT_CATALOG "
        "screen is skipped and the wizard jumps straight to SELECT_IMAGE "
        "with this catalog overlaying the local image-root (equivalent "
        "to picking ``[c]`` on the source screen and typing the URL). "
        "Ignored in server-driven mode (``--mac`` set) because the server "
        "supplies the catalog as part of /pxe/<mac>/plan.",
    )
    args = parser.parse_args(argv)

    # Lifecycle progress -- the launch path has two slow phases an
    # operator stares at without feedback otherwise:
    #
    #   1. ``from bty.tui._app import BtyTui`` (1-3s): pulls Rich
    #      + bty.catalog + bty.flash + bty.oras into the
    #      interpreter. On slower hardware (low-end mini-PCs, EPYC
    #      bringup boxes) this is several seconds of "blinking
    #      cursor".
    #   2. ``BtyTui(...).run()`` -> the wizard prints its first
    #      header (Rich is no-alt-screen, so prior stderr output
    #      stays visible above the header). On the live env's
    #      framebuffer console first print is typically under a
    #      second after import.
    #
    # Print progress to stderr BEFORE the import + the run. The
    # operator sees: wrapper banner (from /usr/local/sbin/bty-on-tty1
    # on the live env) -> these progress lines -> the bty header.
    # The blank-screen window narrows to a few hundred ms while
    # Rich's Console initialises.
    #
    # Also mirror to ``/run/bty.status`` so an operator who Alt-F2'd
    # to tty2 can ``cat`` it without having to read tty1's transient
    # output. ``/run`` is tmpfs on the live env so this is forgotten
    # on reboot; cheap to write.
    def _progress(msg: str) -> None:
        line = f"{prog}: {msg}"
        print(line, file=sys.stderr, flush=True)
        try:
            with open("/run/bty.status", "a") as f:
                f.write(line + "\n")
        except OSError:
            pass

    _progress(f"v{bty.__version__} starting...")
    _progress("loading UI dependencies (Rich)...")
    try:
        from bty.tui._app import BtyTui
    except ImportError as exc:
        print(
            f"{prog} {bty.__version__}: required dependency is not installed "
            f"({exc.name or exc}); reinstall with "
            '`pipx install "bty-lab[tui]"`',
            file=sys.stderr,
        )
        sys.exit(1)
    _progress("dependencies loaded")
    if args.mac:
        _progress(f"server-driven mode: server={args.server} mac={args.mac}")
    _progress("starting interface (first paint may take a few seconds)...")

    BtyTui(server=args.server, mac=args.mac, catalog=args.catalog).run()
