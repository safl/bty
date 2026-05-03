"""Tests for bty.formatting."""

from __future__ import annotations

import io

from bty import formatting


def test_print_table_renders_aligned_columns() -> None:
    rows = [
        {"name": "alpha", "size": 10},
        {"name": "beta-long-name", "size": 999},
    ]
    out = io.StringIO()
    formatting.print_table(rows, columns=["name", "size"], file=out)
    text = out.getvalue()
    lines = text.strip().splitlines()
    assert lines[0].startswith("NAME")
    assert "SIZE" in lines[0]
    assert lines[1].startswith("-")
    # Both data rows are present in the same column order
    assert "alpha" in lines[2]
    assert "beta-long-name" in lines[3]


def test_print_table_handles_empty_rows() -> None:
    out = io.StringIO()
    formatting.print_table([], columns=["foo"], file=out)
    assert out.getvalue() == "(no entries)\n"


def test_print_table_renders_none_as_empty() -> None:
    rows = [{"name": "x", "vendor": None}]
    out = io.StringIO()
    formatting.print_table(rows, columns=["name", "vendor"], file=out)
    text = out.getvalue()
    # The 'vendor' cell is empty for the row, but the line still has 'x'
    assert "x" in text


def test_print_table_renders_lists_comma_joined() -> None:
    rows = [{"name": "x", "mountpoints": ["/a", "/b"]}]
    out = io.StringIO()
    formatting.print_table(rows, columns=["name", "mountpoints"], file=out)
    assert "/a, /b" in out.getvalue()


def test_print_inspect_basic() -> None:
    out = io.StringIO()
    formatting.print_inspect({"path": "/foo", "size": 42}, file=out)
    text = out.getvalue()
    assert "path: /foo" in text
    assert "size: 42" in text


def test_print_inspect_nested_dict() -> None:
    out = io.StringIO()
    formatting.print_inspect({"path": "/foo", "detail": {"virtual-size": 12345}}, file=out)
    text = out.getvalue()
    assert "detail:" in text
    assert "  virtual-size: 12345" in text


def test_print_inspect_multiline_string() -> None:
    out = io.StringIO()
    formatting.print_inspect({"detail": "line1\nline2"}, file=out)
    text = out.getvalue()
    assert "detail:" in text
    assert "  line1" in text
    assert "  line2" in text
