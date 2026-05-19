"""bty - flash images onto target disks, offline or networked with and without PXE.

Top-level package. Subpackages:

- ``bty.tui`` - the operator-facing wizard / scripted flasher
  (``bty`` console script); requires the ``[tui]`` extra.
- ``bty.web`` - HTTP server with browser UI (``bty-web`` console
  script); requires the ``[web]`` extra.
- ``bty.flash`` / ``bty.images`` / ``bty.catalog`` / ``bty.disks``
  / ``bty.oras`` - the library modules ``bty.tui`` + ``bty.web``
  share.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version

try:
    __version__ = _pkg_version("bty-lab")
except PackageNotFoundError:
    # The package is not installed (e.g. running directly from a
    # source checkout without ``uv sync``). Fall back to a sentinel
    # so ``bty.__version__`` is always a string.
    __version__ = "0.0.0.dev0+unknown"
