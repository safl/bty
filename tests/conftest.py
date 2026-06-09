"""Global pytest configuration.

Installs a default :class:`bty.web._config.LoadedConfig` before every
test so call sites that read ``cfg()`` work without each test having
to manually bootstrap one. Tests that want a bespoke config call
``set_active_config(load_config([custom.toml]))`` inside the test
body -- the autouse fixture's default install does NOT block that.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from bty.web import _config


@pytest.fixture(autouse=True)
def _default_active_config(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Install a default-only LoadedConfig before every test.

    The fixture clears any ``BTY_*`` env vars first so a host-level
    BTY_ADMIN_PASSWORD (e.g. operator's shell rc) can't leak into
    the test's view of the config. Tests that DO want env overrides
    re-set them via ``monkeypatch.setenv(...)`` inside the test body
    AND call ``reload_config()`` (below) for the change to land in
    the active config.
    """
    import os

    for k in list(os.environ):
        if k.startswith("BTY_"):
            monkeypatch.delenv(k, raising=False)
    _config.set_active_config(_config.load_config([]))
    yield
    # No explicit teardown -- the next test's setup overwrites the
    # singleton. Leaving _ACTIVE non-None between tests is harmless
    # (each test installs a fresh one).


def _reload() -> None:
    _config.set_active_config(_config.load_config(None))


@pytest.fixture
def reload_config():  # type: ignore[no-untyped-def]
    """Helper that re-loads the active config from the current
    environment. Use it after a ``monkeypatch.setenv("BTY_*", ...)``
    so the change shows up in subsequent ``cfg()`` reads.

    Usage::

        def test_foo(monkeypatch, reload_config):
            monkeypatch.setenv("BTY_TUNING_BACKUP_MAX_PARALLEL", "3")
            reload_config()
            assert _resolve_max_parallel() == 3
    """
    return _reload
