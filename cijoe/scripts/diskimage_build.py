"""
Build disk image from cloud image
==================================

Downloads the configured Debian cloud image, resizes the boot disk so
cloud-init has room to install packages, builds a cloud-init seed.iso
from the metadata + userdata files referenced in the config, and boots
QEMU with the cloud image and seed attached. cloud-init runs once and
powers off the VM (per ``power_state`` in the userdata). The baked
qcow2 is then compacted via ``qemu-img convert -c`` and a sha256sum is
written.

Adapted from safl/jellyfin-kiosk-appliance-builder. Generic with respect
to the appliance: the variant lives in the cijoe config that this script
reads via ``--image_name``.

Retargetable: False
"""

from __future__ import annotations

import errno
import logging as log
import time
from argparse import ArgumentParser
from pathlib import Path

from cijoe.core.misc import download
from cijoe.qemu.wrapper import Guest

# Bake-time disk size for the cloud-init build VM. The base Debian
# cloud image grows to fill, then we apt-install bty-lab[web]'s
# deps + dnsmasq + iPXE, then trim caches. Final on-disk usage is
# ~1.7 GB; 6 GiB gives ~4 GiB transient headroom for apt + pip
# working space during the install. The pre-built image expands to
# the operator's actual disk size on first boot via
# ``bty-grow-rootfs.service``.
DISK_SIZE = "6G"

# Debian's cloud-image mirror (cloud.debian.org / cdimage.debian.org)
# periodically resets HTTP connections mid-stream during release
# windows or mirror sync; ``cijoe.core.misc.download`` doesn't
# retry on ``requests.ConnectionError``. Retry a few times with
# escalating backoff before failing the release.
_DOWNLOAD_RETRIES = 3
_DOWNLOAD_BACKOFF_S = (5, 15, 45)


def _download_with_retry(url: str, dst: Path) -> int:
    """Wrap :func:`cijoe.core.misc.download` with bounded retry.

    cijoe's ``download`` has two failure modes we have to handle
    separately:

    * Soft failures (e.g. non-2xx HTTP status) come back as a
      non-zero ``err`` in the ``(err, _)`` tuple.
    * Hard failures (the TCP connection reset mid-stream that
      Debian's cloud-image mirror periodically inflicts during
      release windows) raise ``requests.exceptions.ConnectionError``
      out of ``download``. The CI traceback in v0.22.12 was
      precisely this: ``RemoteDisconnected`` -> ``ConnectionError``
      escaping past the original retry loop.

    Both shapes are retried up to ``_DOWNLOAD_RETRIES`` times with
    escalating backoff. Returns 0 on success or an ``errno.EIO``-ish
    code if all attempts failed. Partial destination bytes are
    unlinked between attempts so ``download``'s own existence-check
    short-circuit doesn't fire on a corrupt partial.
    """
    # Pulled in lazily so this module still imports in environments
    # that lack ``requests`` (the cijoe distribution provides it).
    import requests.exceptions as _req_exc

    last_err = errno.EIO
    for attempt in range(_DOWNLOAD_RETRIES):
        try:
            err, _ = download(url, dst)
        except (_req_exc.ConnectionError, _req_exc.ChunkedEncodingError, OSError) as exc:
            err = errno.EIO
            log.warning(f"download {url} raised {type(exc).__name__}: {exc}")
        if not err:
            return 0
        last_err = err
        if dst.exists():
            dst.unlink()
        if attempt + 1 < _DOWNLOAD_RETRIES:
            delay = _DOWNLOAD_BACKOFF_S[attempt]
            log.warning(
                f"download {url} attempt {attempt + 1}/{_DOWNLOAD_RETRIES} failed "
                f"(err={err}); retrying in {delay}s"
            )
            time.sleep(delay)
    log.error(f"download {url} failed after {_DOWNLOAD_RETRIES} attempts")
    return last_err


def add_args(parser: ArgumentParser):
    parser.add_argument(
        "--image_name",
        type=str,
        default=None,
        help="Override the system-imaging image to build. Defaults to "
        "bty-<variant>-x86_64 (variant from [bty] in the cijoe config).",
    )


def main(args, cijoe):
    image_name = args.image_name or _default_image_name(cijoe)
    images = cijoe.getconf("system-imaging.images", {})
    image = images.get(image_name)
    if not image:
        log.error(f"Image '{image_name}' not found in config")
        return errno.EINVAL

    cloud = image.get("cloud", {})
    disk = image.get("disk", {})
    system_label = image.get("system_label")

    # Relative paths in the cijoe config resolve against the repo
    # root. cwd at run time is ``cijoe/`` (the Makefile cd's there
    # before invoking cijoe), so ``Path.cwd().parent`` is the repo
    # root. Absolute paths (e.g. cloud.path / disk.path with
    # ``{{ local.env.HOME }}``) pass through unchanged because
    # Python's ``Path("/abs") / "/other"`` returns the second path.
    repo_root = Path.cwd().parent
    cloud_image_path = repo_root / cloud["path"]
    cloud_image_url = cloud["url"]
    metadata_path = repo_root / cloud["metadata_path"]
    userdata_path = repo_root / cloud["userdata_path"]

    if not cloud_image_path.exists():
        cloud_image_path.parent.mkdir(parents=True, exist_ok=True)
        err = _download_with_retry(cloud_image_url, cloud_image_path)
        if err:
            log.error(f"Failed to download {cloud_image_url}")
            return err

    guest_name = None
    for name, guest_conf in cijoe.getconf("qemu.guests", {}).items():
        if guest_conf.get("system_label") == system_label:
            guest_name = name
            break

    if not guest_name:
        log.error(f"No guest found with system_label={system_label}")
        return errno.EINVAL

    guest = Guest(cijoe, cijoe.config, guest_name)
    guest.kill()
    guest.initialize(cloud_image_path)

    log.info(f"Resizing boot image to {DISK_SIZE}")
    err, _ = cijoe.run_local(f"qemu-img resize {guest.boot_img} {DISK_SIZE}")
    if err:
        log.error("Failed to resize boot image")
        return err

    guest_metadata = guest.guest_path / "meta-data"
    guest_userdata = guest.guest_path / "user-data"
    # Check the cp outcomes -- a missing source (e.g. gen_userdata
    # didn't run) would otherwise surface as a confusing mkisofs
    # "input file missing" several lines below.
    err, _ = cijoe.run_local(f"cp {metadata_path} {guest_metadata}")
    if err:
        log.error(f"Failed copying metadata {metadata_path} -> {guest_metadata}")
        return err
    err, _ = cijoe.run_local(f"cp {userdata_path} {guest_userdata}")
    if err:
        log.error(f"Failed copying userdata {userdata_path} -> {guest_userdata}")
        return err

    seed_img = guest.guest_path / "seed.img"
    mkisofs_cmd = " ".join(
        [
            "mkisofs",
            "-output",
            str(seed_img),
            "-volid",
            "cidata",
            "-joliet",
            "-rock",
            str(guest_userdata),
            str(guest_metadata),
        ]
    )
    err, _ = cijoe.run_local(mkisofs_cmd)
    if err:
        log.error("Failed creating seed ISO")
        return err

    err = guest.start(daemonize=False, extra_args=["-cdrom", str(seed_img)])
    if err:
        log.error("Cloud-init provisioning failed")
        return err

    disk_path = Path(disk["path"])
    disk_path.parent.mkdir(parents=True, exist_ok=True)
    log.info("Compacting image (qemu-img convert -c)")
    err, _ = cijoe.run_local(f"qemu-img convert -O qcow2 -c {guest.boot_img} {disk_path}")
    if err:
        log.error(f"Failed compacting image to {disk_path}")
        return err

    cijoe.run_local(f"qemu-img info {disk_path}")
    # Record the basename (not the absolute build-host path) so
    # ``sha256sum -c`` works wherever the artifact lands; matches
    # img_gz_publish / live_build / usb_iso_build.
    err, _ = cijoe.run_local(
        f"sh -c 'cd {disk_path.parent} && sha256sum {disk_path.name} > {disk_path.name}.sha256'"
    )
    if err:
        log.error("Failed computing sha256sum")
        return err

    cijoe.run_local(f"ls -la {disk_path}")
    cijoe.run_local(f"cat {disk_path}.sha256")

    return 0


def _default_image_name(cijoe) -> str:
    bty = cijoe.getconf("bty", {})
    variant = bty.get("variant", "usb-x86")
    # Strip arch suffix to derive the role; image config keys stay
    # role-named (``bty-server-x86_64``) so published download URLs
    # don't churn when variant strings change.
    role = variant.split("-")[0]
    return f"bty-{role}-x86_64"
