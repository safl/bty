"""
Stage the bty-lab wheel for the server image
=============================================

Builds a wheel from the parent repo via ``uv build`` and copies it
into ``bty-media/rootfs/server/opt/bty/``. ``gen_userdata.py``
inlines that file as a base64 ``write_files`` entry; the server's
runcmd then ``pip install``s the wheel into ``/opt/bty/venv``.

The cwd at run time is ``bty-media/`` (the Makefile cd's there before
invoking cijoe), so the repo root is ``Path.cwd().parent``.

Skipped for any variant other than ``server`` — the USB image carries
no bty-web service.

Retargetable: False
"""

from __future__ import annotations

import errno
import logging as log
import shutil
from argparse import ArgumentParser
from pathlib import Path


def add_args(parser: ArgumentParser):
    del parser  # no flags; signature kept for cijoe consistency


def main(args, cijoe):
    del args
    bty_media = Path.cwd()
    repo_root = bty_media.parent

    variant = cijoe.getconf("bty", {}).get("variant", "usb")
    if variant != "server":
        log.info(f"Skipping wheel stage (variant={variant!r}; only 'server' bakes the wheel)")
        return 0

    target_dir = bty_media / "rootfs" / "server" / "opt" / "bty"
    target_dir.mkdir(parents=True, exist_ok=True)

    out_dir = bty_media / "_build" / "wheel"
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

    # Strip any previously-staged wheel(s) — we only want one in the
    # write_files block, and an old wheel left around would also be
    # installed by the runcmd glob.
    for stale in target_dir.glob("bty_lab-*.whl"):
        if stale.name != wheels[0].name:
            log.info(f"Removing stale staged wheel {stale}")
            stale.unlink()

    target_path = target_dir / wheels[0].name
    shutil.copy2(wheels[0], target_path)
    log.info(f"Staged {wheels[0].name} -> {target_path}")

    return 0
