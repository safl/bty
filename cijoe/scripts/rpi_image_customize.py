"""
Customise a Raspberry Pi OS Lite arm64 image into a bty-server SD-card
=====================================================================

Drives a Pi-friendly equivalent of the x86 ``diskimage_build`` flow:

1. Download the upstream Raspberry Pi OS Lite arm64 image (.img.xz).
2. Decompress and grow the raw image to the configured size.
3. Loop-mount the image; resize the rootfs partition to fill the
   grown space; mount root + boot partitions on the build host.
4. Stage the bty rootfs overlay (``bty-media/rootfs/server/``) and the
   bty-lab wheel (built by ``bty_wheel_stage`` earlier in the task)
   into the chroot.
5. Drop ``qemu-aarch64-static`` into the chroot so binfmt_misc can
   transparently exec arm64 binaries on the amd64 build host.
6. ``chroot`` in and run the bty install: apt packages, ``bty`` /
   ``odus`` users with default passwords, the bty-lab venv, service
   enables.
7. Tear down mounts, detach the loop device, ``zstd`` the raw image
   into ``bty-server-rpi-arm64.img.gz``, and write a sha256 manifest.

Build-host requirements: passwordless sudo, ``qemu-aarch64-static``,
``binfmt_misc`` registered for arm64, ``losetup``, ``parted``,
``e2fsprogs``, ``xz-utils``, ``zstd``. The script aborts loudly if any
are missing.

Skipped for any variant other than ``server-rpi``.

Retargetable: False
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import logging as log
import shutil
import subprocess
import textwrap
from argparse import ArgumentParser
from pathlib import Path

from cijoe.core.misc import download

VARIANT = "server-rpi"
IMAGE_NAME = f"bty-{VARIANT}-arm64"

# Apt packages installed inside the arm64 chroot. Kept minimal:
# python3-venv for the bty-web venv, dnsmasq for TFTP (bty doesn't
# do DHCP), ipxe for the kpxe / efi binaries served via TFTP,
# qemu-utils for ``qemu-img`` (used by the ``bty`` wizard's image
# probe + flash pipeline), zstd for image-decompression on-target.
APT_PACKAGES = (
    "python3-venv",
    "python3-pip",
    "dnsmasq",
    "ipxe",
    "qemu-utils",
    "zstd",
    "ca-certificates",
)

# Things the bty-server image expects to exist on disk that the upstream
# Pi OS image doesn't ship. Added at chroot time.
STATE_DIRS = (
    ("/var/lib/bty", "0750"),
    ("/var/lib/bty/images", "0750"),
    ("/var/lib/bty/boot", "0750"),
    ("/var/lib/bty/workflows", "0750"),
)


def add_args(parser: ArgumentParser):
    del parser  # no flags; signature kept for cijoe consistency


def main(args, cijoe):
    del args
    variant = cijoe.getconf("bty", {}).get("variant", "")
    if variant != VARIANT:
        log.info(
            f"Skipping rpi_image_customize (variant={variant!r}; "
            f"only {VARIANT!r} runs Pi customisation)"
        )
        return 0

    images = cijoe.getconf("system-imaging.images", {})
    image = images.get(IMAGE_NAME)
    if not image:
        log.error(f"Image {IMAGE_NAME!r} not found in cijoe config")
        return errno.EINVAL

    repo_root = Path.cwd().parent
    bty_media = repo_root / "bty-media"

    source_url = image["source"]["url"]
    source_path = Path(image["source"]["path"])
    raw_path = Path(image["disk"]["raw_path"])
    target_size_gib = int(image["disk"]["target_size_gib"])
    gz_path = Path(image["publish"]["gz_path"])
    gzip_level = int(image["publish"].get("gzip_level", 9))

    # 1. Download
    if not source_path.exists():
        source_path.parent.mkdir(parents=True, exist_ok=True)
        log.info(f"Downloading {source_url} -> {source_path}")
        err, _ = download(source_url, source_path)
        if err:
            log.error(f"Download failed: {source_url}")
            return err

    # 2. Decompress + grow
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    if raw_path.exists():
        raw_path.unlink()
    log.info(f"Decompressing {source_path} -> {raw_path}")
    if (
        _run(["xz", "--decompress", "--keep", "--stdout", str(source_path)], stdout_path=raw_path)
        != 0
    ):
        return errno.EIO
    log.info(f"Growing {raw_path} to {target_size_gib} GiB")
    if _run(["truncate", "--size", f"{target_size_gib}G", str(raw_path)]) != 0:
        return errno.EIO

    # 3. Loop-mount and resize rootfs
    rc = _customize(raw_path, bty_media)
    if rc != 0:
        return rc

    # 4. Compress the customised image with gzip. bty's shipped
    # images use .gz for universal flasher compat (see
    # ``feedback_verify_flasher_compat`` memory note); operator-
    # supplied images are still accepted in any of
    # .img.{zst,xz,gz,bz2} via :mod:`bty.flash`.
    log.info(f"Compressing {raw_path} -> {gz_path} (gzip -{gzip_level})")
    if gz_path.exists():
        gz_path.unlink()
    # ``gzip -<level> -c`` writes to stdout; redirect captures the
    # output. Single-stream output is what every flasher / OS
    # tooling handles uniformly.
    if (
        _run_shell(
            f"gzip -{gzip_level} -c {raw_path} > {gz_path}",
        )
        != 0
    ):
        return errno.EIO

    # 5. sha256 manifest
    sha256_path = gz_path.with_suffix(gz_path.suffix + ".sha256")
    digest = _sha256(gz_path)
    sha256_path.write_text(f"{digest}  {gz_path.name}\n", encoding="utf-8")
    log.info(f"sha256 -> {sha256_path}")

    return 0


# -----------------------------------------------------------------------
def _customize(raw_path: Path, bty_media: Path) -> int:
    """Loop-mount, resize rootfs, run chroot customisation, tear down."""

    # losetup -P binds /dev/loopN with partition discovery (loopNp1, loopNp2).
    rc, loop = _losetup_attach(raw_path)
    if rc != 0:
        return rc

    try:
        # Pi OS Lite ships a ~512 MiB FAT boot partition (p1) and a small
        # ext4 rootfs (p2). Grow p2 to fill the space we created with
        # ``truncate``, then resize the filesystem to match.
        log.info(f"Growing rootfs partition on {loop}p2")
        if (
            _run_sudo(
                [
                    "parted",
                    "--script",
                    loop,
                    "resizepart",
                    "2",
                    "100%",
                ]
            )
            != 0
        ):
            return errno.EIO
        if _run_sudo(["e2fsck", "-fy", f"{loop}p2"]) not in (0, 1):
            return errno.EIO
        if _run_sudo(["resize2fs", f"{loop}p2"]) != 0:
            return errno.EIO

        mnt = Path("/tmp/bty-rpi-mnt")
        boot_mnt = mnt / "boot" / "firmware"
        mnt.mkdir(exist_ok=True)
        _run_sudo(["mount", f"{loop}p2", str(mnt)])
        boot_mnt.mkdir(parents=True, exist_ok=True)
        _run_sudo(["mount", f"{loop}p1", str(boot_mnt)])

        try:
            rc = _stage_and_chroot(mnt, bty_media)
        finally:
            _run_sudo(["umount", str(boot_mnt)])
            _run_sudo(["umount", str(mnt)])
            with contextlib.suppress(OSError):
                mnt.rmdir()
        return rc
    finally:
        _run_sudo(["losetup", "--detach", loop])


def _stage_and_chroot(mnt: Path, bty_media: Path) -> int:
    """Drop overlay files into the mounted rootfs and run chroot install."""

    # The bty rootfs overlay is shared with the x86 server image: service
    # units, sudoers, dnsmasq config, networkd config, /etc/issue, etc.
    rootfs_server = bty_media / "rootfs" / "server"
    if not rootfs_server.exists():
        log.error(f"Missing rootfs overlay: {rootfs_server}")
        return errno.ENOENT

    # The wheel staged by bty_wheel_stage lives under rootfs/server/opt/bty/.
    # Pick whichever bty_lab-*.whl is present.
    wheels = sorted((rootfs_server / "opt" / "bty").glob("bty_lab-*-py3-none-any.whl"))
    if not wheels:
        log.error("No bty_lab wheel staged under rootfs/server/opt/bty/")
        return errno.ENOENT
    wheel = wheels[-1]

    # Bulk-copy the overlay tree into the mounted rootfs. ``cp -a`` keeps
    # ownership/perms; we'll fix per-file ownership inside the chroot.
    log.info(f"Copying overlay {rootfs_server}/* into chroot")
    if (
        _run_sudo(
            [
                "cp",
                "-a",
                f"{rootfs_server}/.",
                str(mnt),
            ]
        )
        != 0
    ):
        return errno.EIO

    # qemu-aarch64-static lets binfmt_misc execve arm64 binaries on the
    # amd64 build host transparently inside the chroot.
    qemu_static = shutil.which("qemu-aarch64-static") or "/usr/bin/qemu-aarch64-static"
    if not Path(qemu_static).exists():
        log.error(
            "qemu-aarch64-static not found. Install with "
            "``sudo apt install qemu-user-static binfmt-support``."
        )
        return errno.ENOENT
    if _run_sudo(["cp", qemu_static, str(mnt / "usr/bin/")]) != 0:
        return errno.EIO

    # Bind-mount /proc, /sys, /dev, /dev/pts so apt/systemctl work.
    # The mounts live INSIDE the try so a partial-failure (e.g. the
    # 3rd bind fails) still hits the finally and unmounts whatever
    # already succeeded -- otherwise the leftover mounts make the
    # caller's ``umount <mnt>`` (and loop detach) fail with EBUSY.
    try:
        for src in ("/proc", "/sys", "/dev", "/dev/pts"):
            dst = mnt / src.lstrip("/")
            dst.mkdir(parents=True, exist_ok=True)
            if _run_sudo(["mount", "--bind", src, str(dst)]) != 0:
                return errno.EIO

        # Render the install script and drop it into the chroot.
        install_script = _render_install_script(wheel.name)
        script_path = mnt / "tmp" / "bty-rpi-install.sh"
        _run_sudo(["mkdir", "-p", str(mnt / "tmp")])
        with subprocess.Popen(
            ["sudo", "tee", str(script_path)],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
        ) as proc:
            proc.communicate(install_script.encode())
        _run_sudo(["chmod", "0755", str(script_path)])

        log.info("Running chroot bty install (apt + venv + users + services)")
        rc = _run_sudo(
            [
                "chroot",
                str(mnt),
                "/bin/bash",
                "/tmp/bty-rpi-install.sh",
            ]
        )
        _run_sudo(["rm", "-f", str(script_path)])
        if rc != 0:
            return errno.EIO
    finally:
        for src in ("/dev/pts", "/dev", "/sys", "/proc"):
            dst = mnt / src.lstrip("/")
            _run_sudo(["umount", str(dst)])

    return 0


def _render_install_script(wheel_filename: str) -> str:
    """Bash script that runs *inside* the arm64 chroot."""

    state_dir_lines = "\n".join(
        f"install -d -o bty -g bty -m {mode} {path}" for path, mode in STATE_DIRS
    )

    return textwrap.dedent(f"""
        #!/bin/bash
        # bty Raspberry Pi server-image customisation. Runs inside the
        # arm64 chroot transparently via qemu-aarch64-static + binfmt.
        set -euo pipefail

        export DEBIAN_FRONTEND=noninteractive

        # 1. Pi OS firstrun wizard (asks for username/password on first
        #    boot) -> disable so the appliance comes up unattended with
        #    our baked credentials.
        systemctl disable userconfig.service 2>/dev/null || true
        rm -f /etc/systemd/system/multi-user.target.wants/userconfig.service

        # 2. apt packages.
        apt-get update
        apt-get install -y --no-install-recommends {" ".join(APT_PACKAGES)}
        apt-get clean
        rm -rf /var/lib/apt/lists/*

        # 3. Users. ``bty`` is the unprivileged service principal whose
        #    OS password gates /ui/login (PAM); ``odus`` is the SSH
        #    admin with passwordless sudo. Same model as the x86 server
        #    image. Operators rotate both with ``passwd``.
        if ! id bty >/dev/null 2>&1; then
            useradd --system --no-create-home --shell /usr/sbin/nologin bty
        fi
        echo 'bty:bty' | chpasswd
        if ! id odus >/dev/null 2>&1; then
            useradd --create-home --shell /bin/bash --groups sudo odus
        fi
        echo 'odus:odus.321' | chpasswd
        printf 'odus ALL=(ALL) NOPASSWD:ALL\\n' > /etc/sudoers.d/odus
        chown root:root /etc/sudoers.d/odus
        chmod 0440 /etc/sudoers.d/odus

        # Lock root, kill the default ``pi`` user if it slipped in.
        passwd -l root
        if id pi >/dev/null 2>&1; then
            userdel -rf pi 2>/dev/null || true
        fi

        # 4. Drop the staged wheel into a fresh venv.
        python3 -m venv /opt/bty/venv
        /opt/bty/venv/bin/pip install --no-cache-dir --upgrade pip
        /opt/bty/venv/bin/pip install --no-cache-dir \\
            "/opt/bty/{wheel_filename}[web]"
        # Symlinks so /usr/local/bin is on PATH for everyone.
        for cmd in bty bty-web; do
            ln -sf /opt/bty/venv/bin/$cmd /usr/local/bin/$cmd
        done

        # 5. State dirs.
        {state_dir_lines}

        # 6. Sudoers + helper perms. bty-web-tftp is the only sudo'd
        #    helper as of v0.18 (start/stop/restart dnsmasq.service).
        chmod 0755 /usr/local/sbin/bty-web-tftp
        chmod 0755 /usr/local/sbin/bty-web-init
        chown root:root /etc/sudoers.d/bty-web
        chmod 0440 /etc/sudoers.d/bty-web

        # 7. Service enables. ``ssh`` so odus can SSH; ``systemd-networkd``
        #    is preferred over Pi OS's NetworkManager for predictable
        #    ``en*`` matching. ``bty-web``/``bty-web-init`` come from the
        #    overlay drop. ``bty-ssh-host-keys`` regenerates host keys on
        #    first boot (we delete the bake-time keys below).
        systemctl enable ssh systemd-networkd systemd-networkd-wait-online
        systemctl enable bty-web bty-web-init bty-ssh-host-keys
        systemctl disable NetworkManager 2>/dev/null || true
        systemctl disable dhcpcd 2>/dev/null || true

        # 8. dnsmasq ships with a sample /etc/dnsmasq.conf that conflicts
        #    with our ``conf-dir=/etc/dnsmasq.d`` overlay - silence it.
        if [ -f /etc/dnsmasq.conf ]; then
            mv /etc/dnsmasq.conf /etc/dnsmasq.conf.dist
            : > /etc/dnsmasq.conf
        fi

        # 9. SSH host-key hygiene. Pi OS's openssh-server postinst
        #    generated host keys during the upstream image build; remove
        #    them now so every operator who downloads
        #    bty-server-rpi-arm64.img.gz does NOT share an identical set
        #    of host keys with every other operator. bty-ssh-host-keys
        #    regenerates per-instance keys on first boot.
        rm -f /etc/ssh/ssh_host_*
    """).lstrip()


# -----------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------
def _run(cmd: list[str], stdout_path: Path | None = None) -> int:
    log.debug(" ".join(cmd))
    if stdout_path is not None:
        with stdout_path.open("wb") as fh:
            return subprocess.call(cmd, stdout=fh)
    return subprocess.call(cmd)


def _run_shell(cmdline: str) -> int:
    """Run a command via the shell so redirections (``>``, ``|``) work.

    Used for the gzip-to-stdout-redirected-to-file pattern where the
    gzip CLI's ``-c`` doesn't have an ``-o`` equivalent.
    """
    log.debug(f"sh -c '{cmdline}'")
    return subprocess.call(["sh", "-c", cmdline])


def _run_sudo(cmd: list[str]) -> int:
    log.debug("sudo " + " ".join(cmd))
    return subprocess.call(["sudo", *cmd])


def _losetup_attach(raw_path: Path) -> tuple[int, str]:
    """Attach raw_path to a loop device with --partscan; return /dev/loopN."""
    # ``losetup --find --show --partscan`` prints the assigned device.
    proc = subprocess.run(
        ["sudo", "losetup", "--find", "--show", "--partscan", str(raw_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        log.error(f"losetup attach failed: {proc.stderr.strip()}")
        return errno.EIO, ""
    loop = proc.stdout.strip()
    if not loop.startswith("/dev/"):
        log.error(f"losetup unexpected output: {proc.stdout!r}")
        return errno.EIO, ""
    log.info(f"losetup -> {loop}")
    return 0, loop


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()
