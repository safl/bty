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

    Resolves the same state dir the server uses (from
    ``BTY_STATE_DIR``), then writes / reads a v3 metadata-only
    inventory bundle. Server-free: needs only stdlib + ``bty.web``,
    so it works without the ``[web]`` extra.

    v0.33.2+: the bundle is just ``<dest>/inventory.json`` -- no
    image bytes. The image-store disk and the catalog handle bytes
    separately.
    """
    from datetime import UTC, datetime

    from bty.web import _portability
    from bty.web._db import default_state_path

    state_path = default_state_path()
    now = datetime.now(UTC).isoformat()

    if args.cmd == "export":
        exp = _portability.export_bundle(state_path, Path(args.dest), now=now)
        print(f"bty-web export -> {exp.dest}: {exp.machines} machines")
    else:  # import
        imp = _portability.import_bundle(state_path, Path(args.src), now=now)
        print(f"bty-web import: {imp.machines} machines (as bty-inventory)")
        for line in imp.skipped:
            print(f"  skipped: {line}", file=sys.stderr)


def _resolve_secret_key(state_dir: Path) -> str:
    """Return the per-server session-cookie secret.

    Read from ``$BTY_SESSION_SECRET`` if set + non-empty (CI tests,
    debugging); otherwise from ``<state_dir>/session-secret`` if it
    exists + non-empty. Otherwise generate a 32-byte URL-safe key,
    persist it under ``state_dir`` with mode 0640, and return it.

    An empty/whitespace value from either source is treated as
    "not set" and falls through to generation. A literal empty
    string would silently degrade ``SessionMiddleware``'s HMAC to a
    predictable signature -- forgeable session cookies on the LAN
    segment -- so we never let one through. Causes that produce an
    empty value in practice:

    - operator sets ``BTY_SESSION_SECRET=""`` thinking they're
      "clearing" the override
    - a half-written ``session-secret`` file from a crashed first
      boot (the prior implementation's ``Path.write_text`` wasn't
      atomic; a process kill between open and write left the file
      empty / truncated)
    - operator manually ``touch``-ed the file expecting it to be
      populated

    Persisting now writes through a same-dir tempfile + atomic
    rename so a crash mid-write either leaves the OLD file (if any)
    or no file (if first boot); never a truncated one. bty-web
    generates and persists this key on first start when neither the
    env var nor an existing file supplies one, so a fresh container
    or host install works without any pre-seeding step. Operators
    who want a fixed key across rebuilds can mount one in or set
    ``$BTY_SESSION_SECRET``.
    """
    env_key = (os.environ.get("BTY_SESSION_SECRET") or "").strip()
    if env_key:
        return env_key
    secret_path = state_dir / "session-secret"
    if secret_path.is_file():
        existing = secret_path.read_text(encoding="utf-8").strip()
        if existing:
            return existing
        # File present but empty / whitespace -- treat as missing
        # and regenerate atomically. The empty file gets overwritten
        # by the rename below.
    state_dir.mkdir(parents=True, exist_ok=True)
    key = secrets.token_urlsafe(32)
    # Atomic write: same-dir tempfile -> rename. Avoids a partially-
    # written file becoming the loaded secret across a crash; cross-
    # device renames aren't a concern because the tempfile shares
    # ``state_dir``.
    tmp = secret_path.with_name(f".{secret_path.name}.{secrets.token_hex(4)}.tmp")
    tmp.write_text(key + "\n", encoding="utf-8")
    tmp.chmod(0o640)
    tmp.replace(secret_path)
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
            "bty-web systemd unit (and the bty-web container) can supply\n"
            "values without command-line plumbing:\n\n"
            "  BTY_WEB_HOST          bind address (default 0.0.0.0)\n"
            "  BTY_WEB_PORT          bind port (default 8080; clamped to 1-65535)\n"
            "  BTY_STATE_DIR         state directory holding state.db /\n"
            "                        session-secret (default /var/lib/bty)\n"
            "  BTY_BOOT_DIR          directory of netboot artifacts (kernel /\n"
            "                        initrd / squashfs); default <BTY_STATE_DIR>/\n"
            "                        boot\n"
            "  BTY_BOOT_SEED_DIR     directory of baked bootstrap artifacts (the\n"
            "                        container image's custom ipxe.efi) copied\n"
            "                        into BTY_BOOT_DIR on startup when absent;\n"
            "                        unset on host/dev installs (no-op)\n"
            "  BTY_BOOT_RELEASE_REPO GitHub repo to fetch netboot artifacts +\n"
            "                        catalog.toml from (default safl/bty)\n"
            "  BTY_CATALOG_FILE      catalog.toml path (default <BTY_STATE_DIR>/\n"
            "                        catalog.toml)\n"
            "  BTY_MAX_UPLOAD_BYTES  cap on /boot upload body size in bytes\n"
            "                        (default 200 GiB; values <= 0 ignored)\n"
            "  BTY_SESSION_SECRET    override the persisted session-cookie key\n"
            "                        (default: read/create <BTY_STATE_DIR>/\n"
            "                        session-secret)\n"
            "  BTY_TRUSTED_PROXY     when set (any truthy value), read the\n"
            "                        client IP from X-Forwarded-For; only\n"
            "                        enable behind a reverse proxy that\n"
            "                        strips inbound X-Forwarded-For\n"
            "  BTY_BACKUP_DIR        directory scheduled / on-demand backups\n"
            "                        land in (default <BTY_STATE_DIR>/backups)\n"
            "  BTY_BACKUP_MAX_PARALLEL  max concurrent backup jobs (default 1;\n"
            "                        concurrent exports would race on dest dirs)\n"
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
    # per-machine hardware identity (mac + hw_lshw + known_disks) in/out
    # of a metadata-only inventory.json bundle, for migration + backup.
    # Image bytes are NOT included; rsync / disk-copy / catalog re-fetch
    # handles those.
    sub = parser.add_subparsers(dest="cmd")
    p_exp = sub.add_parser(
        "export",
        help="write a metadata-only inventory bundle (mac + lshw + known_disks)",
    )
    p_exp.add_argument("dest", help="bundle directory to create (holds inventory.json)")
    p_imp = sub.add_parser(
        "import",
        help="load an inventory bundle (machines arrive as bty-inventory)",
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
    boot_root_env = os.environ.get("BTY_BOOT_DIR")
    boot_root = Path(boot_root_env) if boot_root_env else None
    secret_key = _resolve_secret_key(state_path.parent)

    app = create_app(
        state_path=state_path,
        service_user=service_user,
        secret_key=secret_key,
        boot_root=boot_root,
    )

    host = os.environ.get("BTY_WEB_HOST", "0.0.0.0")
    raw_port = os.environ.get("BTY_WEB_PORT", "8080")
    try:
        port = int(raw_port)
    except ValueError:
        # Pre-1.0 policy: no read-side leniency for invalid config.
        # A typo'd BTY_WEB_PORT exits with a clear error rather than
        # silently binding 8080 -- the operator's intent isn't
        # "default" but "fix my typo".
        print(
            f"bty-web: BTY_WEB_PORT={raw_port!r} is not an integer; "
            f"set it to a number between 1 and 65535 (default 8080)",
            file=sys.stderr,
        )
        sys.exit(2)
    if not (1 <= port <= 65535):
        print(
            f"bty-web: BTY_WEB_PORT={port} is out of range (must be 1-65535; default 8080)",
            file=sys.stderr,
        )
        sys.exit(2)

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
