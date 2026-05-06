"""
Stage the workspace for the PXE chain test
============================================

The server appliance ships with a known default credential
(``bty / bty``, baked at image-build time) so the test can boot the
production qcow2 unmodified and ``POST /auth/login`` directly. No
NoCloud overlay, no virt-customize, no DB seeding.

This step just lays out the test workspace under
``cijoe/_build/test-pxe/``:

- ``server.qcow2``       - working copy of the production qcow2
                          (rehydrated from .img.zst when only that's
                          present, the CI artefact shape).
- ``test-image.qcow2``   - 1 MiB dummy qcow2 the chain step uploads
                          to ``PUT /images/<name>``.
- ``boot/{vmlinuz,...}`` - copies of the live trio for the chain
                          step to upload to ``PUT /boot/<name>``.

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

    # Reconstitute the qcow2 from .img.zst when only the .zst is
    # present (CI shape: release.yml uploads only the operator-
    # shippable .img.zst). Locally a fresh ``make build VARIANT=
    # server`` leaves the qcow2 next to the .zst, so this is a no-op.
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

    # Working copy of the server qcow2 - the chain step boots this
    # directly. No mutation: PAM auth uses the baked-in default
    # credential (bty/bty).
    server_dst = workspace / "server.qcow2"
    log.info(f"Copying {server_qcow2_src} -> {server_dst}")
    err, _ = cijoe.run_local(f"qemu-img convert -f qcow2 -O qcow2 {server_qcow2_src} {server_dst}")
    if err:
        log.error("qemu-img convert failed (server.qcow2 copy)")
        return err

    # 1 MiB dummy qcow2 the chain step PUTs to /images/<name>.
    dummy_image = workspace / "test-image.qcow2"
    err, _ = cijoe.run_local(f"qemu-img create -f qcow2 {dummy_image} 1M")
    if err:
        log.error("qemu-img create failed (dummy flash image)")
        return err

    # Stage the live trio so the chain step can PUT each via
    # ``PUT /boot/<name>``. The artefact dir itself is left
    # untouched.
    boot_stage = workspace / "boot"
    boot_stage.mkdir()
    for name in ARTIFACT_NAMES:
        shutil.copy2(artifact_dir / name, boot_stage / name)

    log.info(f"Workspace ready at {workspace}")
    log.info(f"  server qcow2: {server_dst}")
    log.info(f"  dummy image:  {dummy_image}")
    log.info(f"  live trio:    {boot_stage}")
    return 0
