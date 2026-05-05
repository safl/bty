"""
Customise the server qcow2 for the PXE chain test
==================================================

Copies the pre-built ``bty-server-x86_64.qcow2`` from the artefact
dir into ``cijoe/_build/test-pxe/`` (the test workspace; gitignored
alongside the wheel-staging dir) and bakes the test config in via
``virt-customize``:

- ``/etc/default/bty-web`` with a known token so the test does not
  have to discover one at runtime.
- ``/etc/dnsmasq.d/bty-pxe-active.conf`` in **full DHCP** mode (the
  socket-only PXE segment has no other DHCP server, so proxy-DHCP
  has nothing to layer on). dnsmasq binds to the PXE NIC, hands out
  IPs in the configured range, and answers PXE queries with the
  iPXE binaries / chain URL.
- ``/etc/systemd/network/10-bty-pxe.network`` static-IPs the PXE NIC
  to ``server_pxe_ip``.
- ``bty-web-init.service`` is masked so the cooked image's first-
  boot init does not overwrite the pre-baked token.
- The live trio (``vmlinuz``, ``initrd``, ``squashfs``) is copied
  into ``/var/lib/bty/boot/`` so ``GET /boot/<name>`` resolves
  during the chain.

Output: ``cijoe/_build/test-pxe/server.qcow2`` ready to boot in the
next step.

Retargetable: False
"""

from __future__ import annotations

import errno
import logging as log
import shutil
from argparse import ArgumentParser
from pathlib import Path

ARTIFACT_NAMES = (
    "bty-live-x86_64.vmlinuz",
    "bty-live-x86_64.initrd",
    "bty-live-x86_64.squashfs",
)


def add_args(parser: ArgumentParser):
    del parser  # signature kept for cijoe consistency


def main(args, cijoe):
    del args
    cfg = cijoe.getconf("test.pxe", {})
    if not cfg:
        log.error("missing [test.pxe] section in cijoe config")
        return errno.EINVAL

    artifact_dir = Path(cfg["artifact_dir"])
    server_src = artifact_dir / "bty-server-x86_64.qcow2"
    if not server_src.is_file():
        log.error(f"server qcow2 not found: {server_src}")
        log.error("Run `make build VARIANT=server` from the repo root first")
        return errno.ENOENT
    for name in ARTIFACT_NAMES:
        if not (artifact_dir / name).is_file():
            log.error(f"live artefact missing: {artifact_dir / name}")
            log.error("Run `make build VARIANT=live` from the repo root first")
            return errno.ENOENT

    # Workspace: ``cijoe/_build/test-pxe/`` (gitignored alongside the
    # wheel-staging dir). Cleared at start so reruns don't accumulate
    # stale qcow2 copies / serial logs from a prior run.
    cijoe_dir = Path.cwd()
    workspace = cijoe_dir / "_build" / "test-pxe"
    if workspace.exists():
        shutil.rmtree(workspace)
    workspace.mkdir(parents=True)

    server_dst = workspace / "server.qcow2"
    log.info(f"Copying {server_src} -> {server_dst}")
    err, _ = cijoe.run_local(f"qemu-img convert -f qcow2 -O qcow2 {server_src} {server_dst}")
    if err:
        log.error("qemu-img convert failed (server.qcow2 copy)")
        return err

    # Render the per-test config files into the workspace, then
    # ``virt-customize --copy-in`` them into the qcow2.
    pxe_nic_slot = int(cfg["pxe_nic_slot"])
    files = workspace / "customize"
    files.mkdir()
    (files / "default-bty-web").write_text(
        "BTY_WEB_TOKEN={token}\n"
        "BTY_STATE_DIR=/var/lib/bty\n"
        "BTY_IMAGE_ROOT=/var/lib/bty/images\n"
        "BTY_BOOT_DIR=/var/lib/bty/boot\n"
        "BTY_WEB_HOST=0.0.0.0\n"
        "BTY_WEB_PORT=8080\n".format(token=cfg["token"])
    )
    (files / "bty-pxe-active.conf").write_text(
        "# Test full-DHCP config (no other DHCP server on the test segment).\n"
        "bind-interfaces\n"
        f"interface=enp0s{pxe_nic_slot}\n"
        f"dhcp-range={cfg['dhcp_range_lo']},{cfg['dhcp_range_hi']},"
        f"{cfg['pxe_netmask']},1h\n"
        "dhcp-match=set:bios,option:client-arch,0\n"
        "dhcp-match=set:efi,option:client-arch,7\n"
        "dhcp-match=set:efi,option:client-arch,9\n"
        "dhcp-userclass=set:ipxe,iPXE\n"
        "dhcp-boot=tag:!ipxe,tag:bios,undionly.kpxe\n"
        "dhcp-boot=tag:!ipxe,tag:efi,ipxe.efi\n"
        "dhcp-boot=tag:ipxe,http://${next-server}:8080/pxe-bootstrap.ipxe\n"
    )
    (files / "10-bty-pxe.network").write_text(
        "[Match]\n"
        f"Name=enp0s{pxe_nic_slot}\n"
        "\n"
        "[Network]\n"
        f"Address={cfg['server_pxe_ip']}/24\n"
        "DHCP=no\n"
    )

    # Stage live trio into the customize dir for one-shot copy-in.
    boot_files = files / "boot"
    boot_files.mkdir(exist_ok=True)
    for name in ARTIFACT_NAMES:
        shutil.copy2(artifact_dir / name, boot_files / name)

    # virt-customize the qcow2.
    cmd = [
        "virt-customize",
        "-a",
        str(server_dst),
        "--copy-in",
        f"{files / 'default-bty-web'}:/etc/default",
        "--copy-in",
        f"{files / 'bty-pxe-active.conf'}:/etc/dnsmasq.d/",
        "--copy-in",
        f"{files / '10-bty-pxe.network'}:/etc/systemd/network/",
    ]
    for name in ARTIFACT_NAMES:
        cmd.extend(["--copy-in", f"{boot_files / name}:/var/lib/bty/boot/"])
    cmd.extend(
        [
            "--run-command",
            "mv /etc/default/default-bty-web /etc/default/bty-web && "
            "chown root:bty /etc/default/bty-web && "
            "chmod 0640 /etc/default/bty-web && "
            "install -d -o bty -g bty -m 0750 /var/lib/bty/images && "
            "chown -R bty:bty /var/lib/bty && "
            "systemctl mask bty-web-init.service && "
            "systemctl enable systemd-networkd",
        ]
    )

    err, _ = cijoe.run_local(" ".join(cmd))
    if err:
        log.error("virt-customize failed")
        return err

    log.info(f"Customised server qcow2 ready at {server_dst}")
    return 0
