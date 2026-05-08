"""
Stage the bty-lab wheel for variants that bake bty into their image
====================================================================

Builds a wheel from the parent repo via ``uv build`` and copies it
into a per-variant staging directory under ``bty-media/``:

- ``server-x86`` / ``server-rpi`` ->
  ``bty-media/rootfs/server/opt/bty/`` (consumed by the cloud-init
  ``write_files`` block emitted by ``gen_userdata.py``; the server's
  runcmd ``pip install``s it into ``/opt/bty/venv``).
- ``netboot-x86`` / ``usb-x86`` ->
  ``bty-media/live-build/config/includes.chroot/opt/bty/`` (consumed
  by the live-build hook ``0500-bty-install.hook.chroot``, which
  ``pip install``s it into the chroot's ``/opt/bty/venv``).

The cwd at run time is ``cijoe/`` (the Makefile cd's there before
invoking cijoe), so the repo root is ``Path.cwd().parent`` and the
bty-media tree lives at ``repo_root / "bty-media"``.

Variants not in the table are skipped with rc=0.

Retargetable: False
"""

from __future__ import annotations

import errno
import logging as log
import shutil
from argparse import ArgumentParser
from pathlib import Path

# Variant -> destination directory relative to ``bty-media/``.
# Variants not listed here are skipped with rc=0.
TARGET_DIRS: dict[str, Path] = {
    "server-x86": Path("rootfs") / "server" / "opt" / "bty",
    "server-rpi": Path("rootfs") / "server" / "opt" / "bty",
    "netboot-x86": Path("live-build") / "config" / "includes.chroot" / "opt" / "bty",
    "usb-x86": Path("live-build") / "config" / "includes.chroot" / "opt" / "bty",
}


def add_args(parser: ArgumentParser):
    del parser  # no flags; signature kept for cijoe consistency


def main(args, cijoe):
    del args
    cijoe_dir = Path.cwd()
    repo_root = cijoe_dir.parent
    bty_media = repo_root / "bty-media"

    variant = cijoe.getconf("bty", {}).get("variant", "usb-x86")
    target_rel = TARGET_DIRS.get(variant)
    if target_rel is None:
        log.info(f"Skipping wheel stage (variant={variant!r}; nothing to bake)")
        return 0
    target_dir = bty_media / target_rel
    target_dir.mkdir(parents=True, exist_ok=True)

    out_dir = cijoe_dir / "_build" / "wheel"
    if out_dir.exists():
        # Drop any wheel from a prior build so we don't accidentally stage
        # a stale version when the source tree's version bumps.
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    err, _ = cijoe.run_local(f"sh -c 'cd {repo_root} && uv build --wheel --out-dir {out_dir}'")
    if err:
        log.error(f"Failed to build bty-lab wheel from {repo_root}")
        return err

    wheels = sorted(out_dir.glob("bty_lab-*-py3-none-any.whl"))
    if not wheels:
        log.error(f"No wheel matching bty_lab-*-py3-none-any.whl produced in {out_dir}")
        return errno.ENOENT
    if len(wheels) > 1:
        log.error(f"Expected exactly one wheel; found {len(wheels)}: {wheels}")
        return errno.E2BIG

    # Drop any previously-staged wheel(s) under the target dir - we want
    # exactly one for the consuming step's glob to be unambiguous.
    for stale in target_dir.glob("bty_lab-*.whl"):
        if stale.name != wheels[0].name:
            log.info(f"Removing stale staged wheel {stale}")
            stale.unlink()

    target_path = target_dir / wheels[0].name
    shutil.copy2(wheels[0], target_path)
    log.info(f"Staged {wheels[0].name} -> {target_path}")

    return 0
