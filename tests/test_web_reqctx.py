"""Tests for ``bty.web._reqctx.client_ip``.

Covers the X-Forwarded-For + trusted-proxy branches that weren't
exercised elsewhere. The audit-log correctness of the client IP
depends on getting these right.
"""

from __future__ import annotations

import types

import pytest

from bty.web import _config, _reqctx


class _StubHeaders:
    """Case-insensitive ``.get`` matching Starlette's Headers type."""

    def __init__(self, mapping: dict[str, str]):
        self._m = {k.lower(): v for k, v in mapping.items()}

    def get(self, key: str, default=None):
        return self._m.get(key.lower(), default)


def _fake_request(*, xff: str | None, client_host: str | None = "10.0.0.99"):
    return types.SimpleNamespace(
        headers=_StubHeaders({"x-forwarded-for": xff} if xff is not None else {}),
        client=types.SimpleNamespace(host=client_host) if client_host else None,
    )


@pytest.fixture
def trusted_proxy_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the trusted_proxy toggle to True for the test."""

    def _cfg():
        return types.SimpleNamespace(
            server=types.SimpleNamespace(trusted_proxy=True),
        )

    monkeypatch.setattr(_config, "cfg", _cfg)


@pytest.fixture
def trusted_proxy_off(monkeypatch: pytest.MonkeyPatch) -> None:
    def _cfg():
        return types.SimpleNamespace(
            server=types.SimpleNamespace(trusted_proxy=False),
        )

    monkeypatch.setattr(_config, "cfg", _cfg)


def test_client_ip_returns_socket_when_trusted_proxy_off(trusted_proxy_off) -> None:
    """Even with X-F-F set, an untrusted-proxy deploy returns the
    socket IP so a spoofed header can't rewrite the audit log."""
    r = _fake_request(xff="1.2.3.4", client_host="10.0.0.99")
    assert _reqctx.client_ip(r) == "10.0.0.99"


def test_client_ip_returns_xff_leftmost_when_trusted(trusted_proxy_on) -> None:
    r = _fake_request(xff="1.2.3.4, 10.0.0.1", client_host="10.0.0.99")
    assert _reqctx.client_ip(r) == "1.2.3.4"


def test_client_ip_falls_back_to_socket_when_xff_absent(trusted_proxy_on) -> None:
    """trusted_proxy=True but no X-F-F header (direct connection to
    bty-web) -> socket IP, not None."""
    r = _fake_request(xff=None, client_host="10.0.0.99")
    assert _reqctx.client_ip(r) == "10.0.0.99"


def test_client_ip_falls_back_to_socket_when_xff_empty(trusted_proxy_on) -> None:
    """Empty X-F-F header -> fall through to socket IP."""
    r = _fake_request(xff="", client_host="10.0.0.99")
    assert _reqctx.client_ip(r) == "10.0.0.99"


def test_client_ip_handles_leading_empty_xff_entry(trusted_proxy_on) -> None:
    """``,10.0.0.1`` (empty leftmost) -> ``first`` is empty -> fall
    through to socket IP rather than storing '' as the client IP."""
    r = _fake_request(xff=",10.0.0.1", client_host="10.0.0.99")
    assert _reqctx.client_ip(r) == "10.0.0.99"


def test_client_ip_returns_none_when_no_client_and_no_xff(trusted_proxy_on) -> None:
    """ASGI test-client requests can have request.client=None; the
    normaliser handles None cleanly (returns None)."""
    r = _fake_request(xff=None, client_host=None)
    assert _reqctx.client_ip(r) is None


def test_client_ip_normalises_v4_mapped_v6_socket(trusted_proxy_off) -> None:
    """``::ffff:1.2.3.4`` from a v4-over-v6 socket collapses to bare
    v4 so the same client doesn't show up as two audit rows."""
    r = _fake_request(xff=None, client_host="::ffff:1.2.3.4")
    assert _reqctx.client_ip(r) == "1.2.3.4"
