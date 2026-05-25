"""Round-trip tests for the slim export/import (migration / backup) tool.

v0.31.0+ contract: the bundle carries ONLY ``BTY_IMAGE_ROOT`` files
(operator-typed + catalog-fetched flat) and minimal per-machine
records (``mac``, ``hw_lshw``, ``known_disks``). Everything else
(boot mode, image bindings, catalog entries, settings, audit log)
is operator-re-typed on the destination appliance.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bty.web import _db, _portability


def _seed(state_path: Path, image_root: Path) -> None:
    _db.init_db(state_path)
    image_root.mkdir(parents=True, exist_ok=True)
    (image_root / "demo.img").write_bytes(b"\xff" * 64)
    # Stage a representative catalog-fetched filename too -- the slim
    # bundle should carry it byte-identical alongside the operator file.
    (image_root / "catalog-deadbeefcafe-fedora-sysdev.img.gz").write_bytes(b"\xaa" * 32)
    with _db.open_db(state_path) as conn:
        # The slim format intentionally drops catalog_entries on export
        # and bindings on the machine row -- but the source DB still
        # has them. We're testing that they DON'T travel.
        conn.execute(
            "INSERT INTO catalog_entries (bty_image_ref, src, disk_image_sha, name, "
            "sha_url, format, size_bytes, description, added_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                "ref-demo",
                "file://demo.img",
                "sha-demo",
                "demo.img",
                None,
                "img",
                64,
                None,
                "2026-05-22T00:00:00+00:00",
            ),
        )
        conn.execute(
            "INSERT INTO machines (mac, bty_image_ref, hostname, boot_mode, sanboot_drive, "
            "saw_flasher_boot, known_disks, known_disks_at, hw_lshw, hw_lshw_at, "
            "target_disk_serial, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "aa:bb:cc:dd:ee:ff",
                "ref-demo",
                "lab-box",
                "bty-flash-once",
                "0x80",
                1,
                '[{"path": "/dev/sda", "serial": "SER1"}]',
                "2026-05-22T01:00:00+00:00",
                '{"id": "system"}',
                "2026-05-22T01:00:00+00:00",
                "SER1",
                "2026-05-22T00:00:00+00:00",
                "2026-05-22T00:30:00+00:00",
            ),
        )
        conn.commit()


def test_export_import_round_trip(tmp_path: Path) -> None:
    src_state = tmp_path / "src" / "state.db"
    src_state.parent.mkdir()
    src_images = tmp_path / "src" / "images"
    _seed(src_state, src_images)

    bundle = tmp_path / "bundle"
    exp = _portability.export_bundle(src_state, src_images, bundle, now="2026-05-22T02:00:00+00:00")
    # Slim format: only machines + files (no catalog_entries / images
    # split). Both files land in the flat files/ subdir.
    assert exp.machines == 1
    assert exp.files == 2

    manifest = json.loads((bundle / "manifest.json").read_text())
    # Slim machine record: only mac + hardware-inventory fields.
    machine = manifest["machines"][0]
    assert machine["mac"] == "aa:bb:cc:dd:ee:ff"
    assert machine["hw_lshw"] == '{"id": "system"}'
    assert "/dev/sda" in machine["known_disks"]
    # Bindings + boot_mode + transient state DELIBERATELY absent.
    for forbidden in (
        "boot_mode",
        "bty_image_ref",
        "target_disk_serial",
        "sanboot_drive",
        "hostname",
        "saw_flasher_boot",
        "last_flashed_at",
    ):
        assert forbidden not in machine, f"slim format must not export {forbidden}"
    # No catalog_entries section at all.
    assert "catalog_entries" not in manifest
    # Files land flat under files/, not split images/ + cache/.
    assert (bundle / "files" / "demo.img").is_file()
    assert (bundle / "files" / "catalog-deadbeefcafe-fedora-sysdev.img.gz").is_file()
    assert not (bundle / "images").exists()
    assert not (bundle / "cache").exists()

    # Import into a fresh destination (the migration case).
    dst_state = tmp_path / "dst" / "state.db"
    dst_state.parent.mkdir()
    dst_images = tmp_path / "dst" / "images"
    _db.init_db(dst_state)
    imp = _portability.import_bundle(dst_state, dst_images, bundle, now="2026-05-22T03:00:00+00:00")
    assert (imp.machines, imp.files) == (1, 2)
    assert imp.skipped == []

    with _db.open_db(dst_state) as conn:
        m = dict(
            conn.execute("SELECT * FROM machines WHERE mac=?", ("aa:bb:cc:dd:ee:ff",)).fetchone()
        )
        c_count = conn.execute("SELECT COUNT(*) FROM catalog_entries").fetchone()[0]
    # Slim import: hardware fingerprint carried, NOTHING else.
    assert m["hw_lshw"] == '{"id": "system"}'
    assert "/dev/sda" in m["known_disks"]
    # Bindings reset on import (operator re-binds on the new appliance).
    assert m["hostname"] is None
    assert m["bty_image_ref"] is None
    assert m["target_disk_serial"] is None
    assert m["sanboot_drive"] is None
    assert m["boot_mode"] == "bty-inventory"
    assert m["saw_flasher_boot"] == 0
    assert m["last_flashed_at"] is None
    # Catalog rows are NOT imported -- re-import the catalog on the
    # new appliance.
    assert c_count == 0
    # Files present at the destination's image_root.
    assert (dst_images / "demo.img").read_bytes() == b"\xff" * 64
    assert (dst_images / "catalog-deadbeefcafe-fedora-sysdev.img.gz").read_bytes() == b"\xaa" * 32


def test_import_rejects_unknown_bundle_version(tmp_path: Path) -> None:
    bundle = tmp_path / "b"
    bundle.mkdir()
    (bundle / "manifest.json").write_text(json.dumps({"bty_export_version": 999}))
    state = tmp_path / "state.db"
    _db.init_db(state)
    with pytest.raises(_portability.BundleVersionMismatch, match="bty_export_version=999"):
        _portability.import_bundle(state, tmp_path / "img", bundle, now="x")


def test_import_rolls_back_partial_file_copies_on_oserror(tmp_path: Path) -> None:
    """v0.32.1: ``import_bundle``'s file-copy loop is half-atomic --
    if any single ``shutil.copy2`` raises ``OSError`` (disk full,
    permissions, race), every file already copied gets unlinked
    before the exception re-raises. This avoids the partial-import
    state v0.32.0 ran into: DB transaction committed, then file N
    of M failed to copy, leaving N-1 files in image_root with no
    rollback path.

    Reproduce by making the destination image_root read-only after
    the first file has been copied; the second copy hits PermissionError,
    the helper catches it, unlinks the first, and re-raises an
    annotated OSError.
    """
    src_state = tmp_path / "src" / "state.db"
    src_state.parent.mkdir()
    src_images = tmp_path / "src" / "images"
    src_images.mkdir(parents=True, exist_ok=True)
    (src_images / "first.img.gz").write_bytes(b"\xaa" * 16)
    (src_images / "second.img.gz").write_bytes(b"\xbb" * 16)
    _db.init_db(src_state)
    bundle = tmp_path / "bundle"
    _portability.export_bundle(src_state, src_images, bundle, now="x")
    assert (bundle / "files" / "first.img.gz").is_file()
    assert (bundle / "files" / "second.img.gz").is_file()

    dst_state = tmp_path / "dst" / "state.db"
    dst_state.parent.mkdir()
    dst_images = tmp_path / "dst" / "images"
    dst_images.mkdir(parents=True, exist_ok=True)

    # Monkeypatch ``shutil.copy2`` so the SECOND call raises. The
    # first call lands "first.img.gz" in dst_images; the helper must
    # unlink it before re-raising.
    import shutil as _shutil

    real_copy = _shutil.copy2
    calls: list[Path] = []

    def fake_copy(src: Path, dst: Path, *args: object, **kw: object) -> Path:
        calls.append(Path(dst))
        if len(calls) >= 2:
            raise OSError("ENOSPC: simulated disk full")
        return Path(real_copy(src, dst, *args, **kw))

    import bty.web._portability as _portability_mod

    _portability_mod.shutil.copy2 = fake_copy  # type: ignore[assignment]
    try:
        with pytest.raises(OSError, match="copy failed after 1 files"):
            _portability.import_bundle(dst_state, dst_images, bundle, now="y")
    finally:
        _portability_mod.shutil.copy2 = real_copy  # type: ignore[assignment]

    # The first file (successfully copied before the failure) must
    # have been cleaned up. Operator sees image_root in its pre-import
    # state, not a half-loaded mess.
    remaining = sorted(p.name for p in dst_images.iterdir())
    assert remaining == [], (
        f"partial copy not cleaned up: dst_images={remaining!r} "
        f"(expected empty; the rollback step should have unlinked "
        f"first.img.gz before re-raising)"
    )


def test_import_rejects_v1_legacy_bundle(tmp_path: Path) -> None:
    """v1 bundles (pre-v0.31.0, with separate ``images/`` / ``cache/``
    subdirs + machine bindings + catalog_entries section) are no longer
    migratable. Pre-1.0 policy: regenerate on the source release."""
    bundle = tmp_path / "b"
    bundle.mkdir()
    (bundle / "manifest.json").write_text(json.dumps({"bty_export_version": 1}))
    state = tmp_path / "state.db"
    _db.init_db(state)
    with pytest.raises(_portability.BundleVersionMismatch, match="bty_export_version=1"):
        _portability.import_bundle(state, tmp_path / "img", bundle, now="x")
