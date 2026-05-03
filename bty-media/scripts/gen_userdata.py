"""
Generate cloud-init user-data
==============================

Assembles cloud-init user-data by combining the base config with files
from the ``rootfs/`` directory. Each file in ``rootfs/`` becomes a
``write_files`` entry with path, owner, permissions, and content derived
from the actual file.

Reads the ``[bty]`` config from cijoe to:

- Substitute ``__BTY_TIMEZONE__`` in the base config.
- Substitute ``__BTY_HOSTNAME__`` in the base config.

Retargetable: False
"""

from __future__ import annotations

import logging as log
import stat
from pathlib import Path


def main(args, cijoe):
    repo_dir = Path.cwd()
    rootfs_dir = repo_dir / "rootfs"
    base_path = repo_dir / "auxiliary" / "cloudinit-base.user"
    output_path = repo_dir / "auxiliary" / "cloudinit-userdata.user"

    if not base_path.exists():
        log.error(f"Base config not found: {base_path}")
        return 1

    if not rootfs_dir.exists():
        log.error(f"rootfs directory not found: {rootfs_dir}")
        return 1

    bty = cijoe.getconf("bty", {})
    if not bty:
        log.error("No [bty] section found in config")
        return 1

    base = base_path.read_text()
    base = base.replace("__BTY_TIMEZONE__", bty.get("timezone", "UTC"))
    base = base.replace("__BTY_HOSTNAME__", bty.get("hostname", "bty"))

    lines = [base, "", "write_files:"]

    for filepath in sorted(rootfs_dir.rglob("*")):
        if not filepath.is_file():
            continue

        target = "/" + str(filepath.relative_to(rootfs_dir))
        content = filepath.read_text()
        mode = stat.S_IMODE(filepath.stat().st_mode)
        perms = f"0{mode:o}"

        lines.append(f"  - path: {target}")
        lines.append("    owner: root:root")
        lines.append(f'    permissions: "{perms}"')
        lines.append("    content: |")
        for line in content.splitlines():
            lines.append(f"      {line}")
        lines.append("")

    output_path.write_text("\n".join(lines) + "\n")
    log.info(f"Generated {output_path}")

    return 0
