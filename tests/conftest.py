"""Shared pytest fixtures.

The ``_isolate_system_bri_root`` autouse fixture defends every test
against a bty package that's been pip-installed system-wide (or any
other source of ``/usr/share/bty/bri/*.bri``): without it,
``list_all_remote_images`` would silently include the host's
descriptors and tests asserting "exactly N rows" would flake on
machines where N != 0. Tests that *want* to exercise the system
root override the env var explicitly via
``monkeypatch.setenv("BTY_SYSTEM_BRI_ROOT", ...)``.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_system_bri_root(
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Point ``BTY_SYSTEM_BRI_ROOT`` at a guaranteed-empty directory
    for every test. ``system_bri_root`` returns ``None`` when its
    target doesn't exist; we pick an existing-but-empty tmp dir so
    behaviour is "system root configured, no entries" -- exercises
    the same code path as production with a clean baseline.
    """
    empty = tmp_path_factory.mktemp("system_bri_root_empty_session")
    monkeypatch.setenv("BTY_SYSTEM_BRI_ROOT", str(empty))
