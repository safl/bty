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

- Pick the variant (``bty.variant``: ``"server-x86"`` is the only
  cloud-init bake variant after M19 phase 6 retired the
  ``usb-x86`` cloud-init path; ``server-rpi`` uses
  ``build-rpi.yaml`` and doesn't go through this script). The arch
  suffix is stripped to derive the *role* (``"server"``); the role
  then selects the base file
  (``bty-media/auxiliary/cloudinit-base-<role>.user``) and the
  rootfs subdirectory (``bty-media/rootfs/<role>/``). Files under
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

KNOWN_ROLES = ("server",)

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

    variant = bty.get("variant", "server-x86")
    # Strip arch suffix to derive the role: "server-x86" -> "server".
    # Variants with no suffix map to themselves.
    role = variant.split("-")[0]
    if role not in KNOWN_ROLES:
        log.error(
            f"Unknown variant role {role!r} (variant={variant!r}); "
            f"expected first segment to be one of {KNOWN_ROLES}"
        )
        return 1

    base_path = bty_media / "auxiliary" / f"cloudinit-base-{role}.user"
    if not base_path.exists():
        log.error(f"Base config not found: {base_path}")
        return 1

    # Role rootfs is required to exist (even if empty); common is optional.
    role_rootfs = rootfs_dir / role
    common_rootfs = rootfs_dir / "common"
    if not role_rootfs.exists():
        log.error(f"rootfs/{role} directory not found: {role_rootfs}")
        return 1

    bty_version = _read_bty_version(cijoe_dir)

    base = base_path.read_text()
    base = base.replace("__BTY_TIMEZONE__", bty.get("timezone", "UTC"))
    base = base.replace("__BTY_HOSTNAME__", bty.get("hostname", "bty"))
    base = base.replace("__BTY_VERSION__", bty_version)

    lines = [base, "", "write_files:"]

    # Order: common first, then role-specific. ``cloud-init`` write_files
    # are applied in document order, so a role-specific file with the
    # same target path overrides the common one.
    for source_dir in (common_rootfs, role_rootfs):
        if not source_dir.exists():
            continue
        for filepath in sorted(source_dir.rglob("*")):
            if not filepath.is_file():
                continue
            lines.extend(_render_write_file(filepath, source_dir, bty_version))

    output_path.write_text("\n".join(lines) + "\n")
    log.info(f"Generated {output_path} (variant={variant})")

    return 0


def _render_write_file(filepath: Path, source_dir: Path, bty_version: str) -> list[str]:
    """Render one ``write_files`` entry for ``filepath`` (relative to ``source_dir``).

    Tries UTF-8 text first and emits a literal ``content: |`` block; on
    decode failure falls back to base64 + ``encoding: b64``.

    Text content gets ``__BTY_VERSION__`` substituted to the bty-lab
    version, mirroring the live-build hook so the bty-server's
    ``/etc/issue`` / ``/etc/motd`` / ``/etc/profile.d/bty-version.sh``
    can carry the same kind of version stamp.
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
        body.extend(
            f"      {encoded[i : i + _B64_WRAP]}" for i in range(0, len(encoded), _B64_WRAP)
        )
        body.append("")
        return header + body

    text = text.replace("__BTY_VERSION__", bty_version)
    body = ["    content: |"]
    body.extend(f"      {line}" for line in text.splitlines())
    body.append("")
    return header + body


def _read_bty_version(cijoe_dir: Path) -> str:
    """Read the bty-lab version from the repo's top-level pyproject.toml.

    Mirrors the helper in ``usb_iso_build.py`` -- one source of truth
    is the wheel's version string, surfaced into the pre-built image's
    ``/etc/issue`` / motd / profile.d files via the same
    ``__BTY_VERSION__`` placeholder convention.
    """
    pyproject = cijoe_dir.parent / "pyproject.toml"
    for line in pyproject.read_text().splitlines():
        stripped = line.strip()
        if stripped.startswith("version") and "=" in stripped:
            return stripped.split("=", 1)[1].strip().strip('"').strip("'")
    raise RuntimeError(f"could not find version line in {pyproject}")
