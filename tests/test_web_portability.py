"""Round-trip tests for the metadata-only export/import tool.

v0.33.2+ contract: the bundle is just ``inventory.json``; no image
bytes, no ``files/`` subdir. The bundle carries minimal per-machine
records (``mac``, ``hw_lshw``, ``known_disks``). Everything else
(boot mode, image bindings, catalog entries, settings, audit log)
is operator-re-typed on the destination appliance. Image bytes
travel via the image-store disk or re-fetched from the catalog --
not in the backup.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bty.web import _db, _portability


def _seed(state_path: Path) -> None:
    _db.init_db(state_path)
    with _db.open_db(state_path) as conn:
        # The slim format intentionally drops bindings on the
        # machine row -- but the source DB still has them.
        # We're testing that they DON'T travel.
        conn.execute(
            "INSERT INTO machines (mac, bty_image_ref, boot_mode, sanboot_drive, "
            "saw_flasher_boot, known_disks, known_disks_at, hw_lshw, hw_lshw_at, "
            "target_disk_serial, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "aa:bb:cc:dd:ee:ff",
                "ref-demo",
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
        # Sample labels in the side table so the "labels stay behind on
        # import" assertion has something to test against.
        conn.execute(
            "INSERT INTO machine_labels (mac, label) VALUES (?, ?)",
            ("aa:bb:cc:dd:ee:ff", "rack-3"),
        )
        conn.execute(
            "INSERT INTO machine_labels (mac, label) VALUES (?, ?)",
            ("aa:bb:cc:dd:ee:ff", "noisy"),
        )
        conn.commit()


def test_export_import_round_trip(tmp_path: Path) -> None:
    src_state = tmp_path / "src" / "state.db"
    src_state.parent.mkdir()
    _seed(src_state)

    bundle = tmp_path / "bundle"
    exp = _portability.export_bundle(src_state, bundle, now="2026-05-22T02:00:00+00:00")
    # v3 metadata-only: just the machines.
    assert exp.machines == 1

    inventory = json.loads((bundle / "inventory.json").read_text())
    assert inventory["bty_export_version"] == 3
    # Slim machine record: only mac + hardware-inventory fields.
    # known_disks + hw_lshw decode to NATIVE shapes (array / object),
    # not re-encoded JSON strings -- so the bundle is jq-readable.
    machine = inventory["machines"][0]
    assert machine["mac"] == "aa:bb:cc:dd:ee:ff"
    assert machine["hw_lshw"] == {"id": "system"}
    assert isinstance(machine["known_disks"], list)
    assert machine["known_disks"][0]["path"] == "/dev/sda"
    assert machine["known_disks"][0]["serial"] == "SER1"
    # Bindings + boot_mode + transient state DELIBERATELY absent.
    for forbidden in (
        "boot_mode",
        "bty_image_ref",
        "target_disk_serial",
        "sanboot_drive",
        "labels",
        "saw_flasher_boot",
        "last_flashed_at",
    ):
        assert forbidden not in machine, f"slim format must not export {forbidden}"
    # Slim format doesn't carry catalog data (withcache owns it).
    assert "catalog_entries" not in inventory
    # CRITICALLY: no files/ subdir. v3 is metadata-only.
    assert not (bundle / "files").exists(), (
        "v3 bundles must not carry image bytes; that's what made daily "
        "backups multi-GiB through v0.33.1"
    )
    assert not (bundle / "images").exists()
    assert not (bundle / "cache").exists()
    # Bundle size should be small (just the JSON).
    bundle_bytes = (bundle / "inventory.json").stat().st_size
    assert bundle_bytes < 10_000, f"metadata-only bundle should be tiny, got {bundle_bytes} bytes"

    # Import into a fresh destination (the migration case).
    dst_state = tmp_path / "dst" / "state.db"
    dst_state.parent.mkdir()
    _db.init_db(dst_state)
    imp = _portability.import_bundle(dst_state, bundle, now="2026-05-22T03:00:00+00:00")
    assert imp.machines == 1
    assert imp.skipped == []

    with _db.open_db(dst_state) as conn:
        m = dict(
            conn.execute("SELECT * FROM machines WHERE mac=?", ("aa:bb:cc:dd:ee:ff",)).fetchone()
        )
        dst_labels = [
            r[0]
            for r in conn.execute(
                "SELECT label FROM machine_labels WHERE mac=? ORDER BY label",
                ("aa:bb:cc:dd:ee:ff",),
            ).fetchall()
        ]
    # Slim import: hardware fingerprint carried, NOTHING else.
    assert m["hw_lshw"] == '{"id": "system"}'
    assert "/dev/sda" in m["known_disks"]
    # Bindings reset on import (operator re-binds on the new appliance).
    # Labels are bindings: they don't travel.
    assert dst_labels == []
    assert m["bty_image_ref"] is None
    assert m["target_disk_serial"] is None
    assert m["sanboot_drive"] is None
    assert m["boot_mode"] == "bty-inventory"
    assert m["saw_flasher_boot"] == 0
    assert m["last_flashed_at"] is None
    # Catalog data lives in withcache; the bundle never carried it.


def test_import_rejects_unknown_bundle_version(tmp_path: Path) -> None:
    bundle = tmp_path / "b"
    bundle.mkdir()
    (bundle / "inventory.json").write_text(json.dumps({"bty_export_version": 999}))
    state = tmp_path / "state.db"
    _db.init_db(state)
    with pytest.raises(_portability.BundleVersionMismatch, match="bty_export_version=999"):
        _portability.import_bundle(state, bundle, now="x")


def test_import_rejects_v1_legacy_bundle(tmp_path: Path) -> None:
    """v1 bundles (pre-v0.31.0) and v2 bundles (v0.31.0..v0.33.1, with
    image bytes) are no longer migratable. Pre-1.0 policy: regenerate
    on the source release."""
    bundle = tmp_path / "b"
    bundle.mkdir()
    (bundle / "inventory.json").write_text(json.dumps({"bty_export_version": 1}))
    state = tmp_path / "state.db"
    _db.init_db(state)
    with pytest.raises(_portability.BundleVersionMismatch, match="bty_export_version=1"):
        _portability.import_bundle(state, bundle, now="x")


def test_import_rejects_v2_legacy_bundle(tmp_path: Path) -> None:
    """v2 bundles (v0.31.0..v0.33.1) carried image bytes alongside the
    manifest. The v3 format dropped image bytes entirely; old bundles
    must regenerate on the source release."""
    bundle = tmp_path / "b"
    bundle.mkdir()
    (bundle / "inventory.json").write_text(json.dumps({"bty_export_version": 2}))
    state = tmp_path / "state.db"
    _db.init_db(state)
    with pytest.raises(_portability.BundleVersionMismatch, match="bty_export_version=2"):
        _portability.import_bundle(state, bundle, now="x")
