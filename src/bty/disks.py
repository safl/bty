"""Block-device discovery via ``lsblk``.

Pure-data module: returns plain dicts so the result can be JSON-serialised
or tabulated by the CLI without further translation.
"""

from __future__ import annotations

import json
import subprocess
from typing import Any

# Columns we ask ``lsblk`` for. NAME and PATH are both requested because
# loop/ram devices sometimes lack PATH.
_LSBLK_COLS = "NAME,PATH,SIZE,TYPE,VENDOR,MODEL,SERIAL,RM,RO,MOUNTPOINTS,TRAN"

# Top-level types we surface. Partitions are a child of "disk" and are
# not reported as separate entries in the default output.
_INTERESTING_TYPES = {"disk"}


def list_disks() -> list[dict[str, Any]]:
    """Return interesting block devices on the local system.

    Shells out to ``lsblk -J`` and filters to top-level disks (drops
    loop, ram, rom, etc.). Each entry is a plain dict with stable keys.
    """
    proc = subprocess.run(
        ["lsblk", "-J", "-o", _LSBLK_COLS],
        capture_output=True,
        text=True,
        check=True,
    )
    payload = json.loads(proc.stdout)
    devices: list[dict[str, Any]] = payload.get("blockdevices", [])

    out: list[dict[str, Any]] = []
    for d in devices:
        if d.get("type") not in _INTERESTING_TYPES:
            continue
        out.append(
            {
                "path": d.get("path") or f"/dev/{d['name']}",
                "size": d.get("size"),
                "type": d.get("type"),
                "vendor": _strip_or_none(d.get("vendor")),
                "model": _strip_or_none(d.get("model")),
                "serial": d.get("serial"),
                "tran": d.get("tran"),
                "removable": bool(d.get("rm")),
                "readonly": bool(d.get("ro")),
                "mountpoints": [m for m in (d.get("mountpoints") or []) if m],
            }
        )
    return out


def _strip_or_none(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None
