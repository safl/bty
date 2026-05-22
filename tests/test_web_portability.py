"""Round-trip tests for the export/import (migration / backup) tool.

The contract: the operator-owned half of the state (machine hardware
identities + bindings, the catalog, the local image files) travels; the
boot mode does NOT (machines arrive as bty-inventory), and the transient
state bit + server timestamps reset.
"""

from __future__ import annotations

import json
from pathlib import Path

from bty.web import _db, _portability


def _seed(state_path: Path, image_root: Path) -> None:
    _db.init_db(state_path)
    image_root.mkdir(parents=True, exist_ok=True)
    (image_root / "demo.img").write_bytes(b"\xff" * 64)
    with _db.open_db(state_path) as conn:
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
    exp = _portability.export_bundle(
        src_state, src_images, bundle, bty_version="9.9.9", now="2026-05-22T02:00:00+00:00"
    )
    assert (exp.machines, exp.catalog_entries, exp.images) == (1, 1, 1)
    manifest = json.loads((bundle / "manifest.json").read_text())
    # boot_mode is deliberately NOT in the bundle.
    assert "boot_mode" not in manifest["machines"][0]
    assert (bundle / "images" / "demo.img").is_file()

    # Import into a fresh destination (the migration case).
    dst_state = tmp_path / "dst" / "state.db"
    dst_state.parent.mkdir()
    dst_images = tmp_path / "dst" / "images"
    _db.init_db(dst_state)
    imp = _portability.import_bundle(dst_state, dst_images, bundle, now="2026-05-22T03:00:00+00:00")
    assert (imp.machines, imp.catalog_entries, imp.images) == (1, 1, 1)
    assert imp.skipped == []

    with _db.open_db(dst_state) as conn:
        m = dict(
            conn.execute("SELECT * FROM machines WHERE mac=?", ("aa:bb:cc:dd:ee:ff",)).fetchone()
        )
        c = dict(
            conn.execute(
                "SELECT * FROM catalog_entries WHERE bty_image_ref=?", ("ref-demo",)
            ).fetchone()
        )
    # Operator-owned fields carried over...
    assert m["hostname"] == "lab-box"
    assert m["bty_image_ref"] == "ref-demo"
    assert m["target_disk_serial"] == "SER1"
    assert m["sanboot_drive"] == "0x80"
    assert m["hw_lshw"] == '{"id": "system"}'
    assert "/dev/sda" in m["known_disks"]
    # ...but the boot mode resets to bty-inventory + transient state clears.
    assert m["boot_mode"] == "bty-inventory"
    assert m["saw_flasher_boot"] == 0
    assert m["last_flashed_at"] is None
    # Catalog + image file present at the destination.
    assert c["name"] == "demo.img"
    assert (dst_images / "demo.img").read_bytes() == b"\xff" * 64


def test_import_rejects_unknown_bundle_version(tmp_path: Path) -> None:
    bundle = tmp_path / "b"
    bundle.mkdir()
    (bundle / "manifest.json").write_text(json.dumps({"bty_export_version": 999}))
    state = tmp_path / "state.db"
    _db.init_db(state)
    try:
        _portability.import_bundle(state, tmp_path / "img", bundle, now="x")
    except ValueError as exc:
        assert "unsupported bundle version" in str(exc)
    else:
        raise AssertionError("expected ValueError on version mismatch")
