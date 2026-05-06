"""
Generate a NoCloud cidata ISO for the PXE chain test
=====================================================

The test boots the unmodified production server qcow2 with a small
cidata ISO attached as a CD-ROM. cloud-init at first boot reads the
ISO and applies a known bty password (so the next chain step can
``POST /auth/login`` deterministically). Everything else - state
dirs, /etc/default/bty-web, /etc/issue, dnsmasq active config, the
live trio under /var/lib/bty/boot/, the dummy image under
/var/lib/bty/images/ - happens via the same mechanisms an operator
would use: ``bty-web-init.service`` for first-boot dirs, the
``PUT /boot/<name>`` and ``PUT /images/<name>`` upload routes for
artefact staging, and ``POST /ui/settings/pxe-activate`` for
dnsmasq. No virt-customize, no qcow2 mutation, no test-only state
seeding.

Outputs into ``cijoe/_build/test-pxe/``:

- ``server.qcow2``  - copy of the production qcow2 (rehydrated from
                      .img.zst when only that's present)
- ``seed.iso``      - NoCloud cidata ISO carrying user-data
- ``test-image.qcow2`` - 1 MiB dummy qcow2 for the run step to
                         upload via ``PUT /images/<name>``

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
    server_qcow2_src = artifact_dir / "bty-server-x86_64.qcow2"
    server_zst = artifact_dir / "bty-server-x86_64.img.zst"

    # Reconstitute the qcow2 from .img.zst when it's missing - same
    # CI shape as before (release.yml uploads only the operator-
    # shippable .img.zst). Locally a fresh ``make build VARIANT=
    # server`` leaves the qcow2 next to the .zst, so the rehydrate
    # is a no-op.
    if not server_qcow2_src.is_file():
        if not server_zst.is_file():
            log.error(
                f"neither {server_qcow2_src.name} nor {server_zst.name} found in {artifact_dir}"
            )
            log.error("Run `make build VARIANT=server` from the repo root first")
            return errno.ENOENT
        server_raw = artifact_dir / "bty-server-x86_64.img"
        log.info(f"Rehydrating {server_zst.name} -> {server_qcow2_src.name}")
        err, _ = cijoe.run_local(f"zstd -d -k {server_zst} -o {server_raw}")
        if err:
            log.error("zstd decompress failed")
            return err
        err, _ = cijoe.run_local(
            f"qemu-img convert -f raw -O qcow2 {server_raw} {server_qcow2_src}"
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

    # Working copy of the server qcow2 (read-only base; the chain
    # step boots this directly + the cidata ISO).
    server_dst = workspace / "server.qcow2"
    log.info(f"Copying {server_qcow2_src} -> {server_dst}")
    err, _ = cijoe.run_local(f"qemu-img convert -f qcow2 -O qcow2 {server_qcow2_src} {server_dst}")
    if err:
        log.error("qemu-img convert failed (server.qcow2 copy)")
        return err

    # Stage the cidata ISO. NoCloud expects two files at the FS root:
    # ``meta-data`` (instance-id is the only required field) and
    # ``user-data`` (#cloud-config style YAML). cloud-init reads them
    # at first boot and applies what we put in. ``mkisofs -V cidata``
    # is what cloud-init's NoCloud datasource looks for - the volume
    # label is how it discovers the ISO.
    seed_dir = workspace / "cidata"
    seed_dir.mkdir()
    (seed_dir / "meta-data").write_text("instance-id: bty-pxe-test\nlocal-hostname: bty\n")

    # ``chpasswd`` syntax: list of lines ``user:password``. We only
    # set the bty user's password. ``ssh_pwauth: True`` is harmless
    # (cooked image already disables SSH password auth in sshd_config)
    # but documents intent. ``users: []`` keeps cloud-init from
    # creating extra accounts.
    user_data = (
        "#cloud-config\n"
        "users: []\n"
        "chpasswd:\n"
        "  expire: false\n"
        "  list: |\n"
        f"    bty:{cfg['bty_password']}\n"
        "ssh_pwauth: false\n"
    )
    (seed_dir / "user-data").write_text(user_data)

    seed_iso = workspace / "seed.iso"
    err, _ = cijoe.run_local(
        f"genisoimage -output {seed_iso} -volid cidata -joliet -rock {seed_dir}"
    )
    if err:
        log.error("genisoimage failed (cidata ISO)")
        return err

    # 1 MiB dummy qcow2 the chain step will PUT to /images/<name>.
    # Production-realistic: operators upload images the same way.
    dummy_image = workspace / "test-image.qcow2"
    err, _ = cijoe.run_local(f"qemu-img create -f qcow2 {dummy_image} 1M")
    if err:
        log.error("qemu-img create failed (dummy flash image)")
        return err

    # Stage the live trio next to the workspace so the run step can
    # PUT each via the upload route - the artefact dir itself is
    # left untouched.
    boot_stage = workspace / "boot"
    boot_stage.mkdir()
    for name in ARTIFACT_NAMES:
        shutil.copy2(artifact_dir / name, boot_stage / name)

    log.info(f"Workspace ready at {workspace}")
    log.info(f"  server qcow2: {server_dst}")
    log.info(f"  cidata ISO:   {seed_iso}")
    log.info(f"  dummy image:  {dummy_image}")
    log.info(f"  live trio:    {boot_stage}")
    return 0
