"""Tests for bty.disks. lsblk is mocked so the suite stays hermetic."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

from bty import disks

_FAKE_LSBLK = {
    "blockdevices": [
        {
            "name": "sda",
            "path": "/dev/sda",
            "size": "500G",
            "type": "disk",
            "vendor": "ATA     ",
            "model": "Samsung SSD 870",
            "serial": "S5SUNG0123456",
            "rm": False,
            "ro": False,
            "mountpoints": [None],
            "tran": "sata",
        },
        {
            "name": "nvme0n1",
            "path": "/dev/nvme0n1",
            "size": "1T",
            "type": "disk",
            "vendor": None,
            "model": "Samsung 980 PRO",
            "serial": "NVME0X000001",
            "rm": False,
            "ro": False,
            "mountpoints": ["/"],
            "tran": "nvme",
        },
        {
            "name": "loop0",
            "path": "/dev/loop0",
            "size": "55M",
            "type": "loop",
            "vendor": None,
            "model": None,
            "serial": None,
            "rm": False,
            "ro": True,
            "mountpoints": ["/snap/core/0"],
            "tran": None,
        },
        {
            "name": "ram0",
            "path": "/dev/ram0",
            "size": "16M",
            "type": "ram",
            "vendor": None,
            "model": None,
            "serial": None,
            "rm": False,
            "ro": False,
            "mountpoints": [None],
            "tran": None,
        },
    ]
}


def _mock_lsblk(payload: dict[str, Any]) -> MagicMock:
    proc = MagicMock()
    proc.stdout = json.dumps(payload)
    proc.returncode = 0
    return proc


def test_list_disks_filters_to_disks() -> None:
    with patch("bty.disks.subprocess.run", return_value=_mock_lsblk(_FAKE_LSBLK)):
        rows = disks.list_disks()
    paths = [r["path"] for r in rows]
    assert paths == ["/dev/sda", "/dev/nvme0n1"]


def test_list_disks_projects_useful_columns() -> None:
    with patch("bty.disks.subprocess.run", return_value=_mock_lsblk(_FAKE_LSBLK)):
        rows = disks.list_disks()
    sda = rows[0]
    assert sda["vendor"] == "ATA"  # whitespace stripped
    assert sda["model"] == "Samsung SSD 870"
    assert sda["serial"] == "S5SUNG0123456"
    assert sda["tran"] == "sata"
    assert sda["removable"] is False
    assert sda["readonly"] is False
    assert sda["mountpoints"] == []  # None entries dropped


def test_list_disks_handles_mounted_root() -> None:
    with patch("bty.disks.subprocess.run", return_value=_mock_lsblk(_FAKE_LSBLK)):
        rows = disks.list_disks()
    nvme = rows[1]
    assert nvme["mountpoints"] == ["/"]
    assert nvme["vendor"] is None  # absent in input -> None in output


def test_list_disks_strips_whitespace_on_serial() -> None:
    """Some USB enclosures and vendor firmware report serials with
    trailing whitespace via lsblk. ``list_disks`` strips the
    serial like it strips vendor/model so the inventory side and
    the live env's ``pick_target`` agree on the canonical form."""
    payload = {
        "blockdevices": [
            {
                "name": "sda",
                "path": "/dev/sda",
                "type": "disk",
                "serial": "  CRUCIAL12345  ",
                "vendor": "ATA",
                "model": "X",
                "size": "500G",
                "tran": "sata",
                "rm": False,
                "ro": False,
                "mountpoints": [],
            }
        ]
    }
    with patch("bty.disks.subprocess.run", return_value=_mock_lsblk(payload)):
        rows = disks.list_disks()
    assert rows[0]["serial"] == "CRUCIAL12345"


def test_list_disks_empty_when_no_devices() -> None:
    with patch("bty.disks.subprocess.run", return_value=_mock_lsblk({"blockdevices": []})):
        rows = disks.list_disks()
    assert rows == []
