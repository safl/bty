"""
USB auto-grow end-to-end test
=============================

Verifies ``bty-usb-grow.service`` extends the BTY_IMAGES exFAT
partition from its 32 MiB bake-time minimum to fill the underlying
disk on first boot. Boots the freshly-built .iso in QEMU on a 4 GiB
raw IDE disk (writable, with ~3 GiB free behind BTY_IMAGES), waits
for sshd to come up inside the live env, SSHes in to capture the
service state + journal trace + ``/run/bty-usb-grow.status``,
shuts the VM down cleanly, then inspects the disk via lsblk on a
host-side loop device.

Why SSH instead of relying on console + a timeout: a previous
generation of this test killed QEMU after 240s and inspected the
disk -- which could only tell us PARTITION-LEVEL outcomes ("grew"
or "didn't"), never WHY. When ``bty-usb-grow.service`` got skipped
by its ConditionPathExists silently (no console output, no
[FAILED]), the test produced "did not grow" with zero diagnostic.
The live env runs sshd on port 22 (lab credential ``root`` /
``bty``); reach in, dump state, end the iterate-blind loop.

Retargetable: False (sudo / losetup / parted / lsblk / qemu-kvm /
paramiko required; ubuntu-latest + the runner's KVM permission rule
covers all of them).
"""

from __future__ import annotations

import contextlib
import errno
import logging as log
import shutil
import socket
import subprocess
import time
from argparse import ArgumentParser
from pathlib import Path

ISO_BASENAME_GLOB = "bty-usb-x86_64-v*.iso"
TEST_DISK_BYTES = 4 * 1024 * 1024 * 1024  # 4 GiB target stick
BOOT_WINDOW_SEC = 240  # cold-boot + sshd-up in QEMU/KVM is well under this
SSH_USER = "root"  # matches the live env's banner: ``ssh root@... password: bty``
SSH_PASSWORD = "bty"
# 32 MiB at bake -> ~3.5 GiB after grow on a 4 GiB stick. 1 GiB is a
# comfortable floor that distinguishes "grew" from "did not grow".
MIN_GROWN_BYTES = 1 * 1024 * 1024 * 1024


def add_args(parser: ArgumentParser):
    del parser


def main(args, cijoe):
    del args
    cfg = cijoe.getconf("test.usb_grow", {})
    iso_dir = Path(cfg.get("iso_dir") or (Path.home() / "system_imaging" / "disk"))
    candidates = sorted(iso_dir.glob(ISO_BASENAME_GLOB))
    if not candidates:
        log.error(f"no {ISO_BASENAME_GLOB} found in {iso_dir} (did the usb-x86 build run?)")
        return errno.ENOENT
    src_iso = candidates[-1]  # sorted: highest-version .iso wins
    log.info(f"using {src_iso.name} ({src_iso.stat().st_size} bytes)")

    workspace = Path.cwd() / "_build" / "test-usb-grow"
    workspace.mkdir(parents=True, exist_ok=True)
    test_disk = workspace / "usb-grow-test.img"
    serial_log = workspace / "qemu.serial.log"

    log.info(f"staging {src_iso.name} -> {test_disk}")
    shutil.copy2(src_iso, test_disk)
    log.info(f"extending {test_disk} to {TEST_DISK_BYTES} bytes (4 GiB)")
    with test_disk.open("r+b") as fh:
        fh.truncate(TEST_DISK_BYTES)

    # Pre-boot: lsblk via a loop device so the operator running the
    # test locally can confirm the bake produced the expected layout
    # before we burn QEMU minutes. Loop is detached before QEMU boots.
    _loop_lsblk(cijoe, test_disk, label="pre-boot")

    # QEMU with: writable IDE disk (SYSLINUX boots via BIOS INT13, so
    # virtio-blk is invisible; IDE-hd is also writable so bty-usb-grow
    # can parted+mkfs.exfat the partition). user-mode NIC with sshd
    # port forwarded so we can introspect from outside.
    ssh_port = _free_port()
    qemu = _start_qemu(test_disk, serial_log, ssh_port)
    try:
        log.info(f"waiting for sshd on 127.0.0.1:{ssh_port} (up to {BOOT_WINDOW_SEC}s)")
        if not _wait_until(
            lambda: _ssh_ready("127.0.0.1", ssh_port),
            BOOT_WINDOW_SEC,
            "sshd inside the live env",
        ):
            log.error("sshd never came up; serial-log tail:")
            _dump_serial(serial_log)
            return errno.ETIMEDOUT

        # In-VM diagnostics. Every command's output lands in the cijoe
        # report so a future failure leaves an actionable trail.
        in_vm_diagnostics = [
            ("disk geometry the kernel sees", "lsblk -f /dev/sda"),
            ("by-label symlinks udev created", "ls -la /dev/disk/by-label/ 2>&1 || true"),
            ("blkid view of every block device", "blkid"),
            (
                "bty-usb-grow unit state",
                "systemctl status bty-usb-grow.service --no-pager --full || true",
            ),
            (
                "bty-usb-grow journal trace",
                "journalctl -u bty-usb-grow.service --no-pager --no-hostname || true",
            ),
            (
                "/run/bty-usb-grow.status (script's own log)",
                "cat /run/bty-usb-grow.status 2>&1 || echo NO_STATUS_FILE",
            ),
            ("parted view of /dev/sda inside the VM", "parted -ms /dev/sda unit B print || true"),
        ]
        for label, cmd in in_vm_diagnostics:
            out = _ssh_run("127.0.0.1", ssh_port, cmd)
            log.info(f"--- in-VM: {label} ---\n$ {cmd}\n{out}")

        # Clean shutdown so the kernel flushes the partition table.
        log.info("requesting clean poweroff via SSH")
        _ssh_run("127.0.0.1", ssh_port, "systemctl poweroff -i || poweroff -f")
    finally:
        try:
            qemu.wait(timeout=30)
            log.info(f"qemu exited (rc={qemu.returncode})")
        except subprocess.TimeoutExpired:
            log.info("qemu didn't exit on its own after poweroff; terminating")
            qemu.terminate()
            try:
                qemu.wait(timeout=10)
            except subprocess.TimeoutExpired:
                qemu.kill()
                qemu.wait(timeout=5)

    _dump_serial(serial_log)

    # Post-boot inspection.
    largest = _loop_lsblk(cijoe, test_disk, label="post-boot")
    log.info(f"largest partition on disk image: {largest} bytes ({largest / (1 << 20):.1f} MiB)")
    if largest < MIN_GROWN_BYTES:
        log.error(
            f"FAIL: BTY_IMAGES did not grow. Largest partition is "
            f"{largest} bytes ({largest / (1 << 20):.1f} MiB); expected "
            f">= {MIN_GROWN_BYTES} bytes ({MIN_GROWN_BYTES / (1 << 30):.1f} GiB). "
            f"Inspect the in-VM diagnostics dumped above."
        )
        return errno.EPROTO

    log.info(
        f"PASS: BTY_IMAGES grew to {largest / (1 << 30):.2f} GiB on first boot "
        f"(bake-time min was 32 MiB; target stick was 4 GiB)."
    )
    return 0


def _start_qemu(test_disk: Path, serial_log: Path, ssh_port: int) -> subprocess.Popen:
    """Launch QEMU in the background with serial captured to a file
    and sshd port-forwarded. Returns the process handle so the caller
    can ``wait()`` or ``kill()`` it.
    """
    cmd = [
        "qemu-system-x86_64",
        "-enable-kvm",
        "-cpu",
        "host",
        "-smp",
        "2",
        "-m",
        "1G",
        # ``if=ide`` rather than ``if=virtio``: SYSLINUX (the iso-hybrid
        # bootloader) reads files via BIOS INT13, which only sees
        # disks the BIOS enumerated. virtio bypasses BIOS, so the
        # bootloader can't find /live/vmlinuz before the kernel comes
        # up. IDE-hd is also writable, which bty-usb-grow needs for
        # parted resizepart + mkfs.exfat.
        "-drive",
        f"file={test_disk},format=raw,if=ide",
        "-nographic",
        "-serial",
        f"file:{serial_log}",
        # User-mode NIC with sshd port-forward so the test SSHes in
        # for in-VM diagnostics. No external network is needed; the
        # NAT inside user-mode is enough for sshd to bind and accept.
        "-nic",
        f"user,model=e1000,hostfwd=tcp:127.0.0.1:{ssh_port}-:22",
        "-no-reboot",
    ]
    log.info(f"launching qemu (ssh: 127.0.0.1:{ssh_port}, serial: {serial_log})")
    return subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _free_port() -> int:
    """Return a free TCP port on the loopback (race-y but good enough
    for a single-process test runner). Mirrors the helper in
    ``pxe_run_chain_test``."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_until(predicate, timeout: float, what: str) -> bool:
    """Poll ``predicate()`` once a second until it returns truthy or
    the deadline elapses. Returns True on success; logs + returns
    False on timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if predicate():
                return True
        except Exception as exc:
            log.debug(f"{what} predicate raised {exc!r}; retrying")
        time.sleep(1)
    log.error(f"{what}: timed out after {timeout}s")
    return False


def _ssh_ready(host: str, port: int) -> bool:
    """Quick check that sshd is accepting connections: open a TCP
    socket, read the server banner, look for ``SSH-``."""
    try:
        with socket.create_connection((host, port), timeout=2) as sock:
            banner = sock.recv(64)
        return banner.startswith(b"SSH-")
    except OSError:
        return False


def _ssh_run(host: str, port: int, cmd: str) -> str:
    """Run ``cmd`` on the live env via paramiko, return stdout + stderr
    merged. Connects fresh per call (the live env is small + fast;
    keeping a single session across commands isn't worth the
    state-management cost).
    """
    import paramiko

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(
            hostname=host,
            port=port,
            username=SSH_USER,
            password=SSH_PASSWORD,
            allow_agent=False,
            look_for_keys=False,
            timeout=10,
        )
        _stdin, stdout, stderr = client.exec_command(cmd, timeout=30)
        out = stdout.read().decode("utf-8", errors="replace")
        err = stderr.read().decode("utf-8", errors="replace")
        return out + (f"\n[stderr]\n{err}" if err.strip() else "")
    except Exception as exc:
        return f"[ssh exception: {type(exc).__name__}: {exc}]"
    finally:
        with contextlib.suppress(Exception):
            client.close()


def _loop_lsblk(cijoe, disk_file: Path, *, label: str) -> int:
    """Attach ``disk_file`` as a loop device with -P, dump lsblk to
    the report, return the largest partition's size in bytes. The
    loop is detached before this returns.
    """
    log.info(f"--- {label}: loop-mount + lsblk on {disk_file} ---")
    err, out = cijoe.run_local(f"sudo losetup -fP --show {disk_file}")
    if err:
        log.error(f"losetup -fP failed (err={err}); returning 0")
        return 0
    loop_dev = out.output().strip().splitlines()[-1].strip() if hasattr(out, "output") else ""
    if not loop_dev.startswith("/dev/loop"):
        log.error(f"unexpected losetup output: {out!r}")
        return 0
    log.info(f"loop device: {loop_dev}")
    largest = 0
    try:
        cijoe.run_local("sudo udevadm settle --timeout=10")
        err, lsblk_out = cijoe.run_local(f"sudo lsblk -bno NAME,SIZE,TYPE,LABEL {loop_dev}")
        if err:
            log.error(f"lsblk failed (err={err})")
            return 0
        text = lsblk_out.output() if hasattr(lsblk_out, "output") else str(lsblk_out)
        # lsblk -bno NAME,SIZE,TYPE,LABEL: one row per device.
        # The largest ``part`` is the one that grew (or didn't).
        for line in text.splitlines():
            parts = line.split()
            if len(parts) < 3:
                continue
            try:
                size = int(parts[1])
            except ValueError:
                continue
            kind = parts[2]
            if kind != "part":
                continue
            if size > largest:
                largest = size
    finally:
        cijoe.run_local(f"sudo losetup -d {loop_dev}")
    return largest


def _dump_serial(serial_log: Path) -> None:
    if not serial_log.is_file():
        log.error(f"serial log missing: {serial_log}")
        return
    log.info(f"--- serial log ({serial_log.stat().st_size} bytes) ---")
    log.info(serial_log.read_text(encoding="utf-8", errors="replace"))
