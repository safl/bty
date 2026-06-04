"""
Build a slim bty-flavoured iPXE binary
======================================

Clones iPXE upstream, copies in bty's embed script + general.h trim
overrides, and builds ``bin-x86_64-efi/ipxe.efi``. Two entry points share
the build core (:func:`build_ipxe_efi`):

* **Standalone CLI** (``python3 cijoe/scripts/bty_ipxe_build.py --out
  DIR`` / ``make ipxe``) -- copies the binary into DIR. CI uses this to
  stage the custom iPXE into the bty-web and bty-tftp image build
  contexts, so the container deploy gets the one-bootfile chain guarantee.
* **cijoe** (:func:`main`) -- the legacy ``server-x86`` media bake; stages
  into the bty-media rootfs tree at:

  * ``.../tftpboot/ipxe.efi`` -- served via TFTP; the operator's LAN DHCP
    points PXE clients at this file.
  * ``.../var/lib/bty/boot/ipxe.efi`` -- served via HTTP by bty-web for
    UEFI HTTP-Boot clients.

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
import sys
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


class IpxeBuildError(RuntimeError):
    """The iPXE clone / compile failed; message is operator-actionable."""


def build_ipxe_efi(aux: Path, build_root: Path) -> Path:
    """Clone iPXE, stage bty's embed script + trim, build and return the
    path to ``bin-x86_64-efi/ipxe.efi``. Raises :class:`IpxeBuildError` on
    any failure. Shared by the cijoe entry point (server media bake) and the
    standalone CLI (container / CI artifact)."""
    embed_src = aux / "ipxe-embed.ipxe"
    general_src = aux / "ipxe-local-general.h"
    for required in (embed_src, general_src):
        if not required.is_file():
            raise IpxeBuildError(f"missing build input: {required}")

    build_root.mkdir(parents=True, exist_ok=True)
    src_tree = build_root / "ipxe"

    # Clone or refresh the iPXE checkout. ``git fetch`` keeps
    # subsequent builds incremental.
    if (src_tree / ".git").is_dir():
        log.info(f"Reusing existing iPXE checkout at {src_tree}")
        rc = subprocess.call(["git", "-C", str(src_tree), "fetch", "--depth=1", "origin", IPXE_REV])
        if rc != 0:
            raise IpxeBuildError(f"git fetch failed (rc={rc})")
        rc = subprocess.call(["git", "-C", str(src_tree), "reset", "--hard", "FETCH_HEAD"])
        if rc != 0:
            raise IpxeBuildError(f"git reset failed (rc={rc})")
    else:
        log.info(f"Cloning iPXE -> {src_tree}")
        rc = subprocess.call(
            ["git", "clone", "--depth=1", "--branch", IPXE_REV, IPXE_REPO, str(src_tree)]
        )
        if rc != 0:
            raise IpxeBuildError(f"git clone failed (rc={rc})")

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
        raise IpxeBuildError(f"iPXE build failed (rc={rc})")

    built = src_dir / "bin-x86_64-efi" / "ipxe.efi"
    if not built.is_file():
        raise IpxeBuildError(f"expected build output not found: {built}")
    log.info(f"Built {built} ({built.stat().st_size} bytes)")
    return built


def main(args, cijoe):
    """cijoe entry point: build + stage into the server-media rootfs.

    Retained for the (legacy) ``server-x86`` media bake; the container deploy
    builds the same binary via the standalone CLI below (``make ipxe``)."""
    del args
    cijoe_dir = Path.cwd()
    repo_root = cijoe_dir.parent
    bty_media = repo_root / "bty-media"

    variant = cijoe.getconf("bty", {}).get("variant", "server-x86")
    if variant not in SUPPORTED_VARIANTS:
        log.info(f"Skipping iPXE build (variant={variant!r}; nothing to bake)")
        return 0

    try:
        built = build_ipxe_efi(bty_media / "auxiliary", cijoe_dir / "_build" / "ipxe")
    except IpxeBuildError as exc:
        log.error(str(exc))
        return errno.EIO

    # Stage into the server-media rootfs at /var/lib/tftpboot/ and
    # /var/lib/bty/boot/, replacing the symlink-to-Debian-stock path.
    tftp_dst = bty_media / "rootfs" / "server" / "var" / "lib" / "tftpboot" / "ipxe.efi"
    http_dst = bty_media / "rootfs" / "server" / "var" / "lib" / "bty" / "boot" / "ipxe.efi"
    for dst in (tftp_dst, http_dst):
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.is_symlink() or dst.exists():
            dst.unlink()
        shutil.copy2(built, dst)
        log.info(f"Staged {dst}")

    return 0


def _standalone(argv: list[str] | None = None) -> int:
    """``python3 cijoe/scripts/bty_ipxe_build.py --out DIR`` -- build the
    custom ipxe.efi and copy it into DIR. Used by ``make ipxe`` / CI to
    stage the binary into the bty-web + bty-tftp image build contexts,
    independent of cijoe."""
    parser = ArgumentParser(description="Build bty's custom embedded-chain iPXE (x86_64-efi).")
    parser.add_argument("--out", required=True, help="directory to write ipxe.efi into")
    parser.add_argument(
        "--aux", default=None, help="dir with ipxe-embed.ipxe + ipxe-local-general.h"
    )
    parser.add_argument("--build-root", default=None, help="scratch build dir")
    ns = parser.parse_args(argv)

    log.basicConfig(level=log.INFO, format="%(message)s")
    repo_root = Path(__file__).resolve().parents[2]
    aux = Path(ns.aux) if ns.aux else repo_root / "bty-media" / "auxiliary"
    build_root = Path(ns.build_root) if ns.build_root else repo_root / "cijoe" / "_build" / "ipxe"
    out = Path(ns.out)
    out.mkdir(parents=True, exist_ok=True)

    try:
        built = build_ipxe_efi(aux, build_root)
    except IpxeBuildError as exc:
        log.error(str(exc))
        return 1
    dst = out / "ipxe.efi"
    shutil.copy2(built, dst)
    log.info(f"Staged {dst} ({dst.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(_standalone())
