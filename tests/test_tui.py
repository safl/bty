"""Tests for the ``bty.tui`` module (the Rich-based wizard
distributed as the ``bty`` console script).

Two layers:

1. Free helpers (``load_catalog_from_source``, ``post_pxe_done``,
   ``post_inventory``, ``_emit_console_marker``,
   ``_format_progress_bytes``) -- HTTP wrappers + pure utilities
   covered without instantiating ``BtyTui`` at all.
2. ``BtyTui`` smoke / contract tests: argparse routing in
   ``bty.tui.main``, ``_State`` stage machine, ``_refresh_images``
   blending of local + catalog sources, ``_fetch_and_dispatch_plan``
   contract, and the auto-flash code path's progress-callback
   plumbing.

Data sources (``images.list_images``, ``disks.list_disks``,
``load_catalog_from_source``) are monkeypatched per-test to
return synthetic rows; the goal is to verify the wiring around
those calls, not to re-test the underlying functions (those have
their own coverage).
"""

from __future__ import annotations

import urllib.error
from pathlib import Path
from typing import Any

import pytest

from bty.tui import _app as tui_app

_VALID_CATALOG_TOML = b"""\
version = 1

[[images]]
name = "demo.qcow2"
src = "http://server:8080/images/abc123def456"
sha256 = "abc123abc123abc123abc123abc123abc123abc123abc123abc123abc123def4"
format = "qcow2"
size_bytes = 1024

[[images]]
name = "live.img.zst"
src = "https://github.com/safl/bty-images/releases/download/v1/live.img.zst"
sha256 = "fedcba98fedcba98fedcba98fedcba98fedcba98fedcba98fedcba98fedcba98"
format = "img.zst"
size_bytes = 4096
"""


def _patch_open_for_console(fake_path: Path):
    """``builtins.open`` replacement that diverts ``/dev/console``
    writes to ``fake_path`` and passes everything else through to the
    real ``open``. Lets the milestone-emitter tests assert on the
    bytes the wizard would have sent to the serial console without
    actually needing a writable /dev/console (which is root-only in
    real life, missing in containers/CI).

    /dev/console is a chardev: opening it with mode "w" does NOT
    truncate prior writes (consecutive milestone calls all land in
    order on the UART). Mirror that semantics on the fake by
    promoting "w" to "a" -- without it the second milestone write
    would wipe the first on a regular-file backing.
    """
    real_open = open

    def fake_open(path, mode="r", *args, **kwargs):
        if path == "/dev/console":
            if mode == "w":
                mode = "a"
            return real_open(fake_path, mode, *args, **kwargs)
        return real_open(path, mode, *args, **kwargs)

    return fake_open


def _fake_bytes_resp(raw: bytes):
    """urllib.request.urlopen replacement that returns ``raw`` as the
    response body. Compatible with the ``with urlopen(...) as resp``
    + ``resp.read(n)`` pattern."""

    class _Resp:
        def __enter__(self) -> _Resp:
            return self

        def __exit__(self, *_a: object) -> None:
            pass

        def read(self, _n: int = -1) -> bytes:
            return raw

    return _Resp()


def test_load_catalog_from_source_parses_http_toml(monkeypatch: pytest.MonkeyPatch) -> None:
    """``load_catalog_from_source(http://...)`` issues a GET, parses the
    response body as TOML (via ``bty.catalog.load_bytes``), and emits
    one ``_TuiImage`` per ``[[images]]`` entry. ``src`` becomes the
    flashable URL the TUI later hands to the URL pipeline."""
    monkeypatch.setattr(
        tui_app.urllib.request,
        "urlopen",
        lambda *_a, **_kw: _fake_bytes_resp(_VALID_CATALOG_TOML),
    )

    rows = tui_app.load_catalog_from_source("http://server:8080/catalog.toml")

    assert len(rows) == 2
    assert rows[0].name == "demo.qcow2"
    assert rows[0].fmt == "qcow2"
    assert rows[0].size_bytes == 1024
    assert rows[0].url == "http://server:8080/images/abc123def456"
    assert rows[0].path is None  # remote rows never get a local path
    # The catalog's declared sha rides into the row so the flash verifies it.
    assert rows[0].sha == "abc123abc123abc123abc123abc123abc123abc123abc123abc123abc123def4"
    assert rows[1].url == "https://github.com/safl/bty-images/releases/download/v1/live.img.zst"
    assert rows[1].sha == "fedcba98fedcba98fedcba98fedcba98fedcba98fedcba98fedcba98fedcba98"


def test_load_catalog_from_source_parses_local_path(tmp_path: Path) -> None:
    """Bare path (no scheme) goes through the local read path,
    parses identically to the HTTP path."""
    catalog_file = tmp_path / "catalog.toml"
    catalog_file.write_bytes(_VALID_CATALOG_TOML)

    rows = tui_app.load_catalog_from_source(str(catalog_file))
    assert [r.name for r in rows] == ["demo.qcow2", "live.img.zst"]


def test_load_catalog_from_source_rejects_invalid_toml(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Garbage bytes -> ``CatalogError`` (catalog.load_bytes wraps
    tomllib's TOMLDecodeError). Same behaviour for local paths and
    URL fetches."""
    monkeypatch.setattr(
        tui_app.urllib.request,
        "urlopen",
        lambda *_a, **_kw: _fake_bytes_resp(b"not valid toml at all <<<"),
    )
    with pytest.raises(tui_app._catalog.CatalogError):
        tui_app.load_catalog_from_source("http://server/catalog.toml")


def test_load_catalog_from_source_propagates_url_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom(*_a: object, **_kw: object) -> None:
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(tui_app.urllib.request, "urlopen", _boom)

    with pytest.raises(urllib.error.URLError):
        tui_app.load_catalog_from_source("http://server/catalog.toml")


def test_post_pxe_done_sends_post(monkeypatch: pytest.MonkeyPatch) -> None:
    """``post_pxe_done`` issues a POST against ``<server>/pxe/<mac>/done``."""
    seen: dict[str, str] = {}

    class _FakeOpen:
        def __enter__(self) -> _FakeOpen:
            return self

        def __exit__(self, *_a: object) -> None:
            pass

    def _capture(req: Any, **_kw: object) -> _FakeOpen:
        seen["url"] = req.full_url
        seen["method"] = req.get_method()
        return _FakeOpen()

    monkeypatch.setattr(tui_app.urllib.request, "urlopen", _capture)

    tui_app.post_pxe_done("http://server:8080", "aa:bb:cc:dd:ee:ff")

    assert seen["url"] == "http://server:8080/pxe/aa:bb:cc:dd:ee:ff/done"
    assert seen["method"] == "POST"


def test_post_pxe_done_strips_trailing_slash_from_server_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, str] = {}

    class _FakeOpen:
        def __enter__(self) -> _FakeOpen:
            return self

        def __exit__(self, *_a: object) -> None:
            pass

    def _capture(req: Any, **_kw: object) -> _FakeOpen:
        seen["url"] = req.full_url
        return _FakeOpen()

    monkeypatch.setattr(tui_app.urllib.request, "urlopen", _capture)
    tui_app.post_pxe_done("http://server:8080/", "aa:bb:cc:dd:ee:ff")
    assert seen["url"] == "http://server:8080/pxe/aa:bb:cc:dd:ee:ff/done"


def test_post_pxe_done_propagates_url_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*_a: object, **_kw: object) -> None:
        raise urllib.error.URLError("server gone")

    monkeypatch.setattr(tui_app.urllib.request, "urlopen", _boom)

    with pytest.raises(urllib.error.URLError):
        tui_app.post_pxe_done("http://server", "aa:bb:cc:dd:ee:ff")


def test_post_inventory_sends_json_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    """``post_inventory`` serialises the disk list to JSON, sets
    Content-Type and POSTs to ``<server>/pxe/<mac>/inventory``."""
    seen: dict[str, object] = {}

    class _FakeOpen:
        def __enter__(self) -> _FakeOpen:
            return self

        def __exit__(self, *_a: object) -> None:
            pass

    def _capture(req: Any, **_kw: object) -> _FakeOpen:
        seen["url"] = req.full_url
        seen["method"] = req.get_method()
        seen["body"] = req.data
        seen["content_type"] = req.get_header("Content-type")
        return _FakeOpen()

    monkeypatch.setattr(tui_app.urllib.request, "urlopen", _capture)
    tui_app.post_inventory(
        "http://server:8080",
        "aa:bb:cc:dd:ee:ff",
        [{"path": "/dev/sda", "serial": "S1234"}],
    )
    assert seen["url"] == "http://server:8080/pxe/aa:bb:cc:dd:ee:ff/inventory"
    assert seen["method"] == "POST"
    assert seen["content_type"] == "application/json"
    import json as _json

    decoded = _json.loads(seen["body"])  # type: ignore[arg-type]
    assert decoded == {"disks": [{"path": "/dev/sda", "serial": "S1234"}]}


def test_post_inventory_propagates_url_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Transport errors bubble up to the caller so the wrapper in
    BtyTui can surface them on the status bar."""

    def _boom(*_a: object, **_kw: object) -> None:
        raise urllib.error.URLError("no route")

    monkeypatch.setattr(tui_app.urllib.request, "urlopen", _boom)
    with pytest.raises(urllib.error.URLError):
        tui_app.post_inventory("http://server", "aa:bb:cc:dd:ee:ff", [])


def test_main_accepts_server_and_mac_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    """``bty --server URL --mac MAC`` reaches ``BtyTui(...)`` with
    the right kwargs. The actual ``run()`` is monkeypatched so we
    don't try to launch a real wizard from a unit test.

    The cmdline surface is intentionally narrow -- just --server +
    --mac -- because every other knob (image, target disk, catalog)
    comes from the bty-server's /pxe/<mac>/plan response, not the
    cmdline.
    """
    captured: dict[str, object] = {}

    class _FakeBtyTui:
        def __init__(
            self,
            server: object = None,
            mac: object = None,
            **kw: object,
        ) -> None:
            captured["server"] = server
            captured["mac"] = mac
            captured["kw"] = kw

        def run(self) -> None:
            captured["ran"] = True

    monkeypatch.setattr(tui_app, "BtyTui", _FakeBtyTui)

    # Re-import the entry-point to make sure it picks up the patched class.
    import bty.tui as tui_mod

    tui_mod.main(["--server", "http://srv:8080", "--mac", "aa:bb:cc:dd:ee:ff"])

    assert captured["server"] == "http://srv:8080"
    assert captured["mac"] == "aa:bb:cc:dd:ee:ff"
    assert captured["ran"] is True


def test_main_accepts_catalog_flag_for_interactive_prefill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``bty --catalog URL`` (no --mac) reaches
    ``BtyTui(catalog=URL)``. The operator-level intent is "skip
    SELECT_CATALOG; jump to SELECT_IMAGE with this catalog already
    chosen" -- equivalent to picking ``[c] custom`` on the source
    screen and typing the URL.
    """
    captured: dict[str, object] = {}

    class _FakeBtyTui:
        def __init__(
            self,
            server: object = None,
            mac: object = None,
            catalog: object = None,
            **kw: object,
        ) -> None:
            captured["server"] = server
            captured["mac"] = mac
            captured["catalog"] = catalog

        def run(self) -> None:
            captured["ran"] = True

    monkeypatch.setattr(tui_app, "BtyTui", _FakeBtyTui)
    import bty.tui as tui_mod

    tui_mod.main(["--catalog", "http://srv:8080/catalog.toml"])

    assert captured["server"] == "bty-server"
    assert captured["mac"] is None
    assert captured["catalog"] == "http://srv:8080/catalog.toml"
    assert captured["ran"] is True


def test_bty_tui_init_catalog_skips_select_catalog_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``BtyTui(catalog=URL)`` pre-loads the catalog source and
    flags ``catalog_chosen=True``, so the wizard's derived stage is
    SELECT_IMAGE (not SELECT_CATALOG) on the first iteration.
    """
    monkeypatch.setenv("BTY_IMAGE_ROOT", str(tmp_path))
    app = tui_app.BtyTui(catalog="http://srv:8080/catalog.toml")
    assert app._state.catalog_source == "http://srv:8080/catalog.toml"
    assert app._state.catalog_chosen is True
    # No image / disk selected yet; the next stage is SELECT_IMAGE.
    assert app._state.stage() is tui_app._WizardStage.SELECT_IMAGE


def test_main_defaults_to_bty_server_hostname(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bare ``bty`` invocation (no flags) reaches
    ``BtyTui(server="bty-server", mac=None)``. The default exists
    because the netboot live env's tty1 wrapper supplies --server
    explicitly; but on a developer workstation, a LAN DNS entry for
    ``bty-server`` (or an /etc/hosts line) lets ``bty --mac X``
    just work.
    """
    captured: dict[str, object] = {}

    class _FakeBtyTui:
        def __init__(
            self,
            server: object = None,
            mac: object = None,
            **kw: object,
        ) -> None:
            captured["server"] = server
            captured["mac"] = mac

        def run(self) -> None:
            captured["ran"] = True

    monkeypatch.setattr(tui_app, "BtyTui", _FakeBtyTui)
    import bty.tui as tui_mod

    tui_mod.main([])

    assert captured["server"] == "bty-server"
    assert captured["mac"] is None
    assert captured["ran"] is True


# ---------- type-level cross-cutting ---------------------------------------


def test_oras_error_is_an_os_error() -> None:
    """:class:`OrasError` must subclass :class:`OSError` so callers
    that handle remote-I/O generically (``except OSError``) catch
    it. ``urllib.error.URLError`` already follows this pattern;
    keeping OrasError on the same family closes the "TUI freezes
    after oras failure" class of bugs at the type level."""
    from bty import oras as _oras_module

    assert issubclass(_oras_module.OrasError, OSError)
    # And it remains catchable as itself for the call sites that
    # do want ORAS-specific handling.
    err = _oras_module.OrasError("boom")
    assert isinstance(err, _oras_module.OrasError)
    assert isinstance(err, OSError)


# --------------------------------------------------------------------------
# Wizard state machine: _State.stage() + _State.back()
# --------------------------------------------------------------------------


def test_state_stage_advances_with_each_commit() -> None:
    """``_State.stage()`` is derived from selection state. Empty state
    with ``catalog_chosen=False`` -> Stage 1 (SELECT_CATALOG); flipping
    ``catalog_chosen=True`` -> Stage 2 (SELECT_IMAGE); setting
    ``selected_image`` -> Stage 3; both selections -> Stage 4;
    ``post_flash`` -> Stage 5.
    """
    s = tui_app._State(image_root=Path("/tmp"))
    assert s.stage() is tui_app._WizardStage.SELECT_CATALOG

    s.catalog_chosen = True
    assert s.stage() is tui_app._WizardStage.SELECT_IMAGE

    s.selected_image = tui_app._TuiImage(name="x", fmt="img.gz", size_bytes=0, url="http://x")
    assert s.stage() is tui_app._WizardStage.SELECT_DISK

    s.selected_disk = {"path": "/dev/sda"}
    assert s.stage() is tui_app._WizardStage.CONFIRM_FLASH

    s.post_flash = True
    assert s.stage() is tui_app._WizardStage.REBOOT_OR_DONE


def test_state_back_clears_one_commit_at_a_time() -> None:
    """``_State.back()`` drops the most-recent commit, one stage at a
    time. Chain across all five stages:

      REBOOT_OR_DONE -> SELECT_DISK   (clear post_flash + disk)
      CONFIRM_FLASH  -> SELECT_DISK   (clear disk)
      SELECT_DISK    -> SELECT_IMAGE  (clear image)
      SELECT_IMAGE   -> SELECT_CATALOG (clear catalog_chosen)
      SELECT_CATALOG -> no-op (top of wizard)
    """
    s = tui_app._State(image_root=Path("/tmp"))
    s.catalog_chosen = True
    s.selected_image = tui_app._TuiImage(name="x", fmt="img.gz", size_bytes=0, url="http://x")
    s.selected_disk = {"path": "/dev/sda"}
    s.post_flash = True

    s.back()  # REBOOT_OR_DONE -> SELECT_DISK (keep image, clear disk + post_flash)
    assert s.stage() is tui_app._WizardStage.SELECT_DISK
    assert s.selected_image is not None
    assert s.selected_disk is None
    assert s.post_flash is False

    s.selected_disk = {"path": "/dev/sda"}
    s.back()  # CONFIRM_FLASH -> SELECT_DISK (clear disk)
    assert s.stage() is tui_app._WizardStage.SELECT_DISK
    assert s.selected_disk is None

    s.back()  # SELECT_DISK -> SELECT_IMAGE (clear image)
    assert s.stage() is tui_app._WizardStage.SELECT_IMAGE
    assert s.selected_image is None

    s.back()  # SELECT_IMAGE -> SELECT_CATALOG (clear catalog_chosen)
    assert s.stage() is tui_app._WizardStage.SELECT_CATALOG
    assert s.catalog_chosen is False

    s.back()  # SELECT_CATALOG -> no-op
    assert s.stage() is tui_app._WizardStage.SELECT_CATALOG


# --------------------------------------------------------------------------
# Index parsing: numeric prompt -> list index
# --------------------------------------------------------------------------


def test_parse_index_handles_valid_and_invalid_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``_parse_index`` returns 0-based index for a valid 1-based
    numeric choice within ``[1, n]``; returns ``None`` otherwise
    (empty, non-numeric, out of range).
    """
    monkeypatch.setenv("BTY_IMAGE_ROOT", str(tmp_path))
    app = tui_app.BtyTui()
    assert app._parse_index("1", 3) == 0
    assert app._parse_index("3", 3) == 2
    assert app._parse_index("4", 3) is None
    assert app._parse_index("0", 3) is None
    assert app._parse_index("", 3) is None
    assert app._parse_index("q", 3) is None
    assert app._parse_index("1.5", 3) is None
    assert app._parse_index("-1", 3) is None


# --------------------------------------------------------------------------
# Refresh paths: local + remote catalog blending
# --------------------------------------------------------------------------


def test_refresh_images_blends_local_and_remote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``_refresh_images`` combines the local image-root scan and
    the optional ``--catalog`` overlay. Both feeds end up on
    ``_state._images``.
    """
    monkeypatch.setattr(
        tui_app,
        "_list_local_images",
        lambda _root: [
            tui_app._TuiImage(
                name="local.img.gz",
                fmt="img.gz",
                size_bytes=1024,
                path=tmp_path / "local.img.gz",
            )
        ],
    )
    monkeypatch.setattr(
        tui_app,
        "load_catalog_from_source",
        lambda _src, **_kw: [
            tui_app._TuiImage(
                name="remote (rolling)",
                fmt="img.gz",
                size_bytes=2048,
                url="https://example.invalid/remote.img.gz",
            )
        ],
    )
    monkeypatch.setenv("BTY_IMAGE_ROOT", str(tmp_path))
    app = tui_app.BtyTui()
    app._state.catalog_source = "https://example.invalid/catalog.toml"
    app._refresh_images()
    names = [i.name for i in app._state._images]
    assert "local.img.gz" in names
    assert "remote (rolling)" in names
    assert app._catalog_load_error is None


def test_refresh_images_surfaces_catalog_load_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed catalog fetch (URL unreachable, bad TOML, oras
    miss) must NOT abort the TUI -- it sets
    ``_catalog_load_error`` and the next screen render shows a
    soft banner.
    """
    monkeypatch.setattr(tui_app, "_list_local_images", lambda _root: [])

    def _boom(_src, **_kw):
        raise urllib.error.URLError("simulated network failure")

    monkeypatch.setattr(tui_app, "load_catalog_from_source", _boom)
    monkeypatch.setenv("BTY_IMAGE_ROOT", str(tmp_path))
    app = tui_app.BtyTui()
    app._state.catalog_source = "https://example.invalid/catalog.toml"
    app._refresh_images()
    assert app._state._images == []
    assert app._catalog_load_error is not None
    assert "URLError" in app._catalog_load_error


# --------------------------------------------------------------------------
# Rendering smoke tests: tables don't crash on various input shapes
# --------------------------------------------------------------------------


def test_print_image_table_handles_empty_and_partial_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``_print_image_table`` must render cleanly on rows with
    missing fmt / size_bytes. The framebuffer console renders
    ``?`` placeholders; the test just confirms no exceptions.
    """
    monkeypatch.setenv("BTY_IMAGE_ROOT", str(tmp_path))
    app = tui_app.BtyTui()
    app._print_image_table(
        [
            tui_app._TuiImage(name="complete.img.gz", fmt="img.gz", size_bytes=1024, path=tmp_path),
            tui_app._TuiImage(name="missing-fmt", fmt=None, size_bytes=0, url="http://x"),
        ]
    )  # no raise


def test_print_disk_table_handles_partial_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``_print_disk_table`` must render even when lsblk omits
    optional fields (vendor / model / serial). Crashing here used
    to be the v0.19.x "TUI freezes after lsblk reports a sparse
    row" class of bug.
    """
    monkeypatch.setenv("BTY_IMAGE_ROOT", str(tmp_path))
    app = tui_app.BtyTui()
    app._print_disk_table(
        [
            {"path": "/dev/sda", "size": "500G"},  # minimal: just path + size
            {"name": "nvme0n1", "size": "1T", "model": "Samsung 980", "serial": "S1"},
        ]
    )  # no raise


# --------------------------------------------------------------------------
# Format-progress helper
# --------------------------------------------------------------------------


def test_format_progress_bytes_handles_unknowns() -> None:
    """``_format_progress_bytes`` renders ``?`` for None inputs so
    a flash whose total isn't yet known doesn't crash the live
    progress callback.
    """
    assert tui_app._format_progress_bytes(0, 1 << 20) == "0.0 MiB / 1.0 MiB"
    assert tui_app._format_progress_bytes(None, 1 << 20) == "? / 1.0 MiB"
    assert tui_app._format_progress_bytes(1 << 20, None) == "1.0 MiB / ?"
    assert tui_app._format_progress_bytes(None, None) == "? / ?"


def test_emit_console_marker_writes_to_stderr_and_swallows_console_failure(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``_emit_console_marker`` is the chain-test marker emitter. It
    MUST write to ``sys.stderr`` so a workstation-side ``bty`` run
    (no writable /dev/console) still surfaces the line, AND it MUST
    swallow OSError from the /dev/console write so the auto-flash
    path doesn't crash if the kernel console is missing / non-
    writable. The chain test (cijoe/configs/test-pxe.toml) pins the
    exact strings the live env emits; this guards the contract from
    the Python side.
    """
    # Sanity: chain-test markers used by the live env's _run_auto
    # path. Plain text, no Rich markup -- the chain test grep's the
    # QEMU serial log for these substrings.
    tui_app._emit_console_marker("bty: auto-flash starting")
    tui_app._emit_console_marker("bty: flash complete; rebooting")
    captured = capsys.readouterr()
    # Both markers on stderr, one per line, in order.
    assert "bty: auto-flash starting" in captured.err
    assert "bty: flash complete; rebooting" in captured.err
    # /dev/console write was best-effort; on this host it either
    # succeeded (real /dev/console) or was swallowed (read-only,
    # missing, EPERM under non-root) -- either way no exception
    # propagated to here.


def test_milestone_emitter_fires_each_threshold_once_in_order(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``_MilestoneEmitter`` is the SoL-friendly progress heartbeat. It
    must emit ``bty: <stage> NN%`` lines on 25/50/75/100 crossings, at
    most once per crossing, in increasing order, and write directly to
    /dev/console (which resolves to the LAST ``console=`` cmdline
    target, ttyS0 on every bty cmdline -- so the bytes land on the
    serial UART, not on the framebuffer tty0 Rich is painting on).
    Routing through /dev/kmsg (v0.55.11) caused stacked bar pairs
    because the printk fanout reached tty0 too; the direct
    /dev/console write skips printk entirely.
    """
    fake = tmp_path / "console"
    monkeypatch.setattr("builtins.open", _patch_open_for_console(fake))
    em = tui_app._MilestoneEmitter("write")
    # Cross a couple of thresholds at once (a real flash can emit
    # tens of MiB between progress events when the write is fast).
    em.update(30, 100)  # 30% -> fires 25
    em.update(60, 100)  # 60% -> fires 50
    em.update(80, 100)  # 80% -> fires 75
    em.update(100, 100)  # 100% -> fires 100
    assert fake.read_text() == ("bty: write 25%\nbty: write 50%\nbty: write 75%\nbty: write 100%\n")


def test_milestone_emitter_skips_when_total_unknown(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Some write paths can't pre-compute the decompressed total
    (gzip-without-trailer-trust, qcow2 with sparse virtual size).
    The emitter must silently no-op rather than divide-by-zero or
    emit bogus percentages."""
    fake = tmp_path / "console"
    monkeypatch.setattr("builtins.open", _patch_open_for_console(fake))
    em = tui_app._MilestoneEmitter("download")
    em.update(1024, None)
    em.update(1024, 0)
    em.update(1024, -1)
    assert not fake.exists() or fake.read_text() == ""


def test_milestone_emitter_jumping_past_threshold_still_fires_all(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A single fast-path event can jump from 0% to 60% (the dd
    progress thread fires at ~1Hz; a fast NVMe target writes hundreds
    of MiB between two events). The emitter still fires 25 AND 50 in
    order, not just the highest threshold crossed."""
    fake = tmp_path / "console"
    monkeypatch.setattr("builtins.open", _patch_open_for_console(fake))
    em = tui_app._MilestoneEmitter("download")
    em.update(60, 100)  # crosses both 25 and 50
    assert fake.read_text() == "bty: download 25%\nbty: download 50%\n"


def test_milestone_emitter_swallows_unwritable_console(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A workstation run without a writable /dev/console (laptop, CI
    container, non-Linux) must not crash the wizard: the OSError
    from ``open('/dev/console', 'w')`` is suppressed and the
    milestone simply doesn't land."""
    real_open = open

    def broken_open(path, *args, **kwargs):
        if path == "/dev/console":
            raise PermissionError(13, "permission denied", "/dev/console")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", broken_open)
    em = tui_app._MilestoneEmitter("download")
    em.update(60, 100)  # crosses 25 and 50; both writes raise + are swallowed
    # made it here without raising = pass


# --------------------------------------------------------------------------
# Plan dispatch: /pxe/<mac>/plan response is correctly consumed
# --------------------------------------------------------------------------


def test_fetch_and_dispatch_plan_interactive_preserves_pxe_done_base(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the plan returns ``mode=interactive`` with a third-party
    catalog URL (e.g. a github releases catalog.toml that the bty-
    server hands out as the suggested source), the completion POST
    must still go back to the bty-server. The plan's ``catalog``
    field updates ``_state.catalog_source`` but MUST NOT repoint
    ``_state.pxe_done_base`` -- otherwise a successful flash would
    POST `/pxe/<mac>/done` at github.com (404, silently swallowed
    by the URL-error suppressor), leaving the server-side
    ``last_flashed_at`` stale.
    """
    monkeypatch.setenv("BTY_IMAGE_ROOT", str(tmp_path))
    app = tui_app.BtyTui(server="http://bty-server:8080", mac="aa:bb:cc:dd:ee:ff")
    # Sanity: __init__ sets pxe_done_base to the bty-server URL.
    assert app._state.pxe_done_base == "http://bty-server:8080"

    def fake_urlopen(req, **_kw):
        # Simulate a plan response carrying a third-party catalog
        # URL (not pointing at the bty-server). The bug being
        # guarded against is over-eager pxe_done_base derivation
        # that would repoint /done to github.com.
        payload = (
            b'{"mode": "interactive", '
            b'"catalog": "https://github.com/safl/bty/releases/'
            b'latest/download/catalog.toml"}'
        )
        return _fake_bytes_resp(payload)

    monkeypatch.setattr(tui_app.urllib.request, "urlopen", fake_urlopen)
    action = app._fetch_and_dispatch_plan()
    assert action == "interactive"
    # catalog_source updates to the plan's suggestion ...
    assert app._state.catalog_source == (
        "https://github.com/safl/bty/releases/latest/download/catalog.toml"
    )
    # ... but pxe_done_base stays anchored to the bty-server.
    assert app._state.pxe_done_base == "http://bty-server:8080"


def test_fetch_and_dispatch_plan_auto_populates_auto_image_and_serial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``mode=flash`` plans populate ``_auto_image`` +
    ``_auto_target_disk_serial`` (consumed by ``_run_auto``) and
    return ``"flash"`` as the dispatch token. Without this wiring,
    the auto-flash path would assert-fail trying to dereference
    None values.
    """
    monkeypatch.setenv("BTY_IMAGE_ROOT", str(tmp_path))
    app = tui_app.BtyTui(server="http://bty-server:8080", mac="aa:bb:cc:dd:ee:ff")
    assert app._auto_image is None
    assert app._auto_target_disk_serial is None
    assert app._auto is False

    def fake_urlopen(req, **_kw):
        return _fake_bytes_resp(
            b'{"mode": "flash", '
            b'"image": "http://bty-server:8080/images/abc/demo.img.gz", '
            b'"target_disk_serial": "WD-WX12345", '
            b'"disk_image_sha": "abc"}'
        )

    monkeypatch.setattr(tui_app.urllib.request, "urlopen", fake_urlopen)
    action = app._fetch_and_dispatch_plan()
    assert action == "flash"
    assert app._auto is True
    assert app._auto_image == "http://bty-server:8080/images/abc/demo.img.gz"
    assert app._auto_target_disk_serial == "WD-WX12345"
    # The plan's content sha is captured for on-wire verification.
    assert app._auto_image_sha == "abc"


def test_fetch_and_dispatch_plan_inventory_returns_inventory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``mode=inventory`` plan (boot_mode=bty-inventory) dispatches
    to the ``"inventory"`` token, so ``run()`` posts the disk inventory
    and reboots rather than dropping into the wizard."""
    monkeypatch.setenv("BTY_IMAGE_ROOT", str(tmp_path))
    app = tui_app.BtyTui(server="http://bty-server:8080", mac="aa:bb:cc:dd:ee:ff")
    monkeypatch.setattr(
        tui_app.urllib.request,
        "urlopen",
        lambda req, **_kw: _fake_bytes_resp(b'{"mode": "inventory"}'),
    )
    assert app._fetch_and_dispatch_plan() == "inventory"


def test_collect_lshw_parses_json_and_degrades(monkeypatch: pytest.MonkeyPatch) -> None:
    """``collect_lshw`` returns the parsed ``lshw -json`` tree, and
    folds missing-binary / non-zero-exit / unparseable output to
    ``None`` so the inventory post stays best-effort."""

    class _Proc:
        returncode = 0
        stdout = '{"id": "sys", "class": "system"}'

    monkeypatch.setattr(tui_app.subprocess, "run", lambda *_a, **_kw: _Proc())
    assert tui_app.collect_lshw() == {"id": "sys", "class": "system"}

    class _Fail:
        returncode = 1
        stdout = ""

    monkeypatch.setattr(tui_app.subprocess, "run", lambda *_a, **_kw: _Fail())
    assert tui_app.collect_lshw() is None

    class _Garbage:
        returncode = 0
        stdout = "not json{"

    monkeypatch.setattr(tui_app.subprocess, "run", lambda *_a, **_kw: _Garbage())
    assert tui_app.collect_lshw() is None

    def _missing(*_a: object, **_kw: object) -> None:
        raise FileNotFoundError("lshw not installed")

    monkeypatch.setattr(tui_app.subprocess, "run", _missing)
    assert tui_app.collect_lshw() is None


def test_fetch_and_dispatch_plan_auto_missing_fields_falls_back_to_interactive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the server claims ``mode=flash`` but omits ``image`` or
    ``target_disk_serial``, fall back to interactive with a soft
    error banner. The auto-flash path can't proceed without both
    values; the safest landing is the operator at the wizard.
    """
    monkeypatch.setenv("BTY_IMAGE_ROOT", str(tmp_path))
    app = tui_app.BtyTui(server="http://bty-server:8080", mac="aa:bb:cc:dd:ee:ff")

    def fake_urlopen(req, **_kw):
        # mode=flash but missing target_disk_serial.
        return _fake_bytes_resp(
            b'{"mode": "flash", "image": "http://bty-server:8080/images/abc/demo.img.gz"}'
        )

    monkeypatch.setattr(tui_app.urllib.request, "urlopen", fake_urlopen)
    action = app._fetch_and_dispatch_plan()
    assert action == "interactive"
    assert app._auto is False
    # Operator sees a soft banner explaining the fall-back.
    assert app._catalog_load_error is not None
    assert "missing image/target_disk_serial" in app._catalog_load_error


def test_fetch_and_dispatch_plan_local_returns_local_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``mode=exit`` -> ``"exit"`` dispatch token (the bty caller
    exits cleanly with a "nothing to do here" banner)."""
    monkeypatch.setenv("BTY_IMAGE_ROOT", str(tmp_path))
    app = tui_app.BtyTui(server="http://bty-server:8080", mac="aa:bb:cc:dd:ee:ff")
    monkeypatch.setattr(
        tui_app.urllib.request,
        "urlopen",
        lambda *_a, **_kw: _fake_bytes_resp(b'{"mode": "exit"}'),
    )
    assert app._fetch_and_dispatch_plan() == "exit"


def test_fetch_and_dispatch_plan_unknown_mode_clamps_to_interactive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A server returning an unrecognised mode (rolling release
    drift, mistyped enum) falls back to interactive so the
    operator gets SOMETHING they can act on, plus an error
    banner explaining why."""
    monkeypatch.setenv("BTY_IMAGE_ROOT", str(tmp_path))
    app = tui_app.BtyTui(server="http://bty-server:8080", mac="aa:bb:cc:dd:ee:ff")
    monkeypatch.setattr(
        tui_app.urllib.request,
        "urlopen",
        lambda *_a, **_kw: _fake_bytes_resp(b'{"mode": "telephone"}'),
    )
    action = app._fetch_and_dispatch_plan()
    assert action == "interactive"
    assert app._catalog_load_error is not None
    assert "unknown plan mode" in app._catalog_load_error


def test_fetch_and_dispatch_plan_network_error_falls_back_to_interactive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A transport failure on the plan fetch (DNS miss, refused
    connection, timeout) MUST NOT crash bty -- the operator at
    tty1 should still get a wizard they can drive. Soft-fail to
    interactive with the catalog source set in __init__
    (``<server>/catalog.toml``)."""
    monkeypatch.setenv("BTY_IMAGE_ROOT", str(tmp_path))
    app = tui_app.BtyTui(server="http://unreachable:8080", mac="aa:bb:cc:dd:ee:ff")

    def _boom(*_a, **_kw):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(tui_app.urllib.request, "urlopen", _boom)
    action = app._fetch_and_dispatch_plan()
    assert action == "interactive"
    assert app._catalog_load_error is not None
    assert "plan fetch failed" in app._catalog_load_error


# --------------------------------------------------------------------------
# Flash plumbing: progress callback receives flash.FlashProgress events
# --------------------------------------------------------------------------


def test_screen_flash_running_drives_callback_and_sets_post_flash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``_screen_flash_running`` runs ``flash.execute_plan`` in a
    thread, joins, and flips ``_state.post_flash = True`` on
    success. The progress callback is invoked synchronously from
    the flash thread; we stub execute_plan to emit a small
    sequence of events + return cleanly.
    """
    captured_events: list[str] = []

    def _fake_execute(plan, *, progress=None, cancel=None):
        if progress is not None:
            progress(tui_app.flash.FlashProgress(event="started", total_bytes=1024))
            captured_events.append("started")
            progress(tui_app.flash.FlashProgress(event="writing", note="img.gz"))
            captured_events.append("writing")
            progress(
                tui_app.flash.FlashProgress(
                    event="writing_progress", total_bytes=1024, bytes_written=1024
                )
            )
            captured_events.append("writing_progress")
            progress(tui_app.flash.FlashProgress(event="synced"))
            progress(tui_app.flash.FlashProgress(event="partprobed"))
            progress(tui_app.flash.FlashProgress(event="done"))

    monkeypatch.setattr(tui_app.flash, "execute_plan", _fake_execute)
    # Stub post_pxe_done so the test doesn't try to reach the network.
    monkeypatch.setattr(
        tui_app,
        "post_pxe_done",
        lambda *_a, **_kw: None,
    )

    monkeypatch.setenv("BTY_IMAGE_ROOT", str(tmp_path))
    app = tui_app.BtyTui()
    app._state.selected_image = tui_app._TuiImage(
        name="x", fmt="img.gz", size_bytes=0, url="http://x"
    )
    app._state.selected_disk = {"path": "/dev/null"}

    plan = tui_app.flash.FlashPlan(
        image=tui_app.flash.ImageInfo(
            path=None,
            url="http://x",
            format="img.gz",
            size_bytes=1024,
            virtual_size_bytes=1024,
        ),
        target=tui_app.flash.TargetInfo(
            path=Path("/dev/null"),
            size_bytes=10 * 1024 * 1024,
            exists=True,
            is_block_device=True,
            mountpoints=[],
        ),
    )
    app._screen_flash_running(plan)

    assert "started" in captured_events
    assert "writing_progress" in captured_events
    assert app._state.post_flash is True


def test_screen_flash_running_does_not_advance_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: a flash that raises ``FlashError`` must NOT
    flip ``post_flash`` -- the operator stays on Stage 3 and
    sees the failure panel.
    """

    def _fake_execute(plan, *, progress=None, cancel=None):
        if progress is not None:
            progress(tui_app.flash.FlashProgress(event="started"))
        raise tui_app.flash.FlashError("simulated dd failure")

    monkeypatch.setattr(tui_app.flash, "execute_plan", _fake_execute)
    monkeypatch.setattr(tui_app, "post_pxe_done", lambda *_a, **_kw: None)

    monkeypatch.setenv("BTY_IMAGE_ROOT", str(tmp_path))
    app = tui_app.BtyTui()
    plan = tui_app.flash.FlashPlan(
        image=tui_app.flash.ImageInfo(
            path=None,
            url="http://x",
            format="img.gz",
            size_bytes=1024,
            virtual_size_bytes=1024,
        ),
        target=tui_app.flash.TargetInfo(
            path=Path("/dev/null"),
            size_bytes=10 * 1024 * 1024,
            exists=True,
            is_block_device=True,
            mountpoints=[],
        ),
    )
    # Stub the ack-pause so the test doesn't block on input.
    monkeypatch.setattr(app, "_pause_for_ack", lambda: None)
    app._screen_flash_running(plan)
    assert app._state.post_flash is False


# ---------- auto-flash rejection (diagnosis + no restart-loop) ----------------


def test_print_flash_plan_shows_rejection_reasons(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A rejected flash plan must show WHY -- the error list -- not just
    a bare "Flash plan (rejected)" panel. The reasons used to be dropped,
    which (with bty-on-tty1's Restart=on-failure) left the operator
    watching an undiagnosable reject -> exit -> relaunch loop."""
    from bty import flash

    app = tui_app.BtyTui()
    plan = flash.FlashPlan(
        image=flash.ImageInfo(
            path=Path("/img/x.img"), format="img", size_bytes=1024, virtual_size_bytes=1024
        ),
        target=flash.TargetInfo(
            path=Path("/dev/sda"),
            exists=True,
            is_block_device=True,
            size_bytes=512,
            mountpoints=["/boot"],
        ),
    )
    app._print_flash_plan(plan, ["target has mounted partitions: /boot"])
    out = capsys.readouterr().out
    assert "rejected" in out.lower()
    assert "mounted" in out  # the actual reason is shown, not hidden


# ---------- post-flash reboot prompt: Enter defaults to reboot ----------------


def test_screen_reboot_or_done_enter_defaults_to_reboot(monkeypatch: Any) -> None:
    """At the post-flash reboot prompt, a bare Enter ('') reboots -- the
    operator just flashed and the obvious next step is to boot the new
    disk. (Was 'quit', which surprised operators by leaving the box
    flashed-but-not-rebooted.)"""
    app = tui_app.BtyTui()
    app._state.selected_disk = {"path": "/dev/sda", "size": "8G"}
    monkeypatch.setattr(app, "_ask", lambda *_a, **_kw: "")  # Enter
    rebooted: list[bool] = []
    monkeypatch.setattr(app, "_do_reboot", lambda: rebooted.append(True))
    assert app._screen_reboot_or_done() == "quit"
    assert rebooted == [True]


def test_screen_reboot_or_done_explicit_n_does_not_reboot(monkeypatch: Any) -> None:
    """Explicit 'n' opts out -- no reboot."""
    app = tui_app.BtyTui()
    app._state.selected_disk = {"path": "/dev/sda"}
    monkeypatch.setattr(app, "_ask", lambda *_a, **_kw: "n")
    rebooted: list[bool] = []
    monkeypatch.setattr(app, "_do_reboot", lambda: rebooted.append(True))
    assert app._screen_reboot_or_done() == "quit"
    assert rebooted == []


def test_uefi_boot_registration_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """efibootmgr registration is opt-in: unset env -> disabled."""
    monkeypatch.delenv("BTY_REGISTER_UEFI_BOOT", raising=False)
    assert tui_app._uefi_boot_registration_enabled() is False


def test_uefi_boot_registration_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    for truthy in ("1", "true", "YES", "On"):
        monkeypatch.setenv("BTY_REGISTER_UEFI_BOOT", truthy)
        assert tui_app._uefi_boot_registration_enabled() is True
    for falsy in ("", "0", "false", "no", "off", "nonsense"):
        monkeypatch.setenv("BTY_REGISTER_UEFI_BOOT", falsy)
        assert tui_app._uefi_boot_registration_enabled() is False
