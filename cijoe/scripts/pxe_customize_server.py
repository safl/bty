"""
Customise the server qcow2 for the PXE chain test
==================================================

Reconstitutes ``bty-server-x86_64.qcow2`` from the operator-shipped
``.img.zst`` (CI artefact form) when the qcow2 isn't already in the
artefact dir, then bakes the test-time config into a working copy
under ``cijoe/_build/test-pxe/`` via ``virt-customize``:

- ``/etc/default/bty-web`` with a known token so the test does not
  have to discover one at runtime (this also short-circuits
  ``bty-web-init.service`` thanks to its ``ConditionPathExists=!``).
- ``/etc/dnsmasq.d/bty-pxe-active.conf`` in **full DHCP** mode (the
  socket-only PXE segment has no other DHCP server, so proxy-DHCP
  has nothing to layer on). dnsmasq binds to the PXE NIC, hands out
  IPs in the configured range, and answers PXE queries with the
  iPXE binaries / chain URL.
- ``/etc/systemd/network/{00-bty-mgmt,10-bty-pxe}.network`` so the
  mgmt NIC DHCPs from QEMU's user-net (host port-forward) and the
  PXE NIC gets ``server_pxe_ip`` static.
- The live trio (``vmlinuz``, ``initrd``, ``squashfs``) under
  ``/var/lib/bty/boot/`` so ``GET /boot/<name>`` resolves during
  the chain.
- A 1 MiB dummy image at ``/var/lib/bty/images/<machine_image>``
  so the live env's ``bty-flash-on-boot`` can complete the loop:
  GET image -> qemu-img convert -> /dev/vda -> reboot.

Output: ``cijoe/_build/test-pxe/server.qcow2`` ready to boot in the
next step.

Retargetable: False
"""

from __future__ import annotations

import errno
import hashlib
import logging as log
import shlex
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
    server_qcow2 = artifact_dir / "bty-server-x86_64.qcow2"
    server_zst = artifact_dir / "bty-server-x86_64.img.zst"

    # Reconstitute the qcow2 from .img.zst when it's missing - this
    # is the CI shape (release.yml uploads only the operator-
    # shippable .img.zst). Locally a fresh ``make build VARIANT=
    # server`` leaves the qcow2 next to the .zst, so the rehydrate
    # is a no-op.
    if not server_qcow2.is_file():
        if not server_zst.is_file():
            log.error(f"neither {server_qcow2.name} nor {server_zst.name} found in {artifact_dir}")
            log.error("Run `make build VARIANT=server` from the repo root first")
            return errno.ENOENT
        server_raw = artifact_dir / "bty-server-x86_64.img"
        log.info(f"Rehydrating {server_zst.name} -> {server_qcow2.name}")
        err, _ = cijoe.run_local(f"zstd -d -k {server_zst} -o {server_raw}")
        if err:
            log.error("zstd decompress failed")
            return err
        err, _ = cijoe.run_local(
            f"qemu-img convert -f raw -O qcow2 {server_raw} {server_qcow2}"
        )
        if err:
            log.error("qemu-img convert raw -> qcow2 failed")
            return err
        server_raw.unlink(missing_ok=True)

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
    log.info(f"Copying {server_qcow2} -> {server_dst}")
    err, _ = cijoe.run_local(f"qemu-img convert -f qcow2 -O qcow2 {server_qcow2} {server_dst}")
    if err:
        log.error("qemu-img convert failed (server.qcow2 copy)")
        return err

    # Render the per-test config files into the workspace, then
    # ``virt-customize --copy-in`` them into the qcow2.
    pxe_nic_slot = int(cfg["pxe_nic_slot"])
    mgmt_nic_slot = int(cfg["mgmt_nic_slot"])
    nic_prefix = cfg.get("nic_prefix", "ens")
    files = workspace / "customize"
    files.mkdir()
    # Auth is OS-PAM against the bty service user; no global token in
    # the env file. The chain test seeds an active session row
    # directly into ``state.db`` (below) so PUT /machines works
    # without going through /auth/login (which would need the bty
    # account to have a real PAM password set in the cooked image).
    (files / "default-bty-web").write_text(
        "BTY_STATE_DIR=/var/lib/bty\n"
        "BTY_IMAGE_ROOT=/var/lib/bty/images\n"
        "BTY_BOOT_DIR=/var/lib/bty/boot\n"
        "BTY_WEB_HOST=0.0.0.0\n"
        "BTY_WEB_PORT=8080\n"
    )
    # ``bind-dynamic`` recovers when interfaces come and go or change
    # addresses (as happens when systemd-networkd assigns the static
    # IP after dnsmasq starts). ``bind-interfaces`` would race; the
    # default wildcard-bind would receive on all interfaces but
    # ``interface=`` filtering doesn't consistently work without it.
    (files / "bty-pxe-active.conf").write_text(
        "# Test full-DHCP config (no other DHCP server on the test segment).\n"
        "bind-dynamic\n"
        f"interface={nic_prefix}{pxe_nic_slot}\n"
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
    # The mgmt NIC needs DHCP from QEMU's user-net so the host port-
    # forward can talk to bty-web. The PXE NIC gets a static IP for
    # dnsmasq to serve from.
    (files / "00-bty-mgmt.network").write_text(
        "[Match]\n"
        f"Name={nic_prefix}{mgmt_nic_slot}\n"
        "\n"
        "[Network]\n"
        "DHCP=yes\n"
    )
    (files / "10-bty-pxe.network").write_text(
        "[Match]\n"
        f"Name={nic_prefix}{pxe_nic_slot}\n"
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

    # Stage a 1 MiB dummy disk image so the live env's
    # bty-flash-on-boot can complete the loop:
    # GET /images/<name> -> qemu-img convert -> /dev/vda ->
    # POST /pxe/<mac>/done -> reboot. Name must match
    # cfg["machine_image"] (PUT'd into /machines/<mac>).
    images_dir = files / "images"
    images_dir.mkdir(exist_ok=True)
    dummy_image = images_dir / cfg["machine_image"]
    err, _ = cijoe.run_local(
        f"qemu-img create -f qcow2 {dummy_image} 1M"
    )
    if err:
        log.error("qemu-img create failed (dummy flash image)")
        return err

    cmd = [
        "virt-customize",
        "-a",
        str(server_dst),
        "--mkdir",
        "/var/lib/bty",
        "--mkdir",
        "/var/lib/bty/boot",
        "--mkdir",
        "/var/lib/bty/images",
        "--copy-in",
        f"{files / 'default-bty-web'}:/etc/default",
        "--copy-in",
        f"{files / 'bty-pxe-active.conf'}:/etc/dnsmasq.d/",
        "--copy-in",
        f"{files / '00-bty-mgmt.network'}:/etc/systemd/network/",
        "--copy-in",
        f"{files / '10-bty-pxe.network'}:/etc/systemd/network/",
    ]
    for name in ARTIFACT_NAMES:
        cmd.extend(["--copy-in", f"{boot_files / name}:/var/lib/bty/boot/"])
    cmd.extend(["--copy-in", f"{dummy_image}:/var/lib/bty/images/"])

    # Seed an active session row into the qcow2's state.db so the
    # chain test can hit PUT /machines without going through
    # /auth/login (which would call PAM and need a real password set
    # for the bty user). Plaintext token comes from cfg; we hash it
    # the same way bty.web._db does (sha256 hex). Far-future expiry
    # so the row is unconditionally valid for the test run.
    token_hash = hashlib.sha256(cfg["token"].encode("utf-8")).hexdigest()
    # Stage a tiny Python script as a file so virt-customize doesn't
    # have to wrestle with shell-escaping the multi-line program.
    (files / "seed-session.py").write_text(
        "import sqlite3, sys\n"
        "conn = sqlite3.connect('/var/lib/bty/state.db')\n"
        "conn.executescript('''\n"
        "CREATE TABLE IF NOT EXISTS sessions (\n"
        "    token_hash   TEXT PRIMARY KEY,\n"
        "    created_at   TEXT NOT NULL,\n"
        "    expires_at   TEXT NOT NULL,\n"
        "    last_used_at TEXT,\n"
        "    label        TEXT\n"
        ");\n"
        "''')\n"
        "conn.execute(\n"
        "    'INSERT OR REPLACE INTO sessions(token_hash, created_at, '\n"
        "    'expires_at, last_used_at, label) VALUES (?, ?, ?, NULL, ?)',\n"
        f"    ('{token_hash}', '2026-01-01T00:00:00+00:00',\n"
        "     '2099-01-01T00:00:00+00:00', 'pxe-chain-test'),\n"
        ")\n"
        "conn.commit()\n"
        "conn.close()\n"
    )

    cmd.extend(
        [
            "--copy-in",
            f"{files / 'seed-session.py'}:/tmp",
        ]
    )

    cmd.extend(
        [
            "--run-command",
            # bty-web-init.service has ``ConditionPathExists=!/etc/
            # default/bty-web``; the pre-baked default-bty-web above
            # short-circuits it on first boot so we don't need to mask.
            "mv /etc/default/default-bty-web /etc/default/bty-web && "
            "chown root:bty /etc/default/bty-web && "
            "chmod 0640 /etc/default/bty-web && "
            "install -d -o bty -g bty -m 0750 /var/lib/bty && "
            "install -d -o bty -g bty -m 0750 /var/lib/bty/images && "
            "install -d -o bty -g bty -m 0750 /var/lib/bty/boot && "
            "python3 /tmp/seed-session.py && "
            "rm /tmp/seed-session.py && "
            "chown bty:bty /var/lib/bty/state.db && "
            "chmod 0640 /var/lib/bty/state.db && "
            "chown -R bty:bty /var/lib/bty && "
            "systemctl enable systemd-networkd",
        ]
    )

    # ``cijoe.run_local`` takes a single shell string; ``shlex.join``
    # quotes args (the ``--run-command`` payload has spaces that bare
    # space-join would split into separate tokens).
    err, _ = cijoe.run_local(shlex.join(cmd))
    if err:
        log.error("virt-customize failed")
        return err

    log.info(f"Customised server qcow2 ready at {server_dst}")
    return 0
