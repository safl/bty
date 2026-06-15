"""
Prepare the workspace for the PXE chain test
============================================

Builds the bty-web container image from this checkout (so the chain test runs
the code under review) and stages the artifacts the chain step uploads over the
HTTP API: the netboot live trio and a tiny dummy flash image. Everything lands
under ``cijoe/_build/test-pxe/``. The chain step runs bty-web as a container, so
no server disk image is built.

Retargetable: False
"""

from __future__ import annotations

import errno
import logging as log
import shutil
from argparse import ArgumentParser
from pathlib import Path

ARTIFACT_NAME_FMTS = (
    "bty-netboot-pc-x86_64-v{version}.vmlinuz",
    "bty-netboot-pc-x86_64-v{version}.initrd",
    "bty-netboot-pc-x86_64-v{version}.squashfs",
)


def _read_bty_version() -> str:
    pyproject = Path.cwd().parent / "pyproject.toml"
    for line in pyproject.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if s.startswith("version") and "=" in s:
            return s.split("=", 1)[1].strip().strip('"').strip("'")
    raise RuntimeError(f"could not find version line in {pyproject}")


def _artifact_names() -> tuple[str, ...]:
    return tuple(fmt.format(version=_read_bty_version()) for fmt in ARTIFACT_NAME_FMTS)


def add_args(parser: ArgumentParser):
    del parser


def main(args, cijoe):
    del args
    cfg = cijoe.getconf("test.pxe", {})
    if not cfg:
        log.error("missing [test.pxe] section in cijoe config")
        return errno.EINVAL

    artifact_dir = Path(cfg["artifact_dir"])
    artifact_names = _artifact_names()
    for name in artifact_names:
        if not (artifact_dir / name).is_file():
            log.error(f"live artifact missing: {artifact_dir / name}")
            log.error("Run `make build VARIANT=netboot-pc` from the repo root first")
            return errno.ENOENT

    # Fresh workspace under cijoe/_build/test-pxe/ (gitignored).
    workspace = Path.cwd() / "_build" / "test-pxe"
    if workspace.exists():
        shutil.rmtree(workspace)
    boot_stage = workspace / "boot"
    boot_stage.mkdir(parents=True)

    # Build the bty-web image from this checkout. docker/Dockerfile installs the
    # wheel staged in dist/, so build the wheel first. Run from the repo root.
    image = cfg.get("bty_image", "bty-web:pxetest")
    err, _ = cijoe.run_local("sh -c 'cd .. && uv build --wheel'")
    if err:
        log.error("uv build (wheel for the bty-web image) failed")
        return err
    err, _ = cijoe.run_local(f"sh -c 'cd .. && podman build -f docker/Dockerfile -t {image} .'")
    if err:
        log.error("podman build of the bty-web image failed")
        return err

    # 1 MiB dummy flash image the chain step PUTs to /images/<name>.
    err, _ = cijoe.run_local(f"qemu-img create -f qcow2 {workspace / 'test-image.qcow2'} 1M")
    if err:
        log.error("qemu-img create (dummy flash image) failed")
        return err

    for name in artifact_names:
        shutil.copy2(artifact_dir / name, boot_stage / name)

    log.info(f"Workspace ready at {workspace}")
    log.info(f"  bty-web image: {image}")
    log.info(f"  dummy image:   {workspace / 'test-image.qcow2'}")
    log.info(f"  live trio:     {boot_stage}")
    return 0
