"""bty.web - HTTP server with browser UI for fleet provisioning.

This module is intentionally lightweight: it imports nothing from
:mod:`fastapi` or :mod:`uvicorn` at module level so a CLI-only install
(``pipx install bty-lab`` without the ``[web]`` extra) can still
``import bty.web`` for introspection without crashing. The actual
FastAPI app lives in :mod:`bty.web._app`, which is loaded only when
``bty-web`` is invoked.
"""

from __future__ import annotations

import argparse
import os
import pwd
import sys
from pathlib import Path

import bty


def main(argv: list[str] | None = None) -> None:
    """Console-script entry point for ``bty-web``.

    Defers loading the FastAPI app until invocation time so a missing
    ``[web]`` extra produces a clear "reinstall with extras" message
    rather than a raw ``ModuleNotFoundError``. The service user is
    captured from ``geteuid`` and used as the principal whose OS
    password gates ``/auth/login``.
    """
    parser = argparse.ArgumentParser(
        prog="bty-web",
        description="bty-web - HTTP server with browser UI for fleet provisioning",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"bty-web {bty.__version__}",
    )
    parser.parse_args(argv)

    try:
        import uvicorn

        from bty.web._app import create_app
        from bty.web._db import default_state_path
    except ImportError as exc:
        print(
            f"bty-web {bty.__version__}: required dependency is not installed "
            f"({exc.name or exc}); reinstall with "
            '`pipx install "bty-lab[web]"`',
            file=sys.stderr,
        )
        sys.exit(1)

    service_user = pwd.getpwuid(os.geteuid()).pw_name

    state_path = default_state_path()
    image_root_env = os.environ.get("BTY_IMAGE_ROOT")
    image_root = Path(image_root_env) if image_root_env else None
    boot_root_env = os.environ.get("BTY_BOOT_DIR")
    boot_root = Path(boot_root_env) if boot_root_env else None

    app = create_app(
        state_path=state_path,
        service_user=service_user,
        image_root=image_root,
        boot_root=boot_root,
    )

    host = os.environ.get("BTY_WEB_HOST", "0.0.0.0")
    port = int(os.environ.get("BTY_WEB_PORT", "8080"))

    uvicorn.run(app, host=host, port=port, log_level="info")
