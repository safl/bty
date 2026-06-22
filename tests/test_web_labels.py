"""Tests for ``bty.web._labels``: side-table CRUD + form parsing.

Pre-1.0 break-freely: labels replaced the singular ``machines.hostname``
column in v0.58.0. These tests pin the contract: alphabetical read,
set-semantic replace, explicit cascade on machine delete, and a
permissive form parser that strips empties + dedupes."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from bty.web import _db, _labels


def _conn(tmp_path: Path) -> sqlite3.Connection:
    state = tmp_path / "state.db"
    _db.init_db(state)
    conn = sqlite3.connect(state)
    conn.row_factory = sqlite3.Row
    return conn


def test_get_labels_empty_returns_empty_list(tmp_path: Path) -> None:
    """A machine with no labels returns ``[]``, not ``None``. Templates
    iterate the result directly; ``None`` would 500 the row."""
    with _conn(tmp_path) as conn:
        assert _labels.get_labels(conn, "aa:bb:cc:dd:ee:ff") == []


def test_set_labels_round_trips_alphabetically(tmp_path: Path) -> None:
    """Read order is alphabetical regardless of insert order; the chip
    rendering in the row template is then deterministic without a
    per-row sort in the Jinja layer."""
    with _conn(tmp_path) as conn:
        _labels.set_labels(conn, "aa:bb:cc:dd:ee:ff", ["rack-3", "noisy", "gmktec-g10"])
        assert _labels.get_labels(conn, "aa:bb:cc:dd:ee:ff") == [
            "gmktec-g10",
            "noisy",
            "rack-3",
        ]


def test_set_labels_replaces_wholesale(tmp_path: Path) -> None:
    """``set_labels`` is set-semantic: the new list replaces the old.
    Operators rarely add one label without touching the others;
    the form posts the full set every time."""
    with _conn(tmp_path) as conn:
        _labels.set_labels(conn, "aa:bb:cc:dd:ee:ff", ["a", "b", "c"])
        _labels.set_labels(conn, "aa:bb:cc:dd:ee:ff", ["x", "y"])
        assert _labels.get_labels(conn, "aa:bb:cc:dd:ee:ff") == ["x", "y"]


def test_set_labels_handles_duplicate_input(tmp_path: Path) -> None:
    """``INSERT OR IGNORE`` against the (mac, label) primary key
    deduplicates: a caller's accidental ``["a", "a", "b"]`` lands as
    ``["a", "b"]``."""
    with _conn(tmp_path) as conn:
        _labels.set_labels(conn, "aa:bb:cc:dd:ee:ff", ["a", "a", "b"])
        assert _labels.get_labels(conn, "aa:bb:cc:dd:ee:ff") == ["a", "b"]


def test_delete_labels_removes_only_target_mac(tmp_path: Path) -> None:
    """The delete-machine cascade clears one MAC's rows without
    touching another MAC's labels."""
    with _conn(tmp_path) as conn:
        _labels.set_labels(conn, "aa:bb:cc:dd:ee:01", ["x"])
        _labels.set_labels(conn, "aa:bb:cc:dd:ee:02", ["y"])
        _labels.delete_labels(conn, "aa:bb:cc:dd:ee:01")
        assert _labels.get_labels(conn, "aa:bb:cc:dd:ee:01") == []
        assert _labels.get_labels(conn, "aa:bb:cc:dd:ee:02") == ["y"]


def test_parse_form_value_strips_and_dedupes() -> None:
    """The form encoding is comma-separated. The parser trims each
    token, drops empties (a stray trailing comma in ``"a, b, "``
    would otherwise hit the empty-string rejection), and dedupes
    case-insensitively (the first casing wins)."""
    assert _labels.parse_form_value("rack-3, noisy , gmktec-g10") == [
        "rack-3",
        "noisy",
        "gmktec-g10",
    ]
    # Trailing comma -> empty token dropped (would otherwise 422).
    assert _labels.parse_form_value("a, b, ") == ["a", "b"]
    # Empty input -> empty list (the "no labels" path).
    assert _labels.parse_form_value("") == []
    assert _labels.parse_form_value("   ") == []
    # Case-insensitive dedup; first occurrence's casing wins.
    assert _labels.parse_form_value("Rack-3, rack-3, RACK-3") == ["Rack-3"]
