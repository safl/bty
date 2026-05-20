"""
Build a slim bty-flavoured iPXE binary for the server appliance
================================================================

Clones iPXE upstream, copies in bty's embed script + general.h
trim overrides, builds ``bin-x86_64-efi/ipxe.efi``, and stages
the resulting binary into the bty-media rootfs tree at:

* ``bty-media/rootfs/server/var/lib/tftpboot/ipxe.efi`` -- served
  via TFTP by dnsmasq on the appliance; the operator's LAN DHCP
  server points PXE clients at this file.
* ``bty-media/rootfs/server/var/lib/bty/boot/ipxe.efi`` -- served
  via HTTP by bty-web for UEFI HTTP-Boot clients.

Why custom build (vs. shipping Debian's stock ``/usr/lib/ipxe/
ipxe.efi`` as a symlink):

* Stock iPXE re-DHCPs after loading and tries to ``chain`` the
  DHCP filename (``ipxe.efi``) -- which is itself. Infinite loop.
* bty's network architecture (v0.18+) deliberately does NOT
  configure DHCP user-class matching on the operator's router;
  the router-config cheatsheet stays a one-liner.
* Embedding ``chain http://${next-server}:8080/pxe-bootstrap.ipxe``
  inside the binary breaks the loop by pre-empting iPXE's
  DHCP-filename autoboot. Operator doesn't have to touch DHCP
  beyond pointing PXE clients at this appliance.

Build inputs (under ``bty-media/auxiliary/``):

* ``ipxe-embed.ipxe`` -- the embedded boot script.
* ``ipxe-local-general.h`` -- trims iPXE's feature set so the
  binary stays close to Debian's stock 996 KB (the test
  firmware on UNDI 3.0.22 accepted that size; bigger builds
  failed to load).

Variant gating:

* ``server-x86`` -- builds + stages the x86_64-EFI binary.
* ``server-rpi`` -- arm64 build of ipxe.efi is a follow-up
  (different EMBED target, separate cross-compile setup).
  For now the RPi bake symlinks Debian's stock and accepts the
  chain-loop limitation -- documented in the deployment notes.
* ``usb-x86`` / ``netboot-x86`` -- no TFTP-served iPXE; skipped.

Retargetable: False
"""

from __future__ import annotations

import errno
import logging as log
import shutil
import subprocess
from argparse import ArgumentParser
from pathlib import Path

# Variants we build for. Others (rpi, netboot, usb) get skipped.
SUPPORTED_VARIANTS: tuple[str, ...] = ("server-x86",)

# Upstream iPXE source.
IPXE_REPO = "https://github.com/ipxe/ipxe.git"
# iPXE git ref to build. Currently tracks ``master`` -- iPXE moves
# slowly enough that the tip mostly stays buildable + loadable on
# the firmware under test. NOTE: this is NOT pinned, so a bad
# upstream day can break the bake; pinning to a known-good commit
# hash here would make the build reproducible, but only after
# verifying that hash's binary still loads on the test firmware
# (UNDI 3.0.22 was size-sensitive, see ipxe-local-general.h).
IPXE_REV = "master"


def add_args(parser: ArgumentParser):
    del parser  # no flags; signature kept for cijoe consistency


def main(args, cijoe):
    del args
    cijoe_dir = Path.cwd()
    repo_root = cijoe_dir.parent
    bty_media = repo_root / "bty-media"

    variant = cijoe.getconf("bty", {}).get("variant", "server-x86")
    if variant not in SUPPORTED_VARIANTS:
        log.info(f"Skipping iPXE build (variant={variant!r}; nothing to bake)")
        return 0

    aux = bty_media / "auxiliary"
    embed_src = aux / "ipxe-embed.ipxe"
    general_src = aux / "ipxe-local-general.h"
    for required in (embed_src, general_src):
        if not required.is_file():
            log.error(f"missing build input: {required}")
            return errno.ENOENT

    # Working dir under the run output -- cleaned between bakes
    # by cijoe's own machinery.
    build_root = cijoe_dir / "_build" / "ipxe"
    build_root.mkdir(parents=True, exist_ok=True)
    src_tree = build_root / "ipxe"

    # Clone or refresh the iPXE checkout. ``git fetch`` keeps
    # subsequent bakes incremental.
    if (src_tree / ".git").is_dir():
        log.info(f"Reusing existing iPXE checkout at {src_tree}")
        rc = subprocess.call(
            ["git", "-C", str(src_tree), "fetch", "--depth=1", "origin", IPXE_REV],
        )
        if rc != 0:
            log.error(f"git fetch failed (rc={rc})")
            return rc
        rc = subprocess.call(
            ["git", "-C", str(src_tree), "reset", "--hard", "FETCH_HEAD"],
        )
        if rc != 0:
            log.error(f"git reset failed (rc={rc})")
            return rc
    else:
        log.info(f"Cloning iPXE -> {src_tree}")
        rc = subprocess.call(
            [
                "git",
                "clone",
                "--depth=1",
                "--branch",
                IPXE_REV,
                IPXE_REPO,
                str(src_tree),
            ],
        )
        if rc != 0:
            log.error(f"git clone failed (rc={rc})")
            return rc

    # Stage bty's build inputs into iPXE's source tree.
    src_dir = src_tree / "src"
    local_config = src_dir / "config" / "local"
    local_config.mkdir(parents=True, exist_ok=True)
    shutil.copy2(general_src, local_config / "general.h")
    shutil.copy2(embed_src, src_dir / "bty-embed.ipxe")

    # Build the x86_64 EFI binary with the embedded script.
    log.info(f"Building ipxe.efi (EMBED={embed_src.name})")
    rc = subprocess.call(
        [
            "make",
            "-j",
            "4",
            "-C",
            str(src_dir),
            "bin-x86_64-efi/ipxe.efi",
            "EMBED=bty-embed.ipxe",
            "NO_WERROR=1",
        ],
    )
    if rc != 0:
        log.error(f"iPXE build failed (rc={rc})")
        return rc

    built = src_dir / "bin-x86_64-efi" / "ipxe.efi"
    if not built.is_file():
        log.error(f"expected build output not found: {built}")
        return errno.ENOENT
    log.info(f"Built {built} ({built.stat().st_size} bytes)")

    # Stage into the appliance rootfs at /var/lib/tftpboot/ and
    # /var/lib/bty/boot/. The cloud-init bake doesn't need to
    # symlink anymore -- our binary replaces the previous
    # symlink-to-Debian-stock path.
    tftp_dst = bty_media / "rootfs" / "server" / "var" / "lib" / "tftpboot" / "ipxe.efi"
    http_dst = bty_media / "rootfs" / "server" / "var" / "lib" / "bty" / "boot" / "ipxe.efi"
    for dst in (tftp_dst, http_dst):
        dst.parent.mkdir(parents=True, exist_ok=True)
        # Remove any previous symlink so we get a regular file.
        if dst.is_symlink() or dst.exists():
            dst.unlink()
        shutil.copy2(built, dst)
        log.info(f"Staged {dst}")

    return 0
