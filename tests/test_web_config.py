"""Tests for ``bty.web._config``.

Covers the layered config loader: defaults -> TOML -> env, with
provenance tracking + multi-file merge semantics.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from bty.web._config import (
    DEFAULT_ADMIN_PASSWORD,
    load_config,
    save_value,
)


def _toml(tmp_path: Path, name: str, body: str) -> Path:
    p = tmp_path / name
    p.write_text(dedent(body), encoding="utf-8")
    return p


# ---------- defaults ---------------------------------------------------------


def test_no_paths_no_env_returns_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty path list + no overriding env yields the built-in
    defaults and every key sourced as ``"default"``."""
    for k in list(os.environ.keys()) if (os := __import__("os")) else []:
        if k.startswith("BTY_"):
            monkeypatch.delenv(k, raising=False)
    r = load_config([])
    assert r.cfg.admin.password == DEFAULT_ADMIN_PASSWORD
    assert r.cfg.server.host == "0.0.0.0"
    assert r.cfg.server.port == 8080
    assert r.cfg.paths.state_dir == "/var/lib/bty"
    # Every key carries a provenance entry, even the unset-at-default ones.
    assert r.sources["admin.password"] == "default"
    assert r.sources["server.host"] == "default"
    assert r.sources["server.port"] == "default"
    assert r.sources["paths.state_dir"] == "default"
    assert r.loaded_files == []
    assert r.primary_toml is None


# ---------- single-file TOML -------------------------------------------------


def test_single_toml_overrides_defaults(tmp_path: Path) -> None:
    p = _toml(
        tmp_path,
        "bty.toml",
        """
        [admin]
        password = "from-toml"

        [server]
        port = 9090
        """,
    )
    r = load_config([p])
    assert r.cfg.admin.password == "from-toml"
    assert r.cfg.server.port == 9090
    # Unset keys retain the default.
    assert r.cfg.server.host == "0.0.0.0"
    # Provenance.
    assert r.sources["admin.password"] == f"toml({p})"
    assert r.sources["server.port"] == f"toml({p})"
    assert r.sources["server.host"] == "default"
    # Loaded files + primary.
    assert r.loaded_files == [p]
    assert r.primary_toml == p


def test_unknown_section_or_key_ignored(tmp_path: Path) -> None:
    """Forward-compat: a TOML carrying a section / key bty-web
    doesn't know yet must not blow up. Newer bty.toml on an older
    bty-web -> the extras are silently dropped."""
    p = _toml(
        tmp_path,
        "bty.toml",
        """
        [admin]
        password = "p"
        unknown_admin_field = "ignored"

        [future_section]
        foo = "bar"
        """,
    )
    r = load_config([p])
    assert r.cfg.admin.password == "p"


# ---------- multi-file overlay -----------------------------------------------


def test_multi_file_later_wins_per_key(tmp_path: Path) -> None:
    """The ``later wins`` rule applies PER KEY, not per file: a key
    set only in the early file isn't clobbered by the later file."""
    base = _toml(
        tmp_path,
        "base.toml",
        """
        [admin]
        password = "base"

        [server]
        port = 1111
        """,
    )
    later = _toml(
        tmp_path,
        "later.toml",
        """
        [server]
        port = 2222
        """,
    )
    r = load_config([base, later])
    # admin.password stays from base.
    assert r.cfg.admin.password == "base"
    assert r.sources["admin.password"] == f"toml({base})"
    # server.port overridden by later.
    assert r.cfg.server.port == 2222
    assert r.sources["server.port"] == f"toml({later})"
    # Primary write target is the last (highest-priority) single file.
    assert r.primary_toml == later


def test_directory_drop_in_expands_lexicographically(tmp_path: Path) -> None:
    """A directory in the candidate list expands to its ``*.toml``
    files in lexicographic order."""
    confd = tmp_path / "conf.d"
    confd.mkdir()
    _toml(confd, "10-base.toml", "[admin]\npassword = 'base'\n")
    _toml(
        confd,
        "20-overrides.toml",
        "[admin]\npassword = 'overrides'\n[server]\nport = 9999\n",
    )
    r = load_config([confd])
    assert r.cfg.admin.password == "overrides"
    assert r.cfg.server.port == 9999
    # loaded_files reflects the lex order.
    assert [p.name for p in r.loaded_files] == ["10-base.toml", "20-overrides.toml"]


def test_nonexistent_paths_silently_skipped(tmp_path: Path) -> None:
    """Missing candidate paths don't raise -- the default search list
    lists multiple speculative candidates and only the present ones
    contribute."""
    real = _toml(tmp_path, "bty.toml", "[admin]\npassword = 'p'\n")
    ghost = tmp_path / "does-not-exist.toml"
    r = load_config([ghost, real])
    assert r.cfg.admin.password == "p"
    assert r.loaded_files == [real]


# ---------- env-var overrides ------------------------------------------------


def test_env_overrides_toml_and_stamps_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = _toml(
        tmp_path,
        "bty.toml",
        """
        [admin]
        password = "from-toml"

        [server]
        port = 9000
        """,
    )
    monkeypatch.setenv("BTY_ADMIN_PASSWORD", "from-env")
    monkeypatch.setenv("BTY_SERVER_PORT", "7777")
    r = load_config([p])
    assert r.cfg.admin.password == "from-env"
    assert r.cfg.server.port == 7777  # coerced str -> int
    assert r.sources["admin.password"] == "env(BTY_ADMIN_PASSWORD)"
    assert r.sources["server.port"] == "env(BTY_SERVER_PORT)"


def test_empty_env_value_does_not_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty-string env var (the shape compose emits for
    ``BTY_FOO: ${{BTY_FOO:-}}`` when BTY_FOO is unset) must NOT
    register as an override -- the TOML / default value stays."""
    p = _toml(tmp_path, "bty.toml", '[admin]\npassword = "from-toml"\n')
    monkeypatch.setenv("BTY_ADMIN_PASSWORD", "")
    r = load_config([p])
    assert r.cfg.admin.password == "from-toml"
    assert r.sources["admin.password"] == f"toml({p})"


def test_env_int_coerce_raises_on_garbage(monkeypatch: pytest.MonkeyPatch) -> None:
    """Typo'd integers fail loud rather than silently dropping the
    override. ``port = "abc"`` would otherwise be hard to debug."""
    monkeypatch.setenv("BTY_SERVER_PORT", "not-a-number")
    with pytest.raises(ValueError):
        load_config([])


# ---------- derived path resolvers -------------------------------------------


def test_paths_derive_from_state_dir(tmp_path: Path) -> None:
    """Blank ``boot_dir`` / ``backup_dir`` / ``catalog_file`` resolve
    relative to ``state_dir`` -- one knob, coherent layout."""
    p = _toml(tmp_path, "bty.toml", f'[paths]\nstate_dir = "{tmp_path}/srv"\n')
    r = load_config([p])
    assert r.cfg.state_dir == tmp_path / "srv"
    assert r.cfg.boot_dir == tmp_path / "srv" / "boot"
    assert r.cfg.backup_dir == tmp_path / "srv" / "backups"
    assert r.cfg.catalog_file == tmp_path / "srv" / "catalog.toml"
    assert r.cfg.state_db == tmp_path / "srv" / "state.db"


def test_paths_explicit_override_wins(tmp_path: Path) -> None:
    p = _toml(
        tmp_path,
        "bty.toml",
        f"""
        [paths]
        state_dir = "{tmp_path}/srv"
        boot_dir = "{tmp_path}/elsewhere/boot"
        """,
    )
    r = load_config([p])
    assert r.cfg.boot_dir == tmp_path / "elsewhere" / "boot"
    # backup_dir was NOT overridden, still derives from state_dir.
    assert r.cfg.backup_dir == tmp_path / "srv" / "backups"


# ---------- save_value (round-trip) ------------------------------------------


def test_save_value_round_trips_through_load(tmp_path: Path) -> None:
    """Writing back a key via :func:`save_value` is picked up on the
    next :func:`load_config`. Validates the Settings-edit path."""
    p = _toml(
        tmp_path,
        "bty.toml",
        """
        # operator comment
        [admin]
        password = "old"
        """,
    )
    save_value(p, "admin", "password", "new")
    r = load_config([p])
    assert r.cfg.admin.password == "new"
    # Operator comment survives the round-trip (tomlkit preserves
    # formatting; a tomli-w-style rewrite would lose this).
    assert "# operator comment" in p.read_text(encoding="utf-8")


def test_save_value_creates_file_when_absent(tmp_path: Path) -> None:
    """First-time write to a non-existent file works; the parent
    directory is created if needed."""
    p = tmp_path / "subdir" / "bty.toml"
    save_value(p, "admin", "password", "hello")
    assert p.is_file()
    r = load_config([p])
    assert r.cfg.admin.password == "hello"


def test_save_value_atomic_no_tmpfile_left_behind(tmp_path: Path) -> None:
    """Atomic via tempfile + rename -- no ``.bty.toml.tmp`` litter
    after a successful write."""
    p = tmp_path / "bty.toml"
    save_value(p, "admin", "password", "x")
    leftovers = [child.name for child in tmp_path.iterdir() if child.name.startswith(".")]
    assert leftovers == []


# ---------- primary_toml semantics -------------------------------------------


def test_primary_toml_is_the_last_writable_single_file(tmp_path: Path) -> None:
    """The Settings-page write target is the highest-priority
    single-file TOML in the candidate list (drop-in directories are
    skipped -- writing into a glob has no defined target)."""
    confd = tmp_path / "conf.d"
    confd.mkdir()
    _toml(confd, "10-base.toml", "")
    single = _toml(tmp_path, "bty.toml", "")
    r = load_config([confd, single])
    assert r.primary_toml == single


def test_primary_toml_picks_creatable_file_when_only_candidate_is_missing(
    tmp_path: Path,
) -> None:
    """If the operator's --config points at a not-yet-created file in
    a writable directory, that's still a valid write target
    (Settings UI seeds it on first edit)."""
    missing = tmp_path / "fresh.toml"
    assert not missing.exists()
    r = load_config([missing])
    assert r.primary_toml == missing
