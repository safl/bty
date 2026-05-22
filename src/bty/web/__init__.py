"""bty.web - HTTP server with browser UI for fleet image flashing.

This module is intentionally lightweight: it imports nothing from
:mod:`fastapi` or :mod:`uvicorn` at module level so a minimal install
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


def _run_portability(args: argparse.Namespace) -> None:
    """Dispatch the ``export`` / ``import`` subcommands.

    Resolves the same state dir + image root the server uses (from
    ``BTY_STATE_DIR`` / ``BTY_IMAGE_ROOT``), then moves the
    operator-owned state in/out of a bundle directory. Server-free: needs
    only stdlib + ``bty.web``, so it works without the ``[web]`` extra.
    """
    from datetime import UTC, datetime

    from bty.web import _portability
    from bty.web._db import default_state_path

    state_path = default_state_path()
    image_root_env = os.environ.get("BTY_IMAGE_ROOT")
    image_root = Path(image_root_env) if image_root_env else state_path.parent / "images"
    now = datetime.now(UTC).isoformat()

    if args.cmd == "export":
        exp = _portability.export_bundle(
            state_path, image_root, Path(args.dest), bty_version=bty.__version__, now=now
        )
        print(
            f"bty-web export -> {exp.dest}: {exp.machines} machines, "
            f"{exp.catalog_entries} catalog entries, {exp.images} image files"
        )
    else:  # import
        imp = _portability.import_bundle(state_path, image_root, Path(args.src), now=now)
        print(
            f"bty-web import: {imp.machines} machines (as bty-inventory), "
            f"{imp.catalog_entries} catalog entries, {imp.images} image files"
        )
        for line in imp.skipped:
            print(f"  skipped: {line}", file=sys.stderr)


def _resolve_secret_key(state_dir: Path) -> str:
    """Return the per-appliance session-cookie secret.

    Read from ``$BTY_SESSION_SECRET`` if set (CI tests, debugging);
    otherwise from ``<state_dir>/session-secret``. If neither exists,
    generate a 32-byte URL-safe key, persist it under ``state_dir``
    with mode 0640, and return it. The appliance pre-creates
    this file in ``bty-web-init``; this fallback covers fresh dev /
    local installs where bty-web is launched without that step.
    """
    env_key = os.environ.get("BTY_SESSION_SECRET")
    if env_key:
        return env_key
    secret_path = state_dir / "session-secret"
    if secret_path.exists():
        return secret_path.read_text(encoding="utf-8").strip()
    state_dir.mkdir(parents=True, exist_ok=True)
    key = secrets.token_urlsafe(32)
    secret_path.write_text(key + "\n", encoding="utf-8")
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
        description=(
            "bty-web: HTTP server with browser UI for fleet image flashing.\n\n"
            "All runtime configuration is read from the environment so the\n"
            "appliance's systemd unit (and the bty-web container) can supply\n"
            "values without command-line plumbing:\n\n"
            "  BTY_WEB_HOST          bind address (default 0.0.0.0)\n"
            "  BTY_WEB_PORT          bind port (default 8080; clamped to 1-65535)\n"
            "  BTY_STATE_DIR         state directory holding state.db / cache /\n"
            "                        session-secret (default /var/lib/bty)\n"
            "  BTY_IMAGE_ROOT        directory of operator-uploaded images\n"
            "                        (default /var/lib/bty/images)\n"
            "  BTY_BOOT_DIR          directory of netboot artifacts (kernel /\n"
            "                        initrd / squashfs); default <BTY_STATE_DIR>/\n"
            "                        boot\n"
            "  BTY_BOOT_RELEASE_REPO GitHub repo to fetch netboot artifacts +\n"
            "                        catalog.toml from (default safl/bty)\n"
            "  BTY_CATALOG_FILE      catalog.toml path (default <BTY_STATE_DIR>/\n"
            "                        catalog.toml)\n"
            "  BTY_SESSION_SECRET    override the persisted session-cookie key\n"
            "                        (default: read/create <BTY_STATE_DIR>/\n"
            "                        session-secret)\n"
            "  BTY_MAX_UPLOAD_BYTES  cap on image-upload body size in bytes\n"
            "                        (default 200 GiB; values <= 0 ignored)\n"
            "  BTY_TRUSTED_PROXY     when set (any truthy value), read the\n"
            "                        client IP from X-Forwarded-For; only\n"
            "                        enable behind a reverse proxy that\n"
            "                        strips inbound X-Forwarded-For\n"
            "  BTY_CATALOG_CACHE_DIR image cache directory (default\n"
            "                        <BTY_STATE_DIR>/cache)\n"
            "  BTY_CATALOG_MAX_PARALLEL  max concurrent catalog downloads\n"
            "                        (default 2)\n"
            "  BTY_HASH_MAX_PARALLEL max concurrent image hash jobs\n"
            "                        (default 1)\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"bty-web {bty.__version__}",
    )
    # Subcommands. Bare ``bty-web`` (no subcommand) still runs the server
    # -- the subparser is optional so the systemd unit / container
    # entrypoint is unchanged. ``export`` / ``import`` move the
    # operator-owned half of the state (machines + catalog + image files)
    # in/out of a portable bundle directory, for migration + backup.
    sub = parser.add_subparsers(dest="cmd")
    p_exp = sub.add_parser(
        "export",
        help="write machines + catalog + image files to a bundle directory",
    )
    p_exp.add_argument("dest", help="bundle directory to create/write")
    p_imp = sub.add_parser(
        "import",
        help="load a bundle directory (machines come in as bty-inventory)",
    )
    p_imp.add_argument("src", help="bundle directory to read")
    args = parser.parse_args(argv)

    if args.cmd in ("export", "import"):
        _run_portability(args)
        return

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
    raw_port = os.environ.get("BTY_WEB_PORT", "8080")
    try:
        port = int(raw_port)
    except ValueError:
        # A typo'd BTY_WEB_PORT used to crash bty-web at start with
        # an unhelpful ValueError traceback. Emit a clear systemd-
        # journal-readable error and fall back to the default port.
        print(
            f"bty-web: BTY_WEB_PORT={raw_port!r} is not an integer; falling back to 8080",
            file=sys.stderr,
        )
        port = 8080
    if not (1 <= port <= 65535):
        print(
            f"bty-web: BTY_WEB_PORT={port} is out of range (1-65535); falling back to 8080",
            file=sys.stderr,
        )
        port = 8080

    # ``timeout_graceful_shutdown`` bounds how long uvicorn waits
    # for in-flight requests to drain on SIGTERM. SSE streams used
    # to hold the worker until systemd's 90s SIGKILL fired on every
    # restart -- the lifespan-driven bus.close() in v0.19.1 fixes
    # that, but we set a hard 10s upper bound here so a bug in any
    # future stream handler can't bring the timeout back.
    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level="info",
        timeout_graceful_shutdown=10,
    )
