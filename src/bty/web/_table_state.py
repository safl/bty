"""URL-state helpers for paginated + sortable HTML tables.

The bty-web UI pages that show real data (events, machines, images)
need consistent sort + paging behaviour: a click on a column header
flips direction, the URL is bookmarkable, the page survives an htmx
or SSE refresh because state is in the query string, not in
JavaScript memory.

Two small helpers cover that:

- :func:`parse_sort` reads ``?sort=<col>&dir=asc|desc`` from the
  request, validates against a per-page allowlist (this is the SQL-
  injection guard: anything not in the allowlist falls back to the
  default), and returns a :class:`SortState` carrying the chosen
  column, direction, and the ``ORDER BY`` SQL fragment ready to be
  spliced into a query.
- :func:`parse_pagination` reads ``?page=<N>&per_page=<N>``,
  clamps ``per_page`` to the dropdown values, computes ``LIMIT /
  OFFSET``, and returns a :class:`PageState` that the template uses
  to render the page-number nav + "Showing X-Y of Z" line.

No FastAPI / Starlette dependency. Tests construct a plain dict
mapping query-param names to values and call the helpers
directly.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

# Page-size choices surfaced as the per-page dropdown. The first
# entry is the default. Kept small so the table fits in a single
# scroll window on a 14" laptop without forcing the operator to
# expand a giant 200-row page.
PER_PAGE_CHOICES: tuple[int, ...] = (25, 50, 100)
DEFAULT_PER_PAGE = PER_PAGE_CHOICES[1]  # 50

# Number of numeric page-buttons shown around the current page in
# the footer. ``window=2`` gives ``... 3 4 [5] 6 7 ...`` (the
# Bootstrap-pagination convention; matches the look of the rest of
# the UI).
_NUMBERED_WINDOW = 2


@dataclass(frozen=True)
class SortState:
    """Parsed ``?sort=<col>&dir=asc|desc``.

    ``column`` is one of the page's allowlisted column keys (the
    DB-name, not the human label). ``direction`` is ``"asc"`` or
    ``"desc"``. ``order_by_sql`` is the safe SQL fragment to splice
    into a query, e.g. ``"discovered_at DESC, mac ASC"``: it
    includes the tie-breaker so paginated views are stable.
    """

    column: str
    direction: str  # "asc" | "desc"
    order_by_sql: str

    def is_active(self, column: str) -> bool:
        return self.column == column

    def next_direction(self, column: str) -> str:
        """For a header link on ``column``: the direction the click
        should send. Clicking the currently-active column flips
        direction; clicking any other column starts at the column's
        natural default direction.
        """
        return "desc" if self.is_active(column) and self.direction == "asc" else "asc"


@dataclass(frozen=True)
class PageState:
    """Parsed ``?page=<N>&per_page=<N>`` + computed totals."""

    page: int  # 1-indexed; clamped to 1 .. last_page
    per_page: int  # one of PER_PAGE_CHOICES
    total: int  # rows matching the (post-filter) query
    offset: int  # LIMIT OFFSET argument
    limit: int  # LIMIT argument; equals per_page

    @property
    def last_page(self) -> int:
        # An empty table still has page 1 (with zero rows shown), so
        # the footer's ``page X of 1`` reads sensibly rather than
        # ``X of 0``.
        if self.total <= 0:
            return 1
        return (self.total + self.per_page - 1) // self.per_page

    @property
    def first_row(self) -> int:
        """1-indexed row number of the first row on this page (0 if
        the table is empty), used in the "Showing X-Y of Z" line."""
        if self.total <= 0:
            return 0
        return self.offset + 1

    @property
    def last_row(self) -> int:
        """1-indexed row number of the last row on this page."""
        if self.total <= 0:
            return 0
        return min(self.offset + self.per_page, self.total)

    @property
    def has_prev(self) -> bool:
        return self.page > 1

    @property
    def has_next(self) -> bool:
        return self.page < self.last_page

    def numbered_pages(self) -> list[int]:
        """The page-number buttons to render in the footer.

        Returns up to ``2 * _NUMBERED_WINDOW + 1`` pages centred on
        the current page, clamped to ``[1, last_page]``. The
        template adds explicit Prev / Next / First / Last buttons
        outside this window, so very large tables don't grow a
        50-button footer.
        """
        lo = max(1, self.page - _NUMBERED_WINDOW)
        hi = min(self.last_page, self.page + _NUMBERED_WINDOW)
        return list(range(lo, hi + 1))


def parse_sort(
    params: Mapping[str, str],
    *,
    allowed: Mapping[str, str],
    default_column: str,
    default_direction: str = "asc",
) -> SortState:
    """Parse ``?sort=...&dir=...`` against a per-page column allowlist.

    ``allowed`` maps the column-key the operator sees in the URL to
    the SQL expression that goes into ``ORDER BY`` (typically just
    the column name, but pages can map ``"name"`` -> ``"LOWER(name)"``
    for case-insensitive sorts, etc.). Anything not in ``allowed``
    falls back to ``default_column``. This is the only SQL
    safety guard: callers are NOT expected to validate further.

    ``default_direction`` is what an unrecognised ``?dir`` falls
    back to; pages that want "newest first" pass ``"desc"``.
    The active column's direction toggles via :meth:`SortState.next_direction`
    when the operator re-clicks the header.

    The returned ``order_by_sql`` ALWAYS includes ``id`` (when present
    in ``allowed`` as a tie-breaker is recommended) or falls back
    to the chosen column alone. The caller is responsible for adding
    a stable secondary sort when the primary column can repeat
    (e.g. for SQLite ``ORDER BY discovered_at DESC, mac ASC`` -- pass
    ``allowed={"discovered_at": "discovered_at, mac ASC", ...}``).
    """
    if default_column not in allowed:
        raise ValueError(
            f"default_column {default_column!r} must be in the allowlist {sorted(allowed)!r}"
        )
    raw_col = params.get("sort") or ""
    column = raw_col if raw_col in allowed else default_column
    raw_dir = (params.get("dir") or "").lower()
    direction = raw_dir if raw_dir in ("asc", "desc") else default_direction
    expr = allowed[column]
    order_by_sql = f"{expr} {direction.upper()}"
    return SortState(column=column, direction=direction, order_by_sql=order_by_sql)


def parse_pagination(
    params: Mapping[str, str],
    *,
    total: int,
    default_per_page: int = DEFAULT_PER_PAGE,
) -> PageState:
    """Parse ``?page=<N>&per_page=<N>``, clamp to sane values, return
    a :class:`PageState` carrying offset / limit / nav data.

    ``per_page`` is clamped to one of :data:`PER_PAGE_CHOICES`; an
    invalid / unrecognised value silently falls back to
    ``default_per_page``. ``page`` is clamped to ``[1, last_page]``
    so an operator pasting ``?page=9999`` lands on the actual last
    page rather than an empty view. ``total`` MUST be the post-
    filter row count from the caller's SQL ``COUNT(*)``.
    """
    raw_per = params.get("per_page") or ""
    try:
        per_candidate = int(raw_per)
    except ValueError:
        per_candidate = default_per_page
    per_page = per_candidate if per_candidate in PER_PAGE_CHOICES else default_per_page

    if total < 0:
        total = 0
    last_page = max(1, (total + per_page - 1) // per_page) if total > 0 else 1

    raw_page = params.get("page") or ""
    try:
        page_candidate = int(raw_page)
    except ValueError:
        page_candidate = 1
    page = max(1, min(last_page, page_candidate))

    offset = (page - 1) * per_page
    return PageState(page=page, per_page=per_page, total=total, offset=offset, limit=per_page)


def build_query_string(
    base: Mapping[str, str | None],
    overrides: Mapping[str, str | None] | None = None,
) -> str:
    """Merge ``base`` and ``overrides``, drop empty / None values, and
    return a URL-encoded query string suitable for header / pagination
    links. Caller wraps with ``"?"`` if non-empty.

    Empty-string values are dropped so a click on a header link
    doesn't accumulate ``?filter=&sort=&dir=`` noise. ``None`` in
    ``overrides`` REMOVES the key from base (handy for "clear this
    filter" links). Stable key order so two callers producing the
    same logical URL emit byte-identical strings (helps testing).
    """
    import urllib.parse

    merged: dict[str, str] = {}
    for k, v in base.items():
        if v:
            merged[k] = str(v)
    if overrides:
        for k, v in overrides.items():
            if v is None or v == "":
                merged.pop(k, None)
            else:
                merged[k] = str(v)
    return urllib.parse.urlencode(sorted(merged.items()))
