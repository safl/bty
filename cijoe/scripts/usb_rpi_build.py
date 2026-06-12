"""
Build the bty USB-Pi flasher (arm64 raw .img.gz) via live-build
================================================================

Drives Debian's live-build on a native arm64 host to produce a
kernel + initrd + squashfs trio, then invokes
``bty-media/scripts/pack_rpi_img.py`` to wrap them into a
Pi-bootable 3-partition raw disk image. Output is
``bty-usb-rpi-arm64-v<version>.img.gz``.

Workflow:

1. Copy ``bty-media/live-build/`` into ``cijoe/_build/usb-rpi/``.
2. Stamp ``__BTY_VERSION__`` placeholders with the current
   pyproject version (kernel cmdline, /etc/issue / motd, etc.).
3. Run ``sudo env BTY_VARIANT=usb-rpi lb clean --all && lb
   build``. ``auto/config`` reads ``BTY_VARIANT`` and configures
   ``--architectures arm64 --binary-images netboot`` + the
   Pi-flavoured kernel cmdline.
4. Run ``pack_rpi_img.py`` to assemble the .img from the lb
   output: FAT32 RPIBOOT (firmware + kernel + initrd + config /
   cmdline), ext4 BTY_LIVE (squashfs as /live/filesystem.squashfs),
   exFAT BTY_IMAGES (scratch, auto-grows on first boot).
5. The packaging script writes the .img.gz + sha256 itself.

The cwd at run time is ``cijoe/`` (the Makefile cd's there before
invoking cijoe), so the bty-media tree lives at
``Path.cwd().parent / "bty-media"`` and the build scratch dir is
``Path.cwd() / "_build" / "usb-rpi"``.

Skipped for any variant other than ``usb-rpi``.

Retargetable: False (needs root for the lb chroot + the
loop-mount steps in pack_rpi_img.py; the build host MUST be
native arm64).
"""

from __future__ import annotations

import errno
import logging as log
import shutil
from argparse import ArgumentParser
from pathlib import Path

PUBLISH_BASENAME_FMT = "bty-usb-rpi-arm64-v{version}.img.gz"


def add_args(parser: ArgumentParser):
    del parser  # no flags; signature kept for cijoe consistency


def main(args, cijoe):
    del args
    cijoe_dir = Path.cwd()
    bty_media = cijoe_dir.parent / "bty-media"

    variant = cijoe.getconf("bty", {}).get("variant", "")
    if variant != "usb-rpi":
        log.info(
            f"Skipping usb_rpi_build (variant={variant!r}; only 'usb-rpi' runs lb arm64 + Pi-pack)"
        )
        return 0

    images = cijoe.getconf("system-imaging.images", {})
    image = images.get("bty-usb-rpi-arm64")
    if not image:
        log.error("missing system-imaging.images.bty-usb-rpi-arm64 in config")
        return errno.EINVAL

    publish_dir_str = image.get("publish", {}).get("dir")
    if not publish_dir_str:
        log.error("system-imaging.images.bty-usb-rpi-arm64.publish.dir is unset")
        return errno.EINVAL
    publish_dir = Path(publish_dir_str)
    publish_dir.mkdir(parents=True, exist_ok=True)

    build_dir = cijoe_dir / "_build" / "usb-rpi"
    if build_dir.exists():
        # ``lb`` writes a chroot tree owned by root; rm needs sudo.
        err, _ = cijoe.run_local(f"sudo rm -rf {build_dir}")
        if err:
            log.error(f"failed to remove stale build dir {build_dir}")
            return err
    build_dir.mkdir(parents=True)

    # Copy the live-build config tree into the working dir.
    config_src = bty_media / "live-build"
    if not config_src.exists():
        log.error(f"live-build config tree missing: {config_src}")
        return errno.ENOENT
    for entry in config_src.iterdir():
        dest = build_dir / entry.name
        if entry.is_dir():
            shutil.copytree(entry, dest, symlinks=True)
        else:
            shutil.copy2(entry, dest)

    # Stamp the bty version into every ``__BTY_VERSION__`` placeholder
    # in the copied tree before ``lb build`` runs. Files that pick up
    # the stamp: ``auto/config`` (kernel cmdline), ``/etc/issue``
    # (login banner), ``/etc/motd`` (post-login), and
    # ``/etc/profile.d/bty-version.sh`` (interactive shell).
    bty_version = _read_bty_version(cijoe_dir)
    img_basename = PUBLISH_BASENAME_FMT.format(version=bty_version)
    log.info(f"Stamping bty version {bty_version} into live-build tree")
    err, _ = cijoe.run_local(
        f"sh -c 'grep -rlF __BTY_VERSION__ {build_dir} | "
        f"xargs --no-run-if-empty sed -i s/__BTY_VERSION__/{bty_version}/g'"
    )
    if err:
        log.error("__BTY_VERSION__ substitution failed")
        return err

    # Drive auto/config into arm64 + netboot mode. ``sudo env`` is
    # used (instead of ``sudo`` with shell variable assignment)
    # because sudo strips environment by default; the env var has
    # to be present at every lb invocation because ``lb build``
    # re-runs ``lb config`` (which re-runs ``auto/config``) during
    # its own setup.
    log.info(f"Running lb build in {build_dir} (BTY_VARIANT=usb-rpi)")
    err, _ = cijoe.run_local(
        f"sh -c 'cd {build_dir} && "
        "sudo env BTY_VARIANT=usb-rpi lb clean --all && "
        "sudo env BTY_VARIANT=usb-rpi lb build'"
    )
    if err:
        log.error("lb build failed; see live-build.log under the build dir")
        return err

    # Verify the lb step actually produced the three expected
    # output files; catches the "lb claimed success but emitted
    # nothing on this arch" class of bug.
    binary_dir = build_dir / "binary"
    expected_globs = (
        binary_dir.rglob("vmlinuz*"),
        binary_dir.rglob("initrd*"),
        binary_dir.rglob("filesystem.squashfs"),
    )
    for matches in expected_globs:
        if not any(m.is_file() for m in matches):
            log.error(
                f"lb output missing under {binary_dir}; "
                f"expected vmlinuz / initrd / filesystem.squashfs"
            )
            cijoe.run_local(f"sudo find {binary_dir} -maxdepth 3 -type f 2>/dev/null")
            return errno.ENOENT

    # Run the Pi-image packaging script. It writes the .img.gz +
    # the .sha256 sidecar to the publish dir directly.
    out_img = publish_dir / img_basename
    log.info(f"Packing Pi-bootable image: {out_img}")
    pack_script = bty_media / "scripts" / "pack_rpi_img.py"
    err, _ = cijoe.run_local(
        f"sudo {pack_script} --build-dir {build_dir} --output {out_img} --bty-version {bty_version}"
    )
    if err:
        log.error("pack_rpi_img.py failed")
        return err

    if not out_img.is_file():
        log.error(f"pack_rpi_img.py exited 0 but no output at {out_img}")
        return errno.ENOENT

    log.info("Published artifacts:")
    cijoe.run_local(f"ls -la {publish_dir}/bty-usb-rpi-arm64-*")
    return 0


def _read_bty_version(cijoe_dir: Path) -> str:
    """Read the bty-lab version from the repo's top-level pyproject.toml.

    The pre-built live env stamps this string into the kernel
    cmdline (``bty.version=``), the login banner, and the shell
    startup file so operators can identify the release at every
    boot moment. Reading pyproject.toml directly (rather than
    ``importlib.metadata``) keeps the bake script independent of
    whether bty-lab is installed in the cijoe runner's env.
    """
    pyproject = cijoe_dir.parent / "pyproject.toml"
    for line in pyproject.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("version") and "=" in stripped:
            return stripped.split("=", 1)[1].strip().strip('"').strip("'")
    raise RuntimeError(f"could not find version line in {pyproject}")
