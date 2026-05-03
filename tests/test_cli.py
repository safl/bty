"""End-to-end-ish tests for bty.cli — modules underneath are mocked."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from bty import cli, images


def test_main_help_exits_cleanly() -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["--help"])
    assert excinfo.value.code == 0


def test_main_no_subcommand_exits_with_usage() -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli.main([])
    # argparse exits 2 on missing required subcommand
    assert excinfo.value.code == 2


def test_list_disks_table(capsys: pytest.CaptureFixture[str]) -> None:
    with patch(
        "bty.cli.disks.list_disks",
        return_value=[
            {
                "path": "/dev/sda",
                "size": "500G",
                "tran": "sata",
                "vendor": "ATA",
                "model": "Samsung SSD",
                "serial": "ABC",
                "removable": False,
            },
        ],
    ):
        rc = cli.main(["list", "disks"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "/dev/sda" in out
    assert "PATH" in out  # uppercased header


def test_list_disks_json(capsys: pytest.CaptureFixture[str]) -> None:
    fake_rows = [{"path": "/dev/sda", "size": "500G"}]
    with patch("bty.cli.disks.list_disks", return_value=fake_rows):
        rc = cli.main(["list", "disks", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == fake_rows


def test_list_images_uses_image_root_argument(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "alpha.qcow2").write_bytes(b"")
    rc = cli.main(["list", "images", "--image-root", str(tmp_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "alpha.qcow2" in out


def test_inspect_image_missing_returns_two(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = cli.main(["inspect", "image", str(tmp_path / "nope.qcow2")])
    assert rc == 2
    err = capsys.readouterr().err
    assert "no such image" in err


def test_inspect_image_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    img = tmp_path / "x.img"
    img.write_bytes(b"\0" * 5)
    rc = cli.main(["inspect", "image", "--json", str(img)])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["format"] == "img"
    assert payload["size_bytes"] == 5


def test_default_image_root_respects_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BTY_IMAGE_ROOT", "/tmp/custom")
    assert images.default_image_root() == Path("/tmp/custom")


def test_default_image_root_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BTY_IMAGE_ROOT", raising=False)
    assert images.default_image_root() == images.DEFAULT_IMAGE_ROOT
