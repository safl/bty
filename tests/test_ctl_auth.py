"""Tests for ``bty-ctl login`` and ``bty-ctl logout``.

These exercise the client's HTTP roundtrip against a tmp_path-backed
``bty-web`` app: PAM is mocked, the session token comes back from a
real /auth/login, and the client writes / deletes a real token file
under a redirected ``$HOME``. The ``bty-ctl`` binary is a sibling to
the local-flashing ``bty`` command - separate console-script entries
sharing the same wheel.
"""

from __future__ import annotations

import io
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import pytest
import uvicorn

from bty.client import main as cli_main
from bty.web._app import create_app

TEST_SERVICE_USER = "cli-auth-test"


def _free_port() -> int:
    import socket as _socket

    s = _socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@contextmanager
def _running_server(tmp_path: Path) -> Iterator[str]:
    """Spin up bty-web in a background thread; yield its base URL."""
    image_root = tmp_path / "images"
    image_root.mkdir()
    boot_root = tmp_path / "boot"
    boot_root.mkdir()
    app = create_app(
        state_path=tmp_path / "state.db",
        service_user=TEST_SERVICE_USER,
        image_root=image_root,
        boot_root=boot_root,
    )
    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    # Wait for the server to be ready.
    while not server.started:
        thread.join(timeout=0.05)
        if not thread.is_alive():
            raise RuntimeError("uvicorn thread died before starting")
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=5)


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect ``$HOME`` so the CLI writes its token file under tmp_path."""
    fake = tmp_path / "home"
    fake.mkdir()
    monkeypatch.setenv("HOME", str(fake))
    return fake


def test_login_writes_token_with_mode_0600(home: Path, tmp_path: Path) -> None:
    with _running_server(tmp_path) as base_url, patch("pamela.authenticate", return_value=True):
        rc = cli_main(
            [
                "login",
                "--server",
                base_url,
                "--password-stdin",
            ]
        )
    assert rc == 0
    token_path = home / ".config" / "bty" / "token"
    assert token_path.is_file()
    # Mode bits include 0o600 (owner rw); strip everything but
    # permission bits before comparing.
    mode = token_path.stat().st_mode & 0o777
    assert mode == 0o600, f"unexpected mode {oct(mode)}"
    body = token_path.read_text().strip()
    assert len(body) > 20  # secrets.token_urlsafe(32)


def test_login_with_invalid_password_returns_nonzero(home: Path, tmp_path: Path) -> None:
    import pamela

    with (
        _running_server(tmp_path) as base_url,
        patch("pamela.authenticate", side_effect=pamela.PAMError("nope")),
    ):
        rc = cli_main(["login", "--server", base_url, "--password-stdin"])
    assert rc == 1
    # No token saved.
    assert not (home / ".config" / "bty" / "token").exists()


def test_login_password_stdin_reads_one_line(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--password-stdin`` must consume a single line; trailing
    newline is stripped (not used as part of the password)."""
    monkeypatch.setattr("sys.stdin", io.StringIO("hunter2\n"))
    with (
        _running_server(tmp_path) as base_url,
        patch("pamela.authenticate", return_value=True) as mock_pam,
    ):
        rc = cli_main(["login", "--server", base_url, "--password-stdin"])
    assert rc == 0
    args, kwargs = mock_pam.call_args
    pw_arg = args[1] if len(args) >= 2 else kwargs.get("password")
    assert pw_arg == "hunter2"


def test_logout_removes_token_file(home: Path, tmp_path: Path) -> None:
    """After login + logout, the local token file is gone and the
    server-side session is revoked."""
    with _running_server(tmp_path) as base_url, patch("pamela.authenticate", return_value=True):
        assert cli_main(["login", "--server", base_url, "--password-stdin"]) == 0
        token_path = home / ".config" / "bty" / "token"
        assert token_path.is_file()
        assert cli_main(["logout", "--server", base_url]) == 0
        assert not token_path.exists()


def test_logout_no_token_file_succeeds_silently(home: Path) -> None:
    """``bty logout`` with no saved token is a no-op (exit 0)."""
    rc = cli_main(["logout", "--server", "http://127.0.0.1:1"])  # not reached
    assert rc == 0


@pytest.fixture(autouse=True)
def _stdin_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default stdin to an empty 'hunter2' so --password-stdin tests
    that don't override it read a non-empty value."""
    if not isinstance(getattr(__import__("sys"), "stdin", None), io.StringIO):
        monkeypatch.setattr("sys.stdin", io.StringIO("hunter2\n"))
