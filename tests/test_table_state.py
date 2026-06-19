"""Tests for the URL-state helpers used by paginated / sortable UI tables.

The helpers are pure functions over a ``Mapping[str, str]`` (the
request query params), so the tests construct plain dicts -- no
FastAPI client needed. This keeps the unit suite under a hundred
milliseconds and lets us cover the edge cases (bogus ?sort=, out-
of-range ?page=, junk ?per_page=) cheaply.
"""

from __future__ import annotations

import pytest

from bty.web._table_state import (
    DEFAULT_PER_PAGE,
    PER_PAGE_CHOICES,
    PageState,
    SortState,
    build_query_string,
    parse_pagination,
    parse_sort,
)

MACHINES_COLUMNS = {
    "mac": "mac",
    "last_seen_at": "last_seen_at",
    "last_flashed_at": "last_flashed_at",
    "bty_image_ref": "bty_image_ref",
    "boot_mode": "boot_mode",
}


def test_parse_sort_falls_back_to_default_when_param_missing() -> None:
    s = parse_sort({}, allowed=MACHINES_COLUMNS, default_column="mac")
    assert s.column == "mac"
    assert s.direction == "asc"
    assert s.order_by_sql == "mac ASC"


def test_parse_sort_honours_valid_query_params() -> None:
    s = parse_sort(
        {"sort": "last_seen_at", "dir": "desc"},
        allowed=MACHINES_COLUMNS,
        default_column="mac",
    )
    assert s.column == "last_seen_at"
    assert s.direction == "desc"
    assert s.order_by_sql == "last_seen_at DESC"


def test_parse_sort_rejects_column_not_in_allowlist() -> None:
    """The allowlist is the only SQL-injection guard: a ``?sort=`` value
    not on the list falls back to the default rather than being
    interpolated into the SQL string."""
    s = parse_sort(
        {"sort": "; DROP TABLE machines;--", "dir": "asc"},
        allowed=MACHINES_COLUMNS,
        default_column="mac",
    )
    assert s.column == "mac"
    assert s.order_by_sql == "mac ASC"
    assert "DROP" not in s.order_by_sql


def test_parse_sort_rejects_unknown_direction() -> None:
    s = parse_sort(
        {"sort": "mac", "dir": "sideways"},
        allowed=MACHINES_COLUMNS,
        default_column="mac",
    )
    assert s.direction == "asc"


def test_parse_sort_default_direction_can_be_desc_for_time_cols() -> None:
    """Event-log style pages want "newest first" out of the box. The
    helper accepts ``default_direction='desc'`` to start that way."""
    s = parse_sort(
        {},
        allowed=MACHINES_COLUMNS,
        default_column="last_seen_at",
        default_direction="desc",
    )
    assert s.direction == "desc"
    assert s.order_by_sql == "last_seen_at DESC"


def test_parse_sort_default_column_must_be_in_allowlist() -> None:
    """Catches a programming error early: the page declared a default
    that isn't in its own allowlist, which would silently fall back
    to whatever ``params`` happens to send."""
    with pytest.raises(ValueError):
        parse_sort({}, allowed=MACHINES_COLUMNS, default_column="not_a_column")


def test_parse_sort_carries_complex_expressions_into_order_by() -> None:
    """Pages can map a logical key to a SQL expression (e.g. a
    case-insensitive sort, or a primary + tie-breaker pair) and the
    helper splices in the expression literally."""
    s = parse_sort(
        {"sort": "name"},
        allowed={
            "name": "LOWER(name)",
            "id": "id",
        },
        default_column="id",
    )
    assert s.order_by_sql == "LOWER(name) ASC"


def test_sort_state_next_direction_flips_on_active_column() -> None:
    s = SortState(column="mac", direction="asc", order_by_sql="mac ASC")
    assert s.next_direction("mac") == "desc"
    s = SortState(column="mac", direction="desc", order_by_sql="mac DESC")
    assert s.next_direction("mac") == "asc"
    # clicking a non-active column starts at asc regardless of the
    # current column's direction
    assert s.next_direction("last_seen_at") == "asc"


def test_parse_pagination_defaults_to_first_page_default_per_page() -> None:
    p = parse_pagination({}, total=120)
    assert p.page == 1
    assert p.per_page == DEFAULT_PER_PAGE
    assert p.offset == 0
    assert p.limit == DEFAULT_PER_PAGE


def test_parse_pagination_honours_valid_page_and_per_page() -> None:
    p = parse_pagination({"page": "3", "per_page": "25"}, total=120)
    assert p.page == 3
    assert p.per_page == 25
    assert p.offset == 50
    assert p.limit == 25


def test_parse_pagination_clamps_per_page_to_allowed_values() -> None:
    """An invalid ``per_page`` (negative, zero, non-choice, garbage)
    silently falls back to the default rather than letting the
    operator request a 1-million-row page that OOMs the server."""
    for bogus in ("0", "-5", "13", "1000", "abc", ""):
        p = parse_pagination({"per_page": bogus}, total=100)
        assert p.per_page == DEFAULT_PER_PAGE, f"per_page={bogus!r} should fall back to default"


def test_parse_pagination_clamps_page_to_last_page() -> None:
    """Bookmarks / shared URLs may reference a page that no longer
    exists after rows were deleted. Clamp to the last page so the
    operator sees rows instead of an empty view + confused state."""
    p = parse_pagination({"page": "9999", "per_page": "25"}, total=100)
    # 100 rows / 25 per page = 4 pages
    assert p.last_page == 4
    assert p.page == 4
    assert p.offset == 75


def test_parse_pagination_clamps_zero_and_negative_pages_to_one() -> None:
    for bogus in ("0", "-1", "abc", ""):
        p = parse_pagination({"page": bogus}, total=100)
        assert p.page == 1, f"page={bogus!r} should clamp to 1"


def test_parse_pagination_empty_table_still_shows_page_one() -> None:
    """A table with zero matching rows still renders page 1 (with the
    empty-state row), so the footer's "page X of Y" line reads
    "page 1 of 1" rather than "page 1 of 0"."""
    p = parse_pagination({"page": "5"}, total=0)
    assert p.page == 1
    assert p.last_page == 1
    assert p.first_row == 0
    assert p.last_row == 0


def test_pagination_first_last_row_indices() -> None:
    p = parse_pagination({"page": "2", "per_page": "25"}, total=120)
    assert p.first_row == 26
    assert p.last_row == 50
    # last page may have fewer than per_page rows
    p = parse_pagination({"page": "5", "per_page": "25"}, total=120)
    assert p.first_row == 101
    assert p.last_row == 120


def test_pagination_numbered_pages_window() -> None:
    # default window=2, so ``?page=5`` of 10 pages shows 3..7
    p = PageState(page=5, per_page=10, total=100, offset=40, limit=10)
    assert p.numbered_pages() == [3, 4, 5, 6, 7]
    # near the start: clamps lower bound
    p = PageState(page=1, per_page=10, total=100, offset=0, limit=10)
    assert p.numbered_pages() == [1, 2, 3]
    # near the end: clamps upper bound
    p = PageState(page=10, per_page=10, total=100, offset=90, limit=10)
    assert p.numbered_pages() == [8, 9, 10]


def test_pagination_has_prev_next() -> None:
    first = PageState(page=1, per_page=25, total=100, offset=0, limit=25)
    assert first.has_prev is False
    assert first.has_next is True
    last = PageState(page=4, per_page=25, total=100, offset=75, limit=25)
    assert last.has_prev is True
    assert last.has_next is False


def test_per_page_choices_includes_the_default() -> None:
    assert DEFAULT_PER_PAGE in PER_PAGE_CHOICES


def test_build_query_string_drops_empty_and_none() -> None:
    out = build_query_string(
        {"sort": "mac", "filter": "", "dir": "asc"},
        {"page": "3", "filter": None},
    )
    # alphabetical (stable) order; filter dropped; page added
    assert out == "dir=asc&page=3&sort=mac"


def test_build_query_string_override_removes_existing_key() -> None:
    out = build_query_string(
        {"filter": "discovered", "page": "2"},
        {"filter": None},
    )
    assert out == "page=2"


def test_build_query_string_url_encodes_values() -> None:
    out = build_query_string({"actor": "operator", "subject_id": "aa:bb:cc:dd:ee:ff"}, None)
    assert "%3A" in out or out == "actor=operator&subject_id=aa:bb:cc:dd:ee:ff"
