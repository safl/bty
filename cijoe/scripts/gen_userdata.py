"""
Generate cloud-init user-data
==============================

Assembles cloud-init user-data for a bty-media variant by combining a
per-variant base config (``bty-media/auxiliary/cloudinit-base-<variant>.user``)
with files staged under ``bty-media/rootfs/``. Each file becomes a
``write_files`` entry with path, owner, permissions, and content
derived from the actual file. Binary files (anything that is not valid
UTF-8) are emitted with ``encoding: b64`` so cloud-init restores the
bytes on the target.

Reads the ``[bty]`` config from cijoe to:

- Pick the variant (``bty.variant``: ``"usb"`` or ``"server"``). The
  variant selects the base file and the rootfs subdirectory
  (``bty-media/rootfs/<variant>/``); files under
  ``bty-media/rootfs/common/`` are always inlined.
- Substitute ``__BTY_HOSTNAME__`` and ``__BTY_TIMEZONE__`` in the base.

The cwd at run time is ``cijoe/`` (the Makefile cd's there before
invoking cijoe), so the bty-media tree lives at
``Path.cwd().parent / "bty-media"``.

Retargetable: False
"""

from __future__ import annotations

import base64
import logging as log
import stat
from pathlib import Path

KNOWN_VARIANTS = ("usb", "server")

# Wrap base64 lines at 76 cols so the YAML output is readable rather
# than a single multi-kilobyte line. Cloud-init concatenates them
# transparently before decoding.
_B64_WRAP = 76


def main(args, cijoe):
    del args
    cijoe_dir = Path.cwd()
    bty_media = cijoe_dir.parent / "bty-media"
    rootfs_dir = bty_media / "rootfs"
    output_path = bty_media / "auxiliary" / "cloudinit-userdata.user"

    bty = cijoe.getconf("bty", {})
    if not bty:
        log.error("No [bty] section found in config")
        return 1

    variant = bty.get("variant", "usb")
    if variant not in KNOWN_VARIANTS:
        log.error(f"Unknown bty.variant {variant!r}; expected one of {KNOWN_VARIANTS}")
        return 1

    base_path = bty_media / "auxiliary" / f"cloudinit-base-{variant}.user"
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
            lines.extend(_render_write_file(filepath, source_dir))

    output_path.write_text("\n".join(lines) + "\n")
    log.info(f"Generated {output_path} (variant={variant})")

    return 0


def _render_write_file(filepath: Path, source_dir: Path) -> list[str]:
    """Render one ``write_files`` entry for ``filepath`` (relative to ``source_dir``).

    Tries UTF-8 text first and emits a literal ``content: |`` block; on
    decode failure falls back to base64 + ``encoding: b64``.
    """
    target = "/" + str(filepath.relative_to(source_dir))
    mode = stat.S_IMODE(filepath.stat().st_mode)
    perms = f"0{mode:o}"
    header = [
        f"  - path: {target}",
        "    owner: root:root",
        f'    permissions: "{perms}"',
    ]

    raw = filepath.read_bytes()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        encoded = base64.b64encode(raw).decode("ascii")
        body = ["    encoding: b64", "    content: |"]
        for i in range(0, len(encoded), _B64_WRAP):
            body.append(f"      {encoded[i : i + _B64_WRAP]}")
        body.append("")
        return header + body

    body = ["    content: |"]
    for line in text.splitlines():
        body.append(f"      {line}")
    body.append("")
    return header + body
