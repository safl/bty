"""Metadata-only portable export / import of bty-web state.

v0.33.2+: the bundle is just ``inventory.json``. No image bytes,
no ``files/`` subdir. The expensive-and-hard-to-recollect data is
the per-machine hardware identity (mac + lshw + known_disks);
image bytes are recoverable from the upstream catalog or already
on the image-store disk.

Bundle layout::

    <bundle>/
      inventory.json   # {bty_export_version, exported_at,
                       #  exported_by_bty_version, machines: [...] }

``inventory.json`` carries ``bty_export_version`` (currently 3).
The import refuses anything else. Pre-1.0 policy: bundles don't
migrate across major-format bumps -- regenerate on the source
release. The name reflects what the file actually is: a machine
inventory (mac + lshw + known_disks), distinct from the catalog
``manifest`` (``${BTY_STATE_DIR}/catalog.toml`` -- a different
file with a different schema).

Why metadata-only: a routine backup runs daily on a cadence, so the
size matters. Earlier releases (v0.31.0 through v0.33.1) shipped
full image_root in every bundle, which produced multi-GiB "backups"
that were dominated by catalog cache files the appliance can just
re-fetch. Splitting image transport out of backup means a daily
backup is dozens of KiB and finishes in milliseconds; an operator
who wants to move an appliance's image_root to a new box uses
``rsync`` or just moves the image-store disk.

The bundle deliberately does NOT carry:

  - image bytes (re-fetch from catalog or copy the image disk);
  - catalog entries (re-import the catalog on the new appliance);
  - per-machine bindings (``boot_mode``, ``bty_image_ref``,
    ``target_disk_serial``, ``sanboot_drive``, ``hostname``) --
    operator re-binds in the new appliance;
  - ``saw_flasher_boot`` state, audit log, settings, backups.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import bty

from . import _db

# v3 = metadata-only (no files/ subdir). v2 was metadata + full
# image_root copy; v1 was the pre-v0.31.0 layout. Both refused on
# import -- regenerate on the source release.
_EXPORT_VERSION = 3

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
    dest: Path


@dataclass
class ImportSummary:
    machines: int
    skipped: list[str] = field(default_factory=list)


class BundleVersionMismatch(ValueError):
    """Raised by :func:`import_bundle` when the bundle's
    ``bty_export_version`` doesn't match the running code's
    expected value. Pre-1.0 policy: bundles don't migrate
    across major-format bumps -- regenerate on the source
    release."""


def export_bundle(
    state_path: Path,
    dest: Path,
    *,
    now: str,
) -> ExportSummary:
    """Write a metadata-only bundle of preservable state to ``dest``.

    Reads minimal machine records (mac + hw_lshw + known_disks +
    their timestamps) from ``state_path`` and writes them to
    ``dest/inventory.json``. ``dest`` is created if absent. No
    image bytes are touched -- this is the routine-backup
    primitive.

    ``known_disks`` and ``hw_lshw`` live in sqlite as JSON-encoded
    TEXT; ``_decode_machine`` decodes them on the way out so the
    inventory carries native objects/arrays rather than re-encoded
    strings. Operators can ``jq`` it without an extra decode step.
    """
    dest.mkdir(parents=True, exist_ok=True)
    with _db.open_db(state_path) as conn:
        m_rows = conn.execute(
            f"SELECT {', '.join(_MACHINE_EXPORT_COLS)} FROM machines ORDER BY mac"
        ).fetchall()
    machines = [_decode_machine(dict(r)) for r in m_rows]
    inventory = {
        "bty_export_version": _EXPORT_VERSION,
        "exported_at": now,
        "exported_by_bty_version": bty.__version__,
        "machines": machines,
    }
    (dest / "inventory.json").write_text(json.dumps(inventory, indent=2) + "\n")
    return ExportSummary(len(machines), dest)


def _decode_machine(row: dict) -> dict:
    """Decode the JSON-TEXT columns (``known_disks``, ``hw_lshw``) so the
    exported inventory carries native objects/arrays. NULL stays NULL;
    malformed JSON degrades to NULL rather than crashing the export."""
    out = dict(row)
    for col in ("known_disks", "hw_lshw"):
        raw = out.get(col)
        if raw is None:
            continue
        try:
            out[col] = json.loads(raw)
        except (TypeError, ValueError):
            out[col] = None
    return out


def _encode_machine_field(value: object) -> str | None:
    """Inverse of :func:`_decode_machine` for one column. ``None``
    round-trips; everything else is re-encoded as JSON TEXT for
    sqlite storage."""
    if value is None:
        return None
    if isinstance(value, str):
        # A legacy v3 bundle authored by an earlier v0.33.2 build
        # carried JSON-string columns; accept either shape on import.
        return value
    return json.dumps(value)


def import_bundle(
    state_path: Path,
    src: Path,
    *,
    now: str,
) -> ImportSummary:
    """Load a bundle written by :func:`export_bundle` into
    ``state_path``.

    Machines are inserted as ``boot_mode=bty-inventory`` with empty
    bindings + the transient state reset; existing rows (same mac)
    are replaced. The machine arrives as a freshly-discovered box
    with just its hardware fingerprint pre-filled.

    Raises :class:`BundleVersionMismatch` if the bundle's version
    doesn't match the format this release understands.
    """
    inventory_path = src / "inventory.json"
    if not inventory_path.is_file():
        raise FileNotFoundError(f"no inventory.json in bundle: {src}")
    inventory = json.loads(inventory_path.read_text())
    ver = inventory.get("bty_export_version")
    if ver != _EXPORT_VERSION:
        raise BundleVersionMismatch(
            f"bundle bty_export_version={ver!r}, expected {_EXPORT_VERSION!r}. "
            f"v0.33.2 introduced a metadata-only bundle format; older bundles "
            f"(v1: pre-v0.31.0, v2: v0.31.0..v0.33.1 with image bytes) aren't "
            f"migratable -- regenerate on the source release."
        )

    n_m = 0
    with _db.open_db(state_path) as conn:
        for m in inventory.get("machines", []):
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
                    _encode_machine_field(m.get("known_disks")),
                    m.get("known_disks_at"),
                    _encode_machine_field(m.get("hw_lshw")),
                    m.get("hw_lshw_at"),
                    now,
                    now,
                ),
            )
            n_m += 1
        conn.commit()

    return ImportSummary(n_m)
