"""Tests for the live env's ``bty-flash-on-boot`` helper.

The script lives at
``bty-media/live-build/config/includes.chroot/usr/local/sbin/bty-flash-on-boot``
- it gets baked into the live squashfs by ``make build VARIANT=live-x86``
and runs at every boot in the live env. It's a standalone executable
(``#!/usr/bin/env python3``), not a member of the ``bty`` package, so
we import it via ``importlib`` for testing.

The regression these tests guard against: bty-flash-on-boot used to
download the image to a fixed ``/var/tmp/bty-flash-on-boot.image``
path; ``bty.images.detect_format()`` keys off the file extension
(``.qcow2``, ``.img``, ``.img.zst``) and ``.image`` matched none, so
``bty flash --yes`` aborted at the validation stage with "image
format not recognised" - on every network-flash, in production.
"""

from __future__ import annotations

import importlib.util
from importlib.machinery import SourceFileLoader
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = (
    REPO_ROOT
    / "bty-media"
    / "live-build"
    / "config"
    / "includes.chroot"
    / "usr"
    / "local"
    / "sbin"
    / "bty-flash-on-boot"
)


def _load_module():
    # The script has no ``.py`` suffix (it's a system-installed
    # executable), so the default file finder skips it. Use an
    # explicit ``SourceFileLoader`` to load it as Python anyway.
    loader = SourceFileLoader("bty_flash_on_boot", str(SCRIPT_PATH))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def test_local_image_path_preserves_qcow2_suffix():
    mod = _load_module()
    p = mod.local_image_path("http://server/images/disk.qcow2")
    assert p.suffix == ".qcow2", p
    assert p.name == "disk.qcow2"


def test_local_image_path_preserves_img_zst_suffix():
    mod = _load_module()
    p = mod.local_image_path("http://server/images/v1/release.img.zst")
    assert p.name.endswith(".img.zst"), p


def test_local_image_path_preserves_img_suffix():
    mod = _load_module()
    p = mod.local_image_path("http://srv:8080/images/raw.img?token=abc")
    assert p.name == "raw.img", p


def test_local_image_path_falls_back_when_url_has_no_filename():
    mod = _load_module()
    p = mod.local_image_path("http://server/")
    assert p.suffix in {".img", ".qcow2"}, p


def test_local_image_path_extension_recognised_by_detect_format():
    """End-to-end: the path bty-flash-on-boot picks must satisfy
    ``bty.images.detect_format()``, otherwise ``bty flash --yes``
    refuses to run.
    """
    from bty import images

    mod = _load_module()
    for url in (
        "http://srv/images/disk.qcow2",
        "http://srv/images/raw.img",
        "http://srv/images/release.img.zst",
    ):
        p = mod.local_image_path(url)
        assert images.detect_format(p) is not None, (url, p)
