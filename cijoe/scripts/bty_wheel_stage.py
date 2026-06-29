"""
Stage the bty-lab wheel for variants that bake bty into their image
====================================================================

Builds a wheel from the parent repo via ``uv build`` and copies it
into a per-variant staging directory under ``bty-media/``:

- ``netboot-pc`` / ``usbboot-pc`` / ``usbboot-rpi`` ->
  ``bty-media/live-build/config/includes.chroot/opt/bty/`` (consumed
  by the live-build hook ``0500-bty-install.hook.chroot``, which
  ``pip install``s it into the chroot's ``/opt/bty/venv``). All
  three variants share the same chroot tree; only the bake's
  binary-image shape and target architecture differ.

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
# Variants not listed here are skipped with rc=0. The live-build
# variants share the same chroot tree, so they share the same
# target dir.
#
# ``ramboot-init`` does not actually use the staged wheel at boot
# time (its initrd pivots to the catalog image's root before any
# of bty's userspace runs), but the shared chroot's
# ``0500-bty-install`` hook expects to find the wheel under
# ``/opt/bty/`` regardless of variant. Staging it here keeps the
# chroot symmetric and lb build green.
_LIVE_CHROOT_BTY = Path("live-build") / "config" / "includes.chroot" / "opt" / "bty"
TARGET_DIRS: dict[str, Path] = {
    "netboot-pc": _LIVE_CHROOT_BTY,
    "usbboot-pc": _LIVE_CHROOT_BTY,
    "usbboot-rpi": _LIVE_CHROOT_BTY,
    "ramboot-init": _LIVE_CHROOT_BTY,
}


def add_args(parser: ArgumentParser):
    del parser  # no flags; signature kept for cijoe consistency


def main(args, cijoe):
    del args
    cijoe_dir = Path.cwd()
    repo_root = cijoe_dir.parent
    bty_media = repo_root / "bty-media"

    variant = cijoe.getconf("bty", {}).get("variant", "usbboot-pc")
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
