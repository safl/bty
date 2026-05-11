"""bty.web - HTTP server with browser UI for fleet image flashing.

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
import secrets
import sys
from pathlib import Path

import bty


def _resolve_secret_key(state_dir: Path) -> str:
    """Return the per-appliance session-cookie secret.

    Read from ``$BTY_SESSION_SECRET`` if set (CI tests, debugging);
    otherwise from ``<state_dir>/session-secret``. If neither exists,
    generate a 32-byte URL-safe key, persist it under ``state_dir``
    with mode 0640, and return it. The cooked appliance pre-creates
    this file in ``bty-web-init``; this fallback covers fresh dev /
    local installs where bty-web is launched without that step.
    """
    env_key = os.environ.get("BTY_SESSION_SECRET")
    if env_key:
        return env_key
    secret_path = state_dir / "session-secret"
    if secret_path.exists():
        return secret_path.read_text().strip()
    state_dir.mkdir(parents=True, exist_ok=True)
    key = secrets.token_urlsafe(32)
    secret_path.write_text(key + "\n")
    secret_path.chmod(0o640)
    return key


def main(argv: list[str] | None = None) -> None:
    """Console-script entry point for ``bty-web``.

    Defers loading the FastAPI app until invocation time so a missing
    ``[web]`` extra produces a clear "reinstall with extras" message
    rather than a raw ``ModuleNotFoundError``. The service user is
    captured from ``geteuid`` and used as the principal whose OS
    password gates ``/ui/login``.
    """
    parser = argparse.ArgumentParser(
        prog="bty-web",
        description="bty-web - HTTP server with browser UI for fleet image flashing",
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
    secret_key = _resolve_secret_key(state_path.parent)

    app = create_app(
        state_path=state_path,
        service_user=service_user,
        secret_key=secret_key,
        image_root=image_root,
        boot_root=boot_root,
    )

    host = os.environ.get("BTY_WEB_HOST", "0.0.0.0")
    port = int(os.environ.get("BTY_WEB_PORT", "8080"))

    uvicorn.run(app, host=host, port=port, log_level="info")
