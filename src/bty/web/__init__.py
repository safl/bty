"""bty.web — HTTP server with browser UI for fleet provisioning.

This module is intentionally lightweight: it imports nothing from
:mod:`fastapi` or :mod:`uvicorn` at module level so a CLI-only install
(``pipx install bty-lab`` without the ``[web]`` extra) can still
``import bty.web`` for introspection without crashing. The actual
FastAPI app lives in :mod:`bty.web._app`, which is loaded only when
``bty-web`` is invoked.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import bty


def main() -> None:
    """Console-script entry point for ``bty-web``.

    Defers loading the FastAPI app until invocation time so a missing
    ``[web]`` extra produces a clear "reinstall with extras" message
    rather than a raw ``ModuleNotFoundError``. Refuses to start if
    ``BTY_WEB_TOKEN`` is unset (fails closed).
    """
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

    token = os.environ.get("BTY_WEB_TOKEN")
    if not token:
        print(
            "bty-web: BTY_WEB_TOKEN is not set; refusing to start.\n"
            "  Generate one with: "
            "python -c 'import secrets; print(secrets.token_urlsafe(32))'\n"
            "  Then export it: export BTY_WEB_TOKEN='<the-token>'",
            file=sys.stderr,
        )
        sys.exit(1)

    state_path = default_state_path()
    image_root_env = os.environ.get("BTY_IMAGE_ROOT")
    image_root = Path(image_root_env) if image_root_env else None
    boot_root_env = os.environ.get("BTY_BOOT_DIR")
    boot_root = Path(boot_root_env) if boot_root_env else None

    app = create_app(
        state_path=state_path,
        bearer_token=token,
        image_root=image_root,
        boot_root=boot_root,
    )

    host = os.environ.get("BTY_WEB_HOST", "0.0.0.0")
    port = int(os.environ.get("BTY_WEB_PORT", "8080"))

    import uvicorn

    uvicorn.run(app, host=host, port=port, log_level="info")
