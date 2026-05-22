"""Export / import the operator-owned half of bty-web's state.

bty's state splits in two: what the operator put in (machine hardware
identities + image bindings, the image catalog + cached image files) and
what bty itself manages (netboot artifacts, settings, the audit log, and
the transient boot-mode state bit). Only the first half is portable --
it's what survives a migration to a new server, and what's worth backing
up. This module moves exactly that, and nothing else, so it's explicit
what travels and what is re-created fresh on the destination.

A bundle is a directory:

    <bundle>/
      manifest.json   # export metadata + machines[] + catalog_entries[]
      images/         # the local image files (BTY_IMAGE_ROOT contents)

Machines come back as ``boot_mode=bty-inventory`` regardless of what they
were exported as -- in fact ``boot_mode`` isn't exported at all. The
operator re-baselines flashing intent on the destination (against
freshly-fetched netboot artifacts) rather than inheriting a flash mode
that might fire against stale config. Everything else operator-owned
carries over: the mac, the lshw tree, the disk inventory, the image
binding, the target-disk serial, and the hostname. The transient bits
(``saw_flasher_boot``, ``last_flashed_at``) and the server-maintained
timestamps reset.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from . import _db

_EXPORT_VERSION = 1

# Machine columns the operator owns and that carry across a migration.
# ``boot_mode`` is deliberately absent (import forces bty-inventory); the
# transient state bit + the server-maintained timestamps are not exported
# and reset on import.
_MACHINE_EXPORT_COLS = (
    "mac",
    "hostname",
    "bty_image_ref",
    "target_disk_serial",
    "sanboot_drive",
    "known_disks",
    "known_disks_at",
    "hw_lshw",
    "hw_lshw_at",
)
_CATALOG_COLS = (
    "bty_image_ref",
    "src",
    "disk_image_sha",
    "name",
    "sha_url",
    "format",
    "size_bytes",
    "description",
    "added_at",
)


@dataclass
class ExportSummary:
    machines: int
    catalog_entries: int
    images: int
    dest: Path


@dataclass
class ImportSummary:
    machines: int
    catalog_entries: int
    images: int
    skipped: list[str] = field(default_factory=list)


def export_bundle(
    state_path: Path,
    image_root: Path,
    dest: Path,
    *,
    bty_version: str,
    now: str,
) -> ExportSummary:
    """Write a portable bundle of the operator-owned state to ``dest``.

    Reads machine + catalog rows from ``state_path`` and copies the local
    image files from ``image_root``. ``dest`` is created if absent.
    """
    dest.mkdir(parents=True, exist_ok=True)
    with _db.open_db(state_path) as conn:
        m_rows = conn.execute(
            f"SELECT {', '.join(_MACHINE_EXPORT_COLS)} FROM machines ORDER BY mac"
        ).fetchall()
        c_rows = conn.execute(
            f"SELECT {', '.join(_CATALOG_COLS)} FROM catalog_entries ORDER BY name"
        ).fetchall()
    machines = [dict(r) for r in m_rows]
    catalog = [dict(r) for r in c_rows]
    manifest = {
        "bty_export_version": _EXPORT_VERSION,
        "exported_at": now,
        "bty_version": bty_version,
        "machines": machines,
        "catalog_entries": catalog,
    }
    (dest / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")

    img_dest = dest / "images"
    img_dest.mkdir(exist_ok=True)
    n_images = 0
    if image_root.is_dir():
        for f in sorted(image_root.iterdir()):
            if f.is_file():
                shutil.copy2(f, img_dest / f.name)
                n_images += 1
    return ExportSummary(len(machines), len(catalog), n_images, dest)


def import_bundle(
    state_path: Path,
    image_root: Path,
    src: Path,
    *,
    now: str,
) -> ImportSummary:
    """Load a bundle written by :func:`export_bundle` into ``state_path``
    + ``image_root``.

    Machines are inserted as ``boot_mode=bty-inventory`` with the
    transient state reset; existing rows (same mac / catalog ref) are
    replaced. Catalog rows that would violate the ``src`` UNIQUE
    constraint (a different ref already owns that src) are skipped and
    reported rather than aborting the whole import.
    """
    manifest_path = src / "manifest.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(f"no manifest.json in bundle: {src}")
    manifest = json.loads(manifest_path.read_text())
    ver = manifest.get("bty_export_version")
    if ver != _EXPORT_VERSION:
        raise ValueError(f"unsupported bundle version {ver!r} (expected {_EXPORT_VERSION})")

    skipped: list[str] = []
    n_m = 0
    n_c = 0
    with _db.open_db(state_path) as conn:
        for m in manifest.get("machines", []):
            # boot_mode forced to bty-inventory; saw_flasher_boot +
            # last_flashed_at + the discovery/seen timestamps reset. The
            # machine arrives as a freshly-discovered box with its
            # hardware + binding pre-filled.
            conn.execute(
                """
                INSERT OR REPLACE INTO machines
                    (mac, hostname, bty_image_ref, target_disk_serial,
                     sanboot_drive, known_disks, known_disks_at,
                     hw_lshw, hw_lshw_at,
                     boot_mode, saw_flasher_boot, last_flashed_at,
                     discovered_at, last_seen_at, last_seen_ip,
                     created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?,
                        'bty-inventory', 0, NULL,
                        NULL, NULL, NULL, ?, ?)
                """,
                (
                    m.get("mac"),
                    m.get("hostname"),
                    m.get("bty_image_ref"),
                    m.get("target_disk_serial"),
                    m.get("sanboot_drive"),
                    m.get("known_disks"),
                    m.get("known_disks_at"),
                    m.get("hw_lshw"),
                    m.get("hw_lshw_at"),
                    now,
                    now,
                ),
            )
            n_m += 1
        for c in manifest.get("catalog_entries", []):
            try:
                conn.execute(
                    f"INSERT OR REPLACE INTO catalog_entries "
                    f"({', '.join(_CATALOG_COLS)}) "
                    f"VALUES ({', '.join(['?'] * len(_CATALOG_COLS))})",
                    tuple(c.get(col) for col in _CATALOG_COLS),
                )
                n_c += 1
            except sqlite3.IntegrityError as exc:
                skipped.append(f"catalog {c.get('src')!r}: {exc}")
        conn.commit()

    image_root.mkdir(parents=True, exist_ok=True)
    n_images = 0
    img_src = src / "images"
    if img_src.is_dir():
        for f in sorted(img_src.iterdir()):
            if f.is_file():
                shutil.copy2(f, image_root / f.name)
                n_images += 1
    return ImportSummary(n_m, n_c, n_images, skipped)
