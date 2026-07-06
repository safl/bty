"""Unit tests for :mod:`bty.web._withcache_catalog`.

Covers the in-process cache + the withcache HTTP client wiring.
Uses monkeypatched ``withcache.client`` methods so no real HTTP
call leaves the test.
"""

from __future__ import annotations

from typing import Any

import pytest

from bty.web._withcache_catalog import WithcacheCatalog


def test_unconfigured_reports_last_error() -> None:
    cat = WithcacheCatalog(withcache_url=None)
    assert cat.configured is False
    assert cat.entries == []
    assert cat.last_error == "withcache URL not configured"


def test_unconfigured_refresh_is_noop() -> None:
    cat = WithcacheCatalog(withcache_url=None)
    cat.refresh()
    assert cat.entries == []


def test_unconfigured_add_raises() -> None:
    """Since v0.67.1 the local-only fallback for add() is gone: the
    catalog is single-sourced from withcache, so a standalone bty
    deploy has nowhere to persist a new entry. Refusing at the
    write path (rather than silently landing in an in-memory cache
    that a restart would drop) matches the operator's mental
    model."""
    cat = WithcacheCatalog(withcache_url=None)
    with pytest.raises(RuntimeError, match="withcache URL not configured"):
        cat.add({"name": "demo", "src": "https://example/demo.img.gz"})


def test_unconfigured_delete_raises() -> None:
    """Symmetric with add: delete refuses when withcache isn't
    configured. Callers previously used delete on the local cache;
    they now hit withcache's own DELETE endpoint or run against a
    configured deploy."""
    cat = WithcacheCatalog(withcache_url=None)
    with pytest.raises(RuntimeError, match="withcache URL not configured"):
        cat.delete("demo")


def test_configured_url_normalises_whitespace() -> None:
    cat = WithcacheCatalog(withcache_url="  https://cache/  ")
    assert cat.configured is True
    assert cat.withcache_url == "https://cache/"


def test_set_url_clears_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    cat = WithcacheCatalog(withcache_url="https://cache-a")

    def _fake_list(server: str, **_: Any) -> dict[str, Any]:
        return {
            "entries": [{"name": "demo", "src": "https://example/demo.img.gz"}],
            "fetched_at": "2026-07-06T00:00:00Z",
            "last_error": None,
        }

    monkeypatch.setattr("bty.web._withcache_catalog._wc_client.list_catalog", _fake_list)
    cat.refresh()
    assert len(cat.entries) == 1

    cat.set_withcache_url("https://cache-b")
    assert cat.entries == []
    assert cat.withcache_url == "https://cache-b"


def test_refresh_populates_entries_and_computes_ref(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every entry gets a ``bty_image_ref`` computed from ``src``. That
    ref is what machine rows key on, so the catalog's read consumers
    look entries up by ref rather than by name."""

    def _fake_list(server: str, **_: Any) -> dict[str, Any]:
        return {
            "entries": [
                {
                    "name": "demo",
                    "src": "https://example/demo.img.gz",
                    "format": "img.gz",
                    "sha256": "a" * 64,
                    "size_bytes": 1024,
                    "description": "demo",
                },
                {
                    "name": "second",
                    "src": "https://example/second.img.gz",
                },
            ],
            "fetched_at": "2026-07-06T00:00:00Z",
            "last_error": None,
        }

    monkeypatch.setattr("bty.web._withcache_catalog._wc_client.list_catalog", _fake_list)
    cat = WithcacheCatalog(withcache_url="https://cache")
    cat.refresh()

    entries = cat.entries
    assert len(entries) == 2
    assert entries[0]["name"] == "demo"
    assert entries[0]["bty_image_ref"], "ref should be computed"
    assert cat.get_by_ref(entries[0]["bty_image_ref"]) == entries[0]
    assert cat.get_by_name("demo") == entries[0]
    assert cat.get_by_name("second") == entries[1]
    assert cat.fetched_at == "2026-07-06T00:00:00Z"
    assert cat.last_error is None


def test_refresh_skips_entries_without_src(monkeypatch: pytest.MonkeyPatch) -> None:
    """Entries whose ``src`` is missing / non-string are dropped rather
    than crashing the whole refresh -- the withcache-side add would
    normally reject these, so a hit here means an operator hand-
    edited catalog.toml."""

    def _fake_list(server: str, **_: Any) -> dict[str, Any]:
        return {
            "entries": [
                {"name": "good", "src": "https://example/good.img.gz"},
                {"name": "no-src"},  # dropped
                {"name": "bad-src", "src": 42},  # dropped
            ],
            "fetched_at": None,
            "last_error": None,
        }

    monkeypatch.setattr("bty.web._withcache_catalog._wc_client.list_catalog", _fake_list)
    cat = WithcacheCatalog(withcache_url="https://cache")
    cat.refresh()
    assert [e["name"] for e in cat.entries] == ["good"]


def test_refresh_transport_failure_preserves_prior_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the withcache round-trip fails, the previous cache is kept
    and ``last_error`` records the failure so the UI can surface a
    stale-data pill."""
    calls = {"n": 0}

    def _fake_list(server: str, **_: Any) -> dict[str, Any]:
        calls["n"] += 1
        if calls["n"] == 1:
            return {
                "entries": [{"name": "demo", "src": "https://example/demo.img.gz"}],
                "fetched_at": "2026-07-06T00:00:00Z",
                "last_error": None,
            }
        from withcache import client as _wc_client

        raise _wc_client.WithcacheError("boom")

    monkeypatch.setattr("bty.web._withcache_catalog._wc_client.list_catalog", _fake_list)
    cat = WithcacheCatalog(withcache_url="https://cache")
    cat.refresh()
    assert len(cat.entries) == 1

    cat.refresh()  # transport failure
    assert len(cat.entries) == 1, "prior cache preserved"
    assert cat.last_error == "boom"


def test_add_posts_and_refreshes(monkeypatch: pytest.MonkeyPatch) -> None:
    posted: dict[str, Any] = {}

    def _fake_add(server: str, entry: dict[str, Any], **_: Any) -> dict[str, Any]:
        posted.update(entry)
        return entry

    def _fake_list(server: str, **_: Any) -> dict[str, Any]:
        return {
            "entries": [posted] if posted else [],
            "fetched_at": None,
            "last_error": None,
        }

    monkeypatch.setattr("bty.web._withcache_catalog._wc_client.add_catalog_entry", _fake_add)
    monkeypatch.setattr("bty.web._withcache_catalog._wc_client.list_catalog", _fake_list)
    cat = WithcacheCatalog(withcache_url="https://cache")
    cat.add({"name": "demo", "src": "https://example/demo.img.gz"})
    assert posted["name"] == "demo"
    assert len(cat.entries) == 1


def test_delete_calls_client_and_refreshes(monkeypatch: pytest.MonkeyPatch) -> None:
    deleted: list[str] = []

    def _fake_delete(server: str, name: str, **_: Any) -> None:
        deleted.append(name)

    def _fake_list(server: str, **_: Any) -> dict[str, Any]:
        return {"entries": [], "fetched_at": None, "last_error": None}

    monkeypatch.setattr("bty.web._withcache_catalog._wc_client.delete_catalog_entry", _fake_delete)
    monkeypatch.setattr("bty.web._withcache_catalog._wc_client.list_catalog", _fake_list)
    cat = WithcacheCatalog(withcache_url="https://cache")
    cat.delete("demo")
    assert deleted == ["demo"]
    assert cat.entries == []
