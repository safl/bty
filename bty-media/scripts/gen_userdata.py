"""
Generate cloud-init user-data
==============================

Assembles cloud-init user-data for a bty-media variant by combining a
per-variant base config with files staged under ``rootfs/``. Each file
becomes a ``write_files`` entry with path, owner, permissions, and
content derived from the actual file.

Reads the ``[bty]`` config from cijoe to:

- Pick the variant (``bty.variant``: ``"usb"`` or ``"server"``). The
  variant selects the base file (``auxiliary/cloudinit-base-<variant>.user``)
  and the rootfs subdirectory (``rootfs/<variant>/``); files under
  ``rootfs/common/`` are always inlined.
- Substitute ``__BTY_HOSTNAME__`` and ``__BTY_TIMEZONE__`` in the base.

Retargetable: False
"""

from __future__ import annotations

import logging as log
import stat
from pathlib import Path

KNOWN_VARIANTS = ("usb", "server")


def main(args, cijoe):
    repo_dir = Path.cwd()
    rootfs_dir = repo_dir / "rootfs"
    output_path = repo_dir / "auxiliary" / "cloudinit-userdata.user"

    bty = cijoe.getconf("bty", {})
    if not bty:
        log.error("No [bty] section found in config")
        return 1

    variant = bty.get("variant", "usb")
    if variant not in KNOWN_VARIANTS:
        log.error(f"Unknown bty.variant {variant!r}; expected one of {KNOWN_VARIANTS}")
        return 1

    base_path = repo_dir / "auxiliary" / f"cloudinit-base-{variant}.user"
    if not base_path.exists():
        log.error(f"Base config not found: {base_path}")
        return 1

    # Variant rootfs is required to exist (even if empty); common is optional.
    variant_rootfs = rootfs_dir / variant
    common_rootfs = rootfs_dir / "common"
    if not variant_rootfs.exists():
        log.error(f"rootfs/{variant} directory not found: {variant_rootfs}")
        return 1

    base = base_path.read_text()
    base = base.replace("__BTY_TIMEZONE__", bty.get("timezone", "UTC"))
    base = base.replace("__BTY_HOSTNAME__", bty.get("hostname", "bty"))

    lines = [base, "", "write_files:"]

    # Order: common first, then variant. ``cloud-init`` write_files are
    # applied in document order, so a variant-specific file with the
    # same target path overrides the common one.
    for source_dir in (common_rootfs, variant_rootfs):
        if not source_dir.exists():
            continue
        for filepath in sorted(source_dir.rglob("*")):
            if not filepath.is_file():
                continue

            target = "/" + str(filepath.relative_to(source_dir))
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
    log.info(f"Generated {output_path} (variant={variant})")

    return 0
