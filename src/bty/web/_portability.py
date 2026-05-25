"""Slim portable export / import of bty-web state.

v0.31.0+: trimmed to "carry the data that's expensive or impossible
to re-collect; re-do operator decisions on the new appliance." The
bundle carries:

  - the contents of ``BTY_IMAGE_ROOT`` (both operator-typed images
    and catalog-fetched ``catalog-<ref:12>-<slug>.<ext>`` files,
    merged into one flat ``files/`` subdir);
  - a minimal per-machine record: ``mac``, ``hw_lshw`` (lshw -json
    blob), ``known_disks`` (lsblk-style array), and the timestamps
    on each.

It deliberately does NOT carry:

  - catalog entries (re-import the catalog on the new appliance);
  - per-machine bindings (``boot_mode``, ``bty_image_ref``,
    ``target_disk_serial``, ``sanboot_drive``, ``hostname``) --
    operator re-binds in the new appliance;
  - ``saw_flasher_boot`` state, audit log, settings, backups.

Bundle layout::

    <bundle>/
      manifest.json   # {bty_export_version, exported_at,
                      #  exported_by_bty_version, machines: [...] }
      files/          # everything from BTY_IMAGE_ROOT (flat)

``manifest.json`` carries ``bty_export_version`` (currently 2). The
import refuses anything else -- the format is version-tolerant in
the sense that a v2 bundle imports cleanly on any bty release that
understands the v2 format, but a v1 bundle (pre-v0.31.0, separate
images/ + cache/ subdirs + machine-bindings + catalog_entries)
isn't migratable; the operator regenerates the bundle on the source
release.

It also carries ``exported_by_bty_version`` for diagnostics, but
that's informational only -- the slim payload is by design version-
tolerant (just machine inventory + raw image files).
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path

import bty

from . import _db

# Bumped from 1 in v0.31.0 -- breaking change to bundle layout
# (flat files/ instead of images/ + cache/; no catalog_entries
# section; slim machine records).
_EXPORT_VERSION = 2

# Per-machine columns the slim export carries. Mac + lshw +
# known_disks (the "what hardware does this MAC bind to") is the
# expensive-to-re-collect part; everything else gets re-typed by
# the operator on the new appliance.
_MACHINE_EXPORT_COLS = (
    "mac",
    "known_disks",
    "known_disks_at",
    "hw_lshw",
    "hw_lshw_at",
)


@dataclass
class ExportSummary:
    machines: int
    files: int
    dest: Path


@dataclass
class ImportSummary:
    machines: int
    files: int
    skipped: list[str] = field(default_factory=list)


class BundleVersionMismatch(ValueError):
    """Raised by :func:`import_bundle` when the bundle's
    ``bty_export_version`` doesn't match the running code's
    expected value. Pre-1.0 policy: bundles don't migrate
    across major-format bumps -- regenerate on the source
    release."""


def export_bundle(
    state_path: Path,
    image_root: Path,
    dest: Path,
    *,
    now: str,
) -> ExportSummary:
    """Write a slim portable bundle of preservable state to ``dest``.

    Reads minimal machine records (mac + hw_lshw + known_disks +
    their timestamps) from ``state_path`` and copies everything
    under ``image_root`` (operator-typed images AND catalog-fetched
    ``catalog-<ref:12>-<slug>.<ext>`` files, treated identically)
    into ``dest/files/``. ``dest`` is created if absent.
    """
    dest.mkdir(parents=True, exist_ok=True)
    with _db.open_db(state_path) as conn:
        m_rows = conn.execute(
            f"SELECT {', '.join(_MACHINE_EXPORT_COLS)} FROM machines ORDER BY mac"
        ).fetchall()
    machines = [dict(r) for r in m_rows]
    manifest = {
        "bty_export_version": _EXPORT_VERSION,
        "exported_at": now,
        "exported_by_bty_version": bty.__version__,
        "machines": machines,
    }
    (dest / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")

    files_dest = dest / "files"
    files_dest.mkdir(exist_ok=True)
    n_files = 0
    if image_root.is_dir():
        for f in sorted(image_root.iterdir()):
            if f.is_file():
                shutil.copy2(f, files_dest / f.name)
                n_files += 1
    return ExportSummary(len(machines), n_files, dest)


def import_bundle(
    state_path: Path,
    image_root: Path,
    src: Path,
    *,
    now: str,
) -> ImportSummary:
    """Load a bundle written by :func:`export_bundle` into
    ``state_path`` + ``image_root``.

    Machines are inserted as ``boot_mode=bty-inventory`` with empty
    bindings + the transient state reset; existing rows (same mac)
    are replaced. Files under ``files/`` are copied into the running
    ``image_root`` -- catalog-prefixed files keep their URL-keyed
    name so the new appliance's catalog re-import wires the
    "cached" state automatically. Operator-typed files just sit
    there alongside.

    Raises :class:`BundleVersionMismatch` if the bundle's version
    doesn't match the format this release understands.
    """
    manifest_path = src / "manifest.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(f"no manifest.json in bundle: {src}")
    manifest = json.loads(manifest_path.read_text())
    ver = manifest.get("bty_export_version")
    if ver != _EXPORT_VERSION:
        raise BundleVersionMismatch(
            f"bundle bty_export_version={ver!r}, expected {_EXPORT_VERSION!r}. "
            f"v0.31.0 introduced a slim bundle format (flat files/ + minimal "
            f"machine records); v1 bundles (pre-v0.31.0) aren't migratable -- "
            f"regenerate on the source release."
        )

    n_m = 0
    with _db.open_db(state_path) as conn:
        for m in manifest.get("machines", []):
            # boot_mode forced to bty-inventory + bindings/timestamps
            # reset. The machine arrives as a freshly-discovered box
            # with just its hardware fingerprint pre-filled.
            conn.execute(
                """
                INSERT OR REPLACE INTO machines
                    (mac, hostname, bty_image_ref, target_disk_serial,
                     sanboot_drive, known_disks, known_disks_at,
                     hw_lshw, hw_lshw_at,
                     boot_mode, saw_flasher_boot, last_flashed_at,
                     discovered_at, last_seen_at, last_seen_ip,
                     created_at, updated_at)
                VALUES (?, NULL, NULL, NULL,
                        NULL, ?, ?, ?, ?,
                        'bty-inventory', 0, NULL,
                        NULL, NULL, NULL, ?, ?)
                """,
                (
                    m.get("mac"),
                    m.get("known_disks"),
                    m.get("known_disks_at"),
                    m.get("hw_lshw"),
                    m.get("hw_lshw_at"),
                    now,
                    now,
                ),
            )
            n_m += 1
        conn.commit()

    image_root.mkdir(parents=True, exist_ok=True)
    n_files = 0
    files_src = src / "files"
    if files_src.is_dir():
        for f in sorted(files_src.iterdir()):
            if f.is_file():
                shutil.copy2(f, image_root / f.name)
                n_files += 1
    return ImportSummary(n_m, n_files)
