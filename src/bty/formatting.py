"""Output formatting helpers for the ``bty`` CLI.

Two surfaces:

- ``print_table`` for tabular output with left-aligned columns sized to
  fit the longest value in each column. No external dependency.
- ``print_inspect`` for ``bty inspect`` output: a labelled list with
  one-level nesting for dict / multiline values.
"""

from __future__ import annotations

import sys
from typing import IO, Any


def print_table(
    rows: list[dict[str, Any]],
    columns: list[str],
    *,
    file: IO[str] | None = None,
) -> None:
    """Print ``rows`` as a left-aligned column table to ``file`` (default stdout)."""
    out = file if file is not None else sys.stdout

    if not rows:
        print("(no entries)", file=out)
        return

    widths: dict[str, int] = {col: len(col) for col in columns}
    for row in rows:
        for col in columns:
            value = _stringify(row.get(col))
            widths[col] = max(widths[col], len(value))

    header = "  ".join(col.upper().ljust(widths[col]) for col in columns)
    print(header, file=out)
    print("  ".join("-" * widths[col] for col in columns), file=out)

    for row in rows:
        cells = [_stringify(row.get(col)).ljust(widths[col]) for col in columns]
        print("  ".join(cells).rstrip(), file=out)


def print_inspect(
    info: dict[str, Any],
    *,
    file: IO[str] | None = None,
) -> None:
    """Print a single inspect record as ``key: value`` lines."""
    out = file if file is not None else sys.stdout

    for key, value in info.items():
        if isinstance(value, dict):
            print(f"{key}:", file=out)
            for k, v in value.items():
                print(f"  {k}: {_stringify(v)}", file=out)
        elif isinstance(value, str) and "\n" in value:
            print(f"{key}:", file=out)
            for line in value.splitlines():
                print(f"  {line}", file=out)
        else:
            print(f"{key}: {_stringify(value)}", file=out)


def _stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return ", ".join(str(v) for v in value)
    return str(value)
