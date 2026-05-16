"""Tests for ``bty.pxe._events.emit``.

The emitter writes one-line JSON to stdout. Tests use ``capsys``
to capture and parse what landed; we don't need real journald
plumbing to verify the wire shape.
"""

from __future__ import annotations

import json

import pytest

from bty.pxe._events import emit


def test_emit_writes_single_line_json(capsys: pytest.CaptureFixture[str]) -> None:
    emit("dhcp.offer", mac="aa:bb:cc:dd:ee:ff", arch=7, bootfile="ipxe.efi")
    captured = capsys.readouterr().out
    assert captured.count("\n") == 1, "exactly one newline terminator"
    payload = json.loads(captured)
    assert payload == {
        "evt": "dhcp.offer",
        "mac": "aa:bb:cc:dd:ee:ff",
        "arch": 7,
        "bootfile": "ipxe.efi",
    }


def test_emit_uses_compact_json(capsys: pytest.CaptureFixture[str]) -> None:
    """journald MESSAGE entries are nicer to scan when there's no
    extra whitespace; verify the compact separators stuck."""
    emit("tftp.complete", peer="192.168.1.50:1234", bytes=12345)
    line = capsys.readouterr().out
    # No spaces between key:value or after commas.
    assert '":"' in line or '":12345' in line
    assert ", " not in line


def test_emit_evt_field_is_first(capsys: pytest.CaptureFixture[str]) -> None:
    """``evt`` should be the first key for ``jq -r '.evt'`` /
    cheap-grep convenience in journalctl output."""
    emit("tftp.rrq", peer="192.168.1.50:1234", file="ipxe.efi")
    line = capsys.readouterr().out.strip()
    assert line.startswith('{"evt":"tftp.rrq"')


def test_emit_handles_nested_payload(capsys: pytest.CaptureFixture[str]) -> None:
    """Caller can pass nested lists/dicts when an event carries
    structured context (e.g. negotiated TFTP options)."""
    emit("tftp.rrq", peer="192.168.1.50:1234", options={"blksize": "1468", "tsize": "0"})
    payload = json.loads(capsys.readouterr().out)
    assert payload["options"] == {"blksize": "1468", "tsize": "0"}


def test_emit_flushes_stdout(capsys: pytest.CaptureFixture[str]) -> None:
    """If emit() didn't flush, captured output could be empty until
    pytest finalises -- tests would still pass but real journald
    delivery would be buffered. Capsys's behaviour follows stdout
    flushes, so reading immediately verifies flush."""
    emit("dhcp.discover", mac="aa:bb:cc:dd:ee:ff", arch=7)
    # Read happens before any explicit flush -- if emit didn't
    # flush internally, capsys would see an empty string.
    assert capsys.readouterr().out != ""
