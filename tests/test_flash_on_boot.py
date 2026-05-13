"""Tests for the live env's ``bty-flash-on-boot`` helper.

The script lives at
``bty-media/live-build/config/includes.chroot/usr/local/sbin/bty-flash-on-boot``
- it gets baked into the live squashfs by ``make build VARIANT=netboot-x86``
and runs at every boot in the live env. It's a standalone executable
(``#!/usr/bin/env python3``), not a member of the ``bty`` package, so
we import it via ``importlib`` for testing.

These tests guard against a class of regressions: bty-flash-on-boot
downloads the image to a path that ``bty.images.detect_format()``
recognises (keying off file extension: ``.qcow2``, ``.img``,
``.img.zst``...) so ``bty flash --yes`` doesn't abort at the
validation stage with "image format not recognised".
"""

from __future__ import annotations

import importlib.util
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

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
        # URL shape from the unified /images endpoint:
        # ``/images/<sha>/<filename>`` where the trailing
        # filename carries the format. The decorative-name
        # design exists exactly so this works.
        "http://srv/images/" + "a" * 64 + "/release.img.zst",
        "http://srv/images/" + "f" * 64 + "/disk.qcow2",
    ):
        p = mod.local_image_path(url)
        assert images.detect_format(p) is not None, (url, p)


# ---------- bty.mode=interactive short-circuit ------------------------------


def test_main_short_circuits_on_interactive_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """When ``bty.mode=interactive`` is on /proc/cmdline, the
    flash-on-boot oneshot exits 0 immediately and leaves the work to
    ``bty-tui-on-tty1.service``. No download attempt, no flash, no
    reboot - the script must not race the TUI session that owns
    tty1 in interactive mode."""
    mod = _load_module()

    # Synthesise a /proc/cmdline file with interactive mode.
    fake_cmdline = tmp_path / "cmdline"
    fake_cmdline.write_text(
        "boot=live components quiet bty.mode=interactive "
        "bty.server=http://srv:8080 bty.mac=aa:bb:cc:dd:ee:ff\n"
    )
    monkeypatch.setattr(mod, "CMDLINE", fake_cmdline)

    # Belt-and-braces: if the short-circuit doesn't fire and we end up
    # in the flash path, blow up loudly instead of trying to download
    # / shell out to ``lsblk`` from inside the test.
    def _explode(*_a: object, **_kw: object) -> None:
        raise AssertionError("flash-path side-effect should not run in interactive mode")

    monkeypatch.setattr(mod, "download", _explode)
    monkeypatch.setattr(mod, "pick_target", _explode)
    monkeypatch.setattr(mod, "signal_done", _explode)

    rc = mod.main()
    assert rc == 0
    captured = capsys.readouterr()
    assert "bty.mode=interactive" in captured.out
    assert "bty-tui-on-tty1.service" in captured.out


def test_main_runs_flash_path_when_interactive_mode_absent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The interactive short-circuit only fires for ``bty.mode=interactive``.
    A cmdline with the standard flash-mode keys still drives the
    flash path. (We monkeypatch the side-effect helpers to no-ops so
    the test doesn't actually shell out.)"""
    mod = _load_module()

    fake_cmdline = tmp_path / "cmdline"
    fake_cmdline.write_text(
        "boot=live bty.server=http://srv bty.mac=aa:bb:cc:dd:ee:ff "
        "bty.image_url=http://srv/images/foo.img\n"
    )
    monkeypatch.setattr(mod, "CMDLINE", fake_cmdline)

    visited: list[str] = []
    monkeypatch.setattr(mod, "download", lambda _u, _d: visited.append("download"))
    monkeypatch.setattr(mod, "pick_target", lambda: (visited.append("pick"), "/dev/sda")[1])
    monkeypatch.setattr(mod, "signal_done", lambda _s, _m: visited.append("signal"))

    # subprocess.run is called by main() for ``bty flash`` + sleep + reboot;
    # stub it out so we don't actually spawn anything.
    monkeypatch.setattr(
        mod.subprocess,
        "run",
        lambda *_a, **_kw: visited.append("subprocess") or _DummyCompleted(),
    )

    rc = mod.main()
    assert rc == 0
    assert "download" in visited
    assert "pick" in visited
    assert "signal" in visited


class _DummyCompleted:
    """Stand-in for subprocess.CompletedProcess; the script only checks rc."""

    returncode = 0
    stdout = ""
    stderr = ""
