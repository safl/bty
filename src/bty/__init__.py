"""bty - flash images onto target disks, locally or remote with and without PXE.

Top-level package. Subpackages:

- ``bty.cli`` - main command-line interface (image inspection,
  catalog management, flashing).
- ``bty.tui`` - terminal UI; requires the ``[tui]`` extra.
- ``bty.web`` - HTTP server with browser UI; requires the ``[web]`` extra.
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
