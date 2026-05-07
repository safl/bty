"""``bty-ctl`` - command-line client for a remote bty-web server.

Sibling to the local-flashing ``bty`` tool. The split mirrors how
``git`` and ``kubectl`` separate local-state operations from
remote-API operations: ``bty list / inspect / flash`` work on the
host they're invoked from, while ``bty-ctl login / logout / ...``
talk to a bty-web server over HTTP.

Subcommands:

    bty-ctl login [--server URL] [--password-stdin] [--token-out PATH]
    bty-ctl logout [--server URL] [--token-file PATH]

Auth is OS-PAM against the bty-web service user's password (the
account bty-web is running as on the server). On success, ``login``
saves the returned session token to ``~/.config/bty/token`` (mode
0600); subsequent invocations reuse it without re-prompting.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

import bty


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="bty-ctl",
        description="bty-ctl - command-line client for a bty-web server",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"bty-ctl {bty.__version__}",
    )
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    p_login = sub.add_parser(
        "login",
        help="acquire a bty-web session token via PAM",
        description=(
            "POST /auth/login on the bty-web server with the OS password of "
            "the service user (the account bty-web runs as). On success the "
            "returned token is saved to ~/.config/bty/token (mode 0600) so "
            "subsequent invocations can reuse it without re-prompting."
        ),
    )
    p_login.add_argument(
        "--server",
        default=os.environ.get("BTY_SERVER", "http://localhost:8080"),
        help="base URL of the bty-web server (default: $BTY_SERVER or %(default)s)",
    )
    p_login.add_argument(
        "--password-stdin",
        action="store_true",
        help="read the password from stdin instead of prompting interactively",
    )
    p_login.add_argument(
        "--label",
        default=None,
        help="optional label stored on the session row (e.g. 'alice@laptop')",
    )
    p_login.add_argument(
        "--token-out",
        type=Path,
        default=None,
        help=(
            "path to write the session token to (mode 0600). default: "
            "~/.config/bty/token. Use '-' to print to stdout instead."
        ),
    )
    p_login.set_defaults(func=cmd_login)

    p_logout = sub.add_parser(
        "logout",
        help="revoke the saved bty-web session token",
        description=(
            "POST /auth/logout on the bty-web server with the saved token, "
            "then delete the local token file."
        ),
    )
    p_logout.add_argument(
        "--server",
        default=os.environ.get("BTY_SERVER", "http://localhost:8080"),
        help="base URL of the bty-web server (default: $BTY_SERVER or %(default)s)",
    )
    p_logout.add_argument(
        "--token-file",
        type=Path,
        default=None,
        help="path of the saved session token (default: ~/.config/bty/token)",
    )
    p_logout.set_defaults(func=cmd_logout)

    args = parser.parse_args(argv)
    func = getattr(args, "func", None)
    if func is None:
        parser.print_help()
        return 2
    result = func(args)
    return int(result or 0)


def default_token_path() -> Path:
    """Resolve the conventional client-side token cache path."""
    return Path.home() / ".config" / "bty" / "token"


def cmd_login(args: argparse.Namespace) -> int:
    """POST /auth/login, save the returned token to a local file."""
    if args.password_stdin:
        password = sys.stdin.readline().rstrip("\n")
    else:
        password = getpass.getpass(f"bty-web password ({args.server}): ")
    if not password:
        print("bty-ctl: no password provided; aborting", file=sys.stderr)
        return 1

    body = json.dumps({"password": password, "label": args.label}).encode("utf-8")
    url = f"{args.server.rstrip('/')}/auth/login"
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            print("bty-ctl: invalid credentials", file=sys.stderr)
            return 1
        print(f"bty-ctl: login failed: HTTP {exc.code} {exc.reason}", file=sys.stderr)
        return 1
    except urllib.error.URLError as exc:
        print(f"bty-ctl: cannot reach {url}: {exc.reason}", file=sys.stderr)
        return 1

    token = payload["token"]
    if args.token_out is not None and str(args.token_out) == "-":
        print(token)
        return 0
    out_path = args.token_out or default_token_path()
    out_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    out_path.write_text(token + "\n")
    out_path.chmod(0o600)
    print(f"bty-ctl: saved session token to {out_path} (expires {payload['expires_at']})")
    return 0


def cmd_logout(args: argparse.Namespace) -> int:
    """POST /auth/logout to revoke the saved token, then delete the file."""
    token_file = args.token_file or default_token_path()
    if not token_file.is_file():
        print(f"bty-ctl: no token file at {token_file}; nothing to do")
        return 0
    token = token_file.read_text().strip()
    if not token:
        token_file.unlink(missing_ok=True)
        print(f"bty-ctl: empty token file {token_file} removed")
        return 0
    url = f"{args.server.rstrip('/')}/auth/logout"
    req = urllib.request.Request(
        url,
        method="POST",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15):
            pass
    except urllib.error.HTTPError as exc:
        # 401 usually means the server already considered the token
        # invalid (expired / revoked elsewhere) - still proceed to
        # delete the local file.
        if exc.code != 401:
            print(f"bty-ctl: logout failed: HTTP {exc.code} {exc.reason}", file=sys.stderr)
            return 1
    except urllib.error.URLError as exc:
        print(f"bty-ctl: cannot reach {url}: {exc.reason}", file=sys.stderr)
        return 1
    token_file.unlink(missing_ok=True)
    print(f"bty-ctl: removed {token_file}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
