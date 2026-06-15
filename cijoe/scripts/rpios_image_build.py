"""
Build the bty USB-Pi flasher by customizing Raspberry Pi OS in-place
====================================================================

The x86 bty variants (netboot-pc / usbboot-pc) are live-build images: a
squashfs + a live-boot initrd that mounts it. That model fights the
Raspberry Pi badly -- the Pi boots via the VideoCore firmware +
``config.txt`` chain, the vendor kernels are split per-SoC
(``linux-image-rpi-2712`` for BCM2712 vs ``linux-image-rpi-v8`` for
BCM2711/2837), and a single live-initrd can only carry one kernel's
modules. The previous live-build ``usbboot-rpi`` path shipped a boot
partition with no device trees at all and could not boot a CM5.

This build takes the opposite, much simpler tack -- the same one
``nosi``'s Pi image uses: customize the official **Raspberry Pi OS
Lite (arm64)** image in place. RPiOS already ships every Pi kernel,
every ``bcm*.dtb`` (incl. the CM5 / CM5IO device trees), the firmware
and the bootloader, so the result boots Pi 4 / CM4 / Pi 5 / CM5 / Zero
2 W with zero per-board branching. We only graft bty on top:

  1. download + ``xz -d`` the Raspberry Pi OS ``.img.xz`` (cached),
  2. copy it to the working path and grow the ext4 root for headroom
     (the bty venv + flash tooling need room; RPiOS's own first-boot
     resize then expands root to fill the USB on the target),
  3. ``losetup -P`` + mount root (+ the FAT firmware partition), bind
     /dev /proc /sys /run + the host resolv.conf,
  4. ``apt-get install`` the bty runtime + flash tooling,
  5. drop the ``includes.chroot/`` tree in (services, scripts, banner,
     the staged wheel) and run the existing bty chroot hooks
     (``0500-bty-install`` / ``0700-clock`` / ``0800-ssh`` /
     ``0900-enable-services``) verbatim -- they are plain POSIX sh and
     chroot-portable,
  6. mask RPiOS's interactive first-boot ``userconfig`` wizard so the
     box boots straight into the bty TUI on tty1, strip per-instance
     identity, clean caches,
  7. unmount and gzip the raw ``.img`` to ``.img.gz`` (+ sha256).

Output: ``bty-usbboot-rpi-arm64-v<version>.img.gz``. Operators ``dd`` it to
a USB stick, plug into any supported Pi, and land in the bty TUI to
flash the box's eMMC / NVMe from the catalog.

Runs natively on an arm64 host/runner (no qemu-user emulation). Needs
root (loop devices, mount, chroot) via passwordless sudo.

Retargetable: False
"""

from __future__ import annotations

import errno
import json
import logging as log
import os
from argparse import ArgumentParser
from pathlib import Path

PUBLISH_BASENAME_FMT = "bty-usbboot-rpi-arm64-v{version}.img.gz"

# bty runtime + flash tooling installed into the RPiOS chroot. Union of
# the live-build lists bty-base.list.chroot (the arch-agnostic runtime)
# and bty-flash.list.chroot, minus the bits RPiOS already owns or that
# do not apply: live-boot/live-config/live-tools (not a live image),
# the kernel / raspi-firmware / microcode (RPiOS ships its own), and the
# x86-only r8125 DKMS build env. apt no-ops anything already present.
BTY_PACKAGES = [
    # runtime
    "python3",
    "python3-venv",
    "git",
    "curl",
    "ca-certificates",
    # network + time + remote access
    "network-manager",
    "systemd-timesyncd",
    "openssh-server",
    # Realtek 2.5GbE NIC firmware + offload control (USB-NIC adapters)
    "firmware-realtek",
    "ethtool",
    # hardware discovery (bty posts an lshw inventory)
    "lshw",
    "pciutils",
    "usbutils",
    # flash dependencies
    "qemu-utils",
    "nvme-cli",
    "parted",
    "gdisk",
    "dosfstools",
    "e2fsprogs",
    "exfatprogs",
    "zstd",
    "efibootmgr",
]

# bty chroot hooks to run verbatim inside the RPiOS chroot. Order
# matches the live-build sort: install bty, then the clock / ssh /
# service-enable tweaks. Skipped: 0600-r8125-dkms (x86-only; its own
# arch guard would early-exit anyway), 0500-skip-bootloader-menu
# (a .binary hook for the x86 ISO menu), 0980-apt-validate (asserts the
# live-build apt sources; RPiOS has its own).
BTY_HOOKS = [
    "0500-bty-install.hook.chroot",
    "0700-bty-clock-from-http.hook.chroot",
    "0800-bty-ssh-live.hook.chroot",
    "0900-bty-enable-services.hook.chroot",
]


def add_args(parser: ArgumentParser):
    del parser  # no flags; signature kept for cijoe consistency


def main(args, cijoe) -> int:
    del args
    cijoe_dir = Path.cwd()
    repo_root = cijoe_dir.parent
    bty_media = repo_root / "bty-media"

    variant = cijoe.getconf("bty", {}).get("variant", "")
    if variant != "usbboot-rpi":
        log.info(
            f"Skipping rpios_image_build (variant={variant!r}; only 'usbboot-rpi' customizes RPiOS)"
        )
        return 0

    images = cijoe.getconf("system-imaging.images", {})
    image = images.get("bty-usbboot-rpi-arm64")
    if not image:
        log.error("missing system-imaging.images.bty-usbboot-rpi-arm64 in config")
        return errno.EINVAL

    publish_dir = Path(image.get("publish", {}).get("dir", ""))
    if not str(publish_dir):
        log.error("system-imaging.images.bty-usbboot-rpi-arm64.publish.dir is unset")
        return errno.EINVAL
    publish_dir.mkdir(parents=True, exist_ok=True)

    version = _read_bty_version(cijoe_dir)
    log.info(f"bty-usbboot-rpi: build version {version}")

    # ---- 1. obtain a decompressed RPiOS base image (cached) ---------------
    base_img = _fetch_base_image(cijoe, image)
    if base_img is None:
        return errno.EIO

    # ---- 2. copy + grow the working image ---------------------------------
    disk_path = Path(image["disk"]["path"])
    disk_path.parent.mkdir(parents=True, exist_ok=True)
    err, _ = cijoe.run_local(f"cp -f --reflink=auto {base_img} {disk_path}")
    if err:
        log.error(f"failed copying base image -> {disk_path}")
        return err

    grow = image.get("grow", "3G")
    if grow:
        err, _ = cijoe.run_local(f"truncate -s +{grow} {disk_path}")
        if err:
            log.error("truncate (grow image) failed")
            return err

    # ---- 3. customize the rootfs in a chroot ------------------------------
    rc = _customize(cijoe, bty_media, disk_path, grow=bool(grow), version=version)
    if rc:
        return rc

    # ---- 4. publish: gzip the raw .img + sha256 sidecar -------------------
    out_img = publish_dir / PUBLISH_BASENAME_FMT.format(version=version)
    log.info(f"gzipping -> {out_img}")
    err, _ = cijoe.run_local(f"sh -c 'gzip -9 -c {disk_path} > {out_img}'")
    if err:
        log.error("gzip of the raw image failed")
        return err

    err, _ = cijoe.run_local(
        f"sh -c 'cd {out_img.parent} && sha256sum {out_img.name} > {out_img.name}.sha256'"
    )
    if err:
        log.error("sha256 sidecar write failed")
        return err

    log.info("Published artifacts:")
    cijoe.run_local(f"ls -la {publish_dir}/bty-usbboot-rpi-arm64-*")
    return 0


# ---------------------------------------------------------------------------
# base image acquisition
# ---------------------------------------------------------------------------


def _fetch_base_image(cijoe, image: dict) -> Path | None:
    """Download the Raspberry Pi OS ``.img.xz`` (cached) and return a path
    to a decompressed ``.img`` (cached next to it)."""
    cloud = image.get("cloud", {})
    url = cloud.get("url")
    xz_path = Path(cloud.get("path", ""))
    if not url or not str(xz_path):
        log.error("system-imaging.images.bty-usbboot-rpi-arm64.cloud.{url,path} unset")
        return None

    if not xz_path.exists():
        xz_path.parent.mkdir(parents=True, exist_ok=True)
        # -L: the _latest endpoint is a redirect to the dated image.
        err, _ = cijoe.run_local(f"curl -fL -o {xz_path} {url}")
        if err:
            log.error(f"Failed to download {url}")
            xz_path.unlink(missing_ok=True)
            return None

    if not xz_path.name.endswith(".img.xz"):
        log.error(f"expected a .img.xz base image path, got {xz_path.name}")
        return None

    img_path = xz_path.with_name(xz_path.name[: -len(".xz")])  # strip .xz
    if not img_path.exists():
        log.info(f"Decompressing {xz_path.name} -> {img_path.name}")
        err, _ = cijoe.run_local(f"sh -c 'xz -dkc {xz_path} > {img_path}'")
        if err:
            log.error(f"Failed to xz-decompress {xz_path}")
            img_path.unlink(missing_ok=True)
            return None
    return img_path


# ---------------------------------------------------------------------------
# rootfs customization (loop-mount + chroot)
# ---------------------------------------------------------------------------


def _customize(cijoe, bty_media: Path, disk_path: Path, *, grow: bool, version: str) -> int:
    mnt = Path(f"/mnt/bty-rpios-{os.getpid()}")
    cleanup: list[str] = []
    try:
        loopdev = _losetup_attach(cijoe, disk_path)
        if not loopdev:
            log.error("losetup failed")
            return errno.EIO
        cleanup.append(f"sudo losetup -d {loopdev} 2>/dev/null || true")

        root_part, boot_part = _partitions(cijoe, loopdev)
        if not root_part:
            log.error(f"no ext4 root partition found on {loopdev}")
            return errno.ENODEV

        if grow:
            rc = _grow_partition(cijoe, loopdev, root_part)
            if rc:
                return rc

        cijoe.run_local(f"sudo mkdir -p {mnt}")
        err, _ = cijoe.run_local(f"sudo mount {root_part} {mnt}")
        if err:
            log.error(f"mount {root_part} failed")
            return err
        # cleanup runs in reverse: rmdir appended before the umount that
        # must precede it. ``umount -R`` also catches the nested boot mount.
        cleanup.append(f"sudo rmdir {mnt} 2>/dev/null || true")
        cleanup.append(f"sudo umount -R {mnt} 2>/dev/null || true")

        # Raspberry Pi OS mounts the FAT firmware partition at
        # /boot/firmware; mount it so any kernel/initramfs postinst
        # writes where the Pi expects them.
        if boot_part:
            cijoe.run_local(f"sudo mkdir -p {mnt}/boot/firmware")
            err, _ = cijoe.run_local(f"sudo mount {boot_part} {mnt}/boot/firmware")
            if err:
                log.error(f"mount {boot_part} (boot/firmware) failed")
                return err

        for sub in ("dev", "proc", "sys", "run"):
            err, _ = cijoe.run_local(f"sudo mount --bind /{sub} {mnt}/{sub}")
            if err:
                log.error(f"bind-mount {sub} failed")
                return err
            cleanup.append(f"sudo umount {mnt}/{sub} 2>/dev/null || true")
        # /dev is a non-recursive bind, so the host devpts submount is not
        # carried in; mount a fresh one so apt + maintainer scripts get ptys.
        cijoe.run_local(f"sudo mount -t devpts devpts {mnt}/dev/pts 2>/dev/null || true")
        cleanup.append(f"sudo umount {mnt}/dev/pts 2>/dev/null || true")
        err, _ = cijoe.run_local(f"sudo mount --bind /etc/resolv.conf {mnt}/etc/resolv.conf")
        if err:
            log.error("bind-mount /etc/resolv.conf failed")
            return err
        cleanup.append(f"sudo umount {mnt}/etc/resolv.conf 2>/dev/null || true")

        return _provision(cijoe, bty_media, mnt, version)
    finally:
        for cmd in reversed(cleanup):
            cijoe.run_local(cmd)


def _provision(cijoe, bty_media: Path, mnt: Path, version: str) -> int:
    # 1. Install the bty runtime + flash tooling. apt no-ops packages
    #    RPiOS already carries.
    install = (
        "apt-get update && "
        "DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "
        + " ".join(BTY_PACKAGES)
    )
    err, _ = cijoe.run_local(f"sudo chroot {mnt} /bin/sh -c {_q(install)}")
    if err:
        log.error("chroot apt-get install (bty packages) failed")
        return err

    # 2. Drop the includes.chroot tree (service units, /usr/local/sbin
    #    scripts, /etc/issue|motd, profile.d, modprobe.d, udev rules, and
    #    the staged bty wheel under /opt/bty) onto the rootfs, exactly as
    #    live-build's includes pipeline would.
    includes = bty_media / "live-build" / "config" / "includes.chroot"
    if not includes.is_dir():
        log.error(f"includes.chroot tree missing: {includes}")
        return errno.ENOENT
    err, _ = cijoe.run_local(f"sudo cp -a {includes}/. {mnt}/")
    if err:
        log.error("copying includes.chroot into the rootfs failed")
        return err
    # live-build does not preserve +x on includes; the bty units exec
    # these wrappers, so make them all executable (the hooks chmod the
    # few they touch, but be exhaustive).
    cijoe.run_local(f"sudo sh -c 'chmod 0755 {mnt}/usr/local/sbin/bty-* 2>/dev/null || true'")

    # 3. Stamp the bty version into the __BTY_VERSION__ placeholders
    #    (/etc/issue, /etc/motd, /etc/profile.d/bty-version.sh).
    stamp = (
        f"grep -rlF __BTY_VERSION__ {mnt}/etc 2>/dev/null | "
        f"xargs --no-run-if-empty sed -i s/__BTY_VERSION__/{version}/g"
    )
    cijoe.run_local(f"sudo sh -c {_q(stamp)}")

    # 4. Run the bty chroot hooks verbatim (install bty into a venv,
    #    finalize clock + ssh, enable the bty services).
    hooks_dir = bty_media / "live-build" / "config" / "hooks" / "normal"
    cijoe.run_local(f"sudo mkdir -p {mnt}/tmp/bty-hooks")
    for hook in BTY_HOOKS:
        src = hooks_dir / hook
        if not src.is_file():
            log.error(f"bty hook missing: {src}")
            return errno.ENOENT
        cijoe.run_local(f"sudo cp {src} {mnt}/tmp/bty-hooks/{hook}")
        err, _ = cijoe.run_local(f"sudo chroot {mnt} /bin/sh /tmp/bty-hooks/{hook}")
        if err:
            log.error(f"bty hook failed in chroot: {hook}")
            return err
    cijoe.run_local(f"sudo rm -rf {mnt}/tmp/bty-hooks")

    # 5. Suppress Raspberry Pi OS's interactive first-boot user wizard so
    #    the box boots straight into bty-on-tty1 (which Conflicts
    #    getty@tty1). The firstboot resize + ssh-host-key regen paths are
    #    left intact. Belt-and-braces ssh-keygen oneshot below covers
    #    RPiOS variants that lack the regen service (0800 deleted the
    #    baked host keys so two flashed sticks are not SSH twins).
    rpi_tweaks = (
        "systemctl mask userconfig.service 2>/dev/null || true; "
        "systemctl mask cancel-rename.service 2>/dev/null || true; "
        "systemctl disable userconfig.service 2>/dev/null || true; "
        "systemctl enable getty@tty1.service 2>/dev/null || true; "
        "true"
    )
    cijoe.run_local(f"sudo chroot {mnt} /bin/sh -c {_q(rpi_tweaks)}")
    _install_ssh_regen(cijoe, mnt)

    # 6. This cut ships no BTY_IMAGES partition (the catalog/oras flow
    #    does not need it). The 0900 hook enabled var-lib-bty-images.mount,
    #    whose What=/dev/disk/by-label/BTY_IMAGES pulls an implicit .device
    #    dependency that blocks boot for the full device timeout (~90s) when
    #    the partition is absent -- ConditionPathExists skips the mount
    #    action but not that wait. Drop it from the boot transaction;
    #    bty-images-discover still scans and bty falls back to catalog mode.
    #    bty-usb-grow.service self-skips via its own ConditionPathExists
    #    (no What=, so no blocking device dependency) and is left alone.
    no_btyimages = (
        "systemctl disable var-lib-bty-images.mount 2>/dev/null || true; "
        "rm -f /etc/systemd/system/multi-user.target.wants/var-lib-bty-images.mount; "
        "true"
    )
    cijoe.run_local(f"sudo chroot {mnt} /bin/sh -c {_q(no_btyimages)}")

    # 7. Strip per-instance identity so two flashes are not twins.
    #    (0800 already removed the ssh host keys.)
    strip = (
        ": > /etc/machine-id; "
        "rm -f /var/lib/dbus/machine-id; "
        "apt-get clean; "
        "rm -rf /var/lib/apt/lists/* /tmp/* /var/tmp/* /root/.cache /home/*/.cache"
    )
    cijoe.run_local(f"sudo chroot {mnt} /bin/sh -c {_q(strip)}")
    return 0


def _install_ssh_regen(cijoe, mnt: Path) -> None:
    """Write + enable a self-contained oneshot that regenerates the SSH
    host keys on first boot if absent. 0800-bty-ssh-live deleted the
    bake-time keys; RPiOS usually ships ``regenerate-ssh-host-keys`` but
    do not depend on it -- a sshd with no host keys fails to start."""
    unit = (
        "[Unit]\n"
        "Description=Regenerate SSH host keys if missing (bty)\n"
        "ConditionPathExists=!/etc/ssh/ssh_host_ed25519_key\n"
        "Before=ssh.service\n"
        "\n"
        "[Service]\n"
        "Type=oneshot\n"
        "ExecStart=/usr/bin/ssh-keygen -A\n"
        "RemainAfterExit=yes\n"
        "\n"
        "[Install]\n"
        "WantedBy=multi-user.target\n"
    )
    dst = mnt / "etc/systemd/system/bty-ssh-regen.service"
    _write_root_file(cijoe, dst, unit, "0644")
    cijoe.run_local(
        f"sudo ln -sf /etc/systemd/system/bty-ssh-regen.service "
        f"{mnt}/etc/systemd/system/multi-user.target.wants/bty-ssh-regen.service"
    )


# ---------------------------------------------------------------------------
# disk + partition helpers
# ---------------------------------------------------------------------------


def _losetup_attach(cijoe, img: Path) -> str | None:
    """Attach ``img`` to a free loop device with partition scanning."""
    out_file = Path(f"/tmp/bty-rpios-loop-{os.getpid()}")
    try:
        err, _ = cijoe.run_local(f"sh -c 'sudo losetup -fP --show {img} > {out_file}'")
        if err or not out_file.exists():
            return None
        loop = out_file.read_text().strip()
    finally:
        out_file.unlink(missing_ok=True)
    cijoe.run_local("sudo udevadm settle --timeout=10 >/dev/null 2>&1 || true")
    return loop or None


def _partitions(cijoe, loopdev: str, attempts: int = 10):
    """Return (root_part, boot_part): the largest ext4 partition (rootfs)
    and the FAT partition (Pi firmware), as /dev paths. Retries because
    the partition nodes can lag the loop attach even after settle."""
    out_file = Path(f"/tmp/bty-rpios-lsblk-{os.getpid()}.json")
    for attempt in range(attempts):
        data = {}
        try:
            err, _ = cijoe.run_local(
                f"sh -c 'sudo lsblk -J -b -o NAME,FSTYPE,SIZE,TYPE {loopdev} > {out_file}'"
            )
            if err == 0 and out_file.exists():
                try:
                    data = json.loads(out_file.read_text())
                except (json.JSONDecodeError, OSError):
                    data = {}
        finally:
            out_file.unlink(missing_ok=True)

        root = None  # (size, name)
        boot = None
        for dev in data.get("blockdevices", []):
            for part in dev.get("children") or []:
                if part.get("type") != "part":
                    continue
                fstype = part.get("fstype")
                name = part.get("name")
                size = int(part.get("size") or 0)
                if fstype in ("ext4", "ext3", "ext2") and (root is None or size > root[0]):
                    root = (size, name)
                elif fstype in ("vfat", "fat", "fat32") and boot is None:
                    boot = name
        if root:
            root_part = f"/dev/{root[1]}"
            boot_part = f"/dev/{boot}" if boot else None
            return root_part, boot_part
        if attempt < attempts - 1:
            cijoe.run_local("sleep 1")
    return None, None


def _grow_partition(cijoe, loopdev: str, root_part: str) -> int:
    """Expand the ext4 root partition + filesystem to fill the (already
    grown) backing file. Operates on the live loop device."""
    partnum = root_part[len(loopdev) :].lstrip("p")
    cijoe.run_local(f"sudo growpart {loopdev} {partnum}")
    cijoe.run_local(f"sudo partprobe {loopdev} >/dev/null 2>&1 || true")
    cijoe.run_local("sudo udevadm settle >/dev/null 2>&1 || true")
    cijoe.run_local(f"sudo e2fsck -p -f {root_part} || true")
    err, _ = cijoe.run_local(f"sudo resize2fs {root_part}")
    if err:
        log.error("resize2fs failed")
        return err
    return 0


def _write_root_file(cijoe, path: Path, body: str, mode: str = "0644") -> None:
    """Write ``body`` to a root-owned path inside the rootfs via a host
    temp + sudo cp."""
    host_tmp = Path(f"/tmp/bty-rpios-{os.getpid()}.tmp")
    host_tmp.write_text(body)
    cijoe.run_local(f"sudo mkdir -p {path.parent}")
    cijoe.run_local(f"sudo cp {host_tmp} {path}")
    cijoe.run_local(f"sudo chmod {mode} {path}")
    host_tmp.unlink(missing_ok=True)


def _read_bty_version(cijoe_dir: Path) -> str:
    """Read the bty-lab version from the repo's top-level pyproject.toml."""
    pyproject = cijoe_dir.parent / "pyproject.toml"
    for line in pyproject.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("version") and "=" in stripped:
            return stripped.split("=", 1)[1].strip().strip('"').strip("'")
    raise RuntimeError(f"could not find version line in {pyproject}")


def _q(s: str) -> str:
    """Single-quote a string for safe use as one ``sh -c`` argument."""
    return "'" + s.replace("'", "'\\''") + "'"
