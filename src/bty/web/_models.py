"""Pydantic models for the bty-web HTTP API.

These describe the wire format. Persistence rows in :mod:`bty.web._db`
are decoded into / encoded from these models on the boundary.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field, StringConstraints


def _enum_pattern(values: tuple[str, ...]) -> str:
    """Build a Pydantic ``pattern=`` regex matching exactly the given
    string set. Auto-derived from the tuple so a new value can be
    added in one place without the regex drifting out of sync."""
    return "^(" + "|".join(re.escape(v) for v in values) + ")$"


# Canonical lower-case ``aa:bb:cc:dd:ee:ff`` MAC address.
MAC_PATTERN = r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$"

# Boot-mode values: what ``GET /pxe/{mac}`` returns.
#
# Since v0.25.0, ``boot_mode`` is a stable per-machine policy that is
# never mutated by the completion signal; the flash-once versus
# flash-always distinction is derived from ``saw_flasher_boot`` +
# ``last_flashed_at`` at plan-emit time. See ``_app.py`` for the state
# machine.
#
# - ``ipxe-exit`` boots the local disk, firmware-aware: on UEFI it
#   ``exit``s to the firmware boot order (which boots the disk's EFI
#   loader -- UEFI has no BIOS INT13 drive map for ``sanboot --drive``);
#   on legacy BIOS it emits ``sanboot --drive <sanboot_drive>`` (default
#   ``0x80`` = first BIOS disk) with ``|| exit`` as the firmware-order
#   fallback. This is the way bty boots an already-provisioned machine,
#   and the explicit-PUT default. The drive is a per-machine override
#   (``sanboot_drive``); iPXE selects by BIOS drive number, not by
#   Linux serial. (There is no separate ``local`` policy: a bare
#   ``exit`` is just ``ipxe-exit``'s fallback, and the no-assignment /
#   error paths emit it internally.) The "sanboot" name in the iPXE
#   verb and in ``sanboot_drive`` reflects the underlying iPXE command;
#   the boot-mode value was renamed to ``ipxe-exit`` in v0.25.0 so it
#   describes what the mode does rather than which iPXE verb it emits.
# - ``bty-flash-always`` returns the live-env chain so the box
#   re-flashes itself, then boots the just-flashed disk (via
#   ``ipxe-exit`` semantics) before the next reflash (per-job CI
#   cadence; see ``saw_flasher_boot``).
# - ``bty-flash-once`` returns the live-env flash chain just like
#   ``bty-flash-always``, but the completion signal
#   (``POST /pxe/{mac}/done``) sets ``saw_flasher_boot`` +
#   ``last_flashed_at`` so subsequent plan-emits observe the machine
#   as flashed and return the ``ipxe-exit`` chain instead. The
#   ``boot_mode`` value itself stays ``bty-flash-once`` and the
#   next explicit reflash request (bty-web UI or PUT) is what flips
#   the machine back onto the live env.
# - ``bty-tui`` returns the live-env chain. ``bty`` on tty1 GETs
#   /pxe/<mac>/plan and (for boot_mode=bty-tui) drops the operator
#   into the wizard so they can pick an image from the server's
#   catalog by hand.
# - ``bty-inventory`` is to inventory what ``bty-flash-always`` is
#   to flashing: it alternates an inventory boot then a disk boot
#   across PXE contacts (same ``saw_flasher_boot`` mechanism). The
#   active boot chains the live env in the ``inventory`` plan mode --
#   ``bty`` posts /pxe/<mac>/inventory and reboots (no flash, no
#   wizard) -- and the next contact returns the ``ipxe-exit`` chain.
#   So every power cycle re-collects the disk inventory before booting,
#   surfacing swapped hardware. This is the auto-discovery default for
#   unknown MACs: a new box self-reports its disks and then just boots,
#   ready for the operator to assign a flash policy from the now-
#   populated inventory.
# - ``ramboot`` mounts the bound catalog image over NBD (served by
#   the sidecar ``nbdmux`` daemon) and pivots into it with
#   overlayfs over tmpfs for writes. The disk is never touched.
#   Useful for CI / preview runs where flashing is overkill; the
#   target box runs the OS in place and the overlay vanishes on
#   reboot. Bytes path is gated on the operator having configured
#   an nbdmux URL in Settings -> Ramboot AND the bound image having
#   been pre-warmed (decompressed and registered as an NBD export).
#   /pxe/{mac}/plan rejects with mode=interactive + a reason when
#   either gate is open.
#
# The ``bty-*`` prefix marks the policies that PXE-boot into bty's own
# live env; ``ipxe-exit`` and ``ramboot`` do not chain the live env.
#
# Completion signal (``POST /pxe/{mac}/done``) updates
# ``last_flashed_at`` and ``saw_flasher_boot`` regardless of policy;
# it does not mutate ``boot_mode``.
BOOT_MODES = (
    "ipxe-exit",
    "bty-flash-always",
    "bty-flash-once",
    "bty-tui",
    "bty-inventory",
    "ramboot",
)

# iPXE BIOS drive selector the ``sanboot`` policy boots: ``0x80`` is the
# first disk, ``0x81`` the second, and so on. The sensible default; a
# per-machine ``sanboot_drive`` overrides it.
SANBOOT_DRIVE_PATTERN = r"^0x[0-9a-fA-F]{1,2}$"
DEFAULT_SANBOOT_DRIVE = "0x80"
BOOT_MODE_PATTERN = _enum_pattern(BOOT_MODES)
DEFAULT_BOOT_MODE = BOOT_MODES[0]

# Per-label shape: alnum-leading then alnum / space / hyphen / underscore
# / dot, up to 64 chars. Stricter than "anything goes" (a blank or
# whitespace-only label would surface as a meaningless empty chip)
# but loose enough for the labels operators actually type ("rack-3",
# "the loud one", "GMKTec G10", "lab.fedora.01").
LABEL_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9 ._-]*$"
LABEL_MAX_LENGTH = 64
LabelStr = Annotated[
    str,
    StringConstraints(min_length=1, max_length=LABEL_MAX_LENGTH, pattern=LABEL_PATTERN),
]

# Cap on labels per machine: high enough that an operator who wants
# to dump role + rack + vendor + a couple of ad-hoc notes never hits
# it, low enough that a forgotten comma in a 200-tag paste bounces
# loudly rather than landing.
MAX_LABELS_PER_MACHINE = 16


class MachineUpsert(BaseModel):
    """Request body for ``PUT /machines/{mac}``.

    All fields are optional except for the implicit ``mac`` from the
    path; ``boot_mode`` defaults to ``DEFAULT_BOOT_MODE`` (currently
    ``"ipxe-exit"``, the disk-boot policy). Binding targets
    ``bty_image_ref`` (a stable provenance ID derived as
    ``sha256(canonicalise_src(src))``) rather than the content sha,
    so URL-only and rolling-tag oras entries are bindable; rename or
    content drift leaves the binding intact.

    ``extra="forbid"`` so unknown fields fail loud at the edge with
    a 422 -- silent drops let a typo put a row with
    ``bty_image_ref=NULL`` and the next PXE chain fall through to
    "no assignment", which is a debugging trap.
    """

    model_config = {"extra": "forbid"}

    # 64 lower-case hex chars; ``None`` = discovered-but-unassigned.
    # Same hex shape as a sha256 because the ref IS a sha256 (of the
    # canonicalised src URL, not of content).
    bty_image_ref: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    # Free-form display labels (replaced the singular ``hostname``
    # in v0.58.0). A box can carry several at once ("rack-3",
    # "noisy", "gmktec-g10") so filtering or searching by any tag
    # surfaces every box that wears it. Each label is alnum-leading
    # + alnum/space/``-``/``_``/``.``, max 64 chars; empty strings
    # rejected per-item. Cap at 16 per machine -- well past the
    # operator-meaningful count, low enough that a typo (e.g. a
    # missing comma in a 200-tag dump) bounces with a 422 instead
    # of landing.
    labels: list[LabelStr] = Field(default_factory=list, max_length=MAX_LABELS_PER_MACHINE)
    boot_mode: str = Field(default=DEFAULT_BOOT_MODE, pattern=BOOT_MODE_PATTERN)
    # iPXE BIOS drive the ``ipxe-exit`` mode sanboots on legacy BIOS
    # (``0x80`` first disk, ``0x81`` second, ...). ``None`` = the default
    # (``0x80``); ignored on UEFI + by non-ipxe-exit modes. Distinct from
    # ``target_disk_serial`` -- iPXE selects local disks by BIOS drive
    # number, not by the Linux serial the flash step matches on.
    sanboot_drive: str | None = Field(default=None, pattern=SANBOOT_DRIVE_PATTERN)
    # Operator-selected target disk serial. ``bty`` in auto-flash
    # mode matches the plan's ``target_disk_serial`` against the
    # SCSI / NVMe / SATA serial of the disk it sees at boot
    # time, refusing to flash if the serial isn't found among
    # current disks. Serial (vs path) is the durable identifier:
    # ``/dev/sda`` can flip across kernel versions, but the
    # disk's serial number is stable. Free-form string because
    # vendor formats vary wildly (NVMe nguid, SATA wwn,
    # spinning-rust ATA serial); the only constraint is
    # "what lsblk -o SERIAL emits". The /ui/machines/{mac}
    # dropdown is populated from the most recent inventory
    # post, so typos are not an exposed failure mode.
    target_disk_serial: str | None = Field(default=None, max_length=128)


class InventoryDisk(BaseModel):
    """One block device as reported by ``bty.disks.list_disks``.

    Shape mirrors the dict that helper returns; Pydantic at the
    boundary catches a future drift between ``bty`` and bty-web.
    """

    model_config = {"extra": "ignore"}

    # /dev path at inventory time. Operator UI displays this
    # alongside model/serial for readability; not the durable
    # identifier (use ``serial`` instead).
    path: str = Field(..., max_length=255)
    # lsblk's human-readable size string ("500G" / "1.5T") --
    # kept as text because operators recognise it.
    size: str | None = Field(default=None, max_length=32)
    vendor: str | None = Field(default=None, max_length=64)
    model: str | None = Field(default=None, max_length=128)
    # The durable identifier. Used as the match key at flash time.
    serial: str | None = Field(default=None, max_length=128)
    # Transport ("sata"/"nvme"/"usb"/...). Hints "is this the
    # external drive plugged in by the operator or the internal
    # boot disk" in the UI.
    tran: str | None = Field(default=None, max_length=32)
    removable: bool = False
    readonly: bool = False


class InventoryPost(BaseModel):
    """Body of ``POST /pxe/{mac}/inventory``.

    Open endpoint (live env's ``bty`` has no token). Trust model
    matches the rest of ``/pxe/*``: bty-web is for trusted networks.
    """

    model_config = {"extra": "forbid"}

    disks: list[InventoryDisk] = Field(default_factory=list, max_length=64)
    # Optional full ``lshw -json`` hardware tree (CPU / RAM / NICs +
    # MACs / peripherals / firmware). Supplementary to ``disks`` -- the
    # flasher only consumes ``disks`` (lsblk), never this. Stored as a
    # blob and surfaced on the Machine view + raw download. ``lshw -json``
    # is usually an object; some versions emit a top-level list, so
    # accept either. Size is capped server-side when stored.
    lshw: dict[str, object] | list[object] | None = None


class Machine(BaseModel):
    """A persisted machine record as returned by the API.

    A machine without ``bty_image_ref`` set is *discovered* but
    unassigned - bty-web saw it via ``GET /pxe/{mac}`` and recorded
    it so the operator can claim it. Once the operator ``PUT``s an
    assignment, the machine is *assigned*.
    """

    mac: str = Field(..., pattern=MAC_PATTERN)
    bty_image_ref: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    labels: list[str] = Field(default_factory=list)
    # Set the first time bty-web sees a ``GET /pxe/{mac}`` for this MAC.
    # ``None`` for machines that were created via ``PUT`` and have not
    # yet PXE-booted through bty-web.
    discovered_at: datetime | None = None
    # Updated on every ``GET /pxe/{mac}``.
    last_seen_at: datetime | None = None
    last_seen_ip: str | None = None
    boot_mode: str = Field(default=DEFAULT_BOOT_MODE, pattern=BOOT_MODE_PATTERN)
    # iPXE BIOS drive the ``ipxe-exit`` policy boots on legacy BIOS;
    # ``None`` = default (``0x80``). See ``MachineUpsert.sanboot_drive``.
    sanboot_drive: str | None = Field(default=None, pattern=SANBOOT_DRIVE_PATTERN)
    last_flashed_at: datetime | None = None
    # JSON-decoded inventory from the most recent
    # ``POST /pxe/{mac}/inventory``. ``None`` means ``bty`` has
    # never reported in for this machine yet.
    known_disks: list[dict[str, object]] | None = None
    known_disks_at: datetime | None = None
    target_disk_serial: str | None = None
    created_at: datetime
    updated_at: datetime


class ImageEntry(BaseModel):
    """One image in the server-side image catalog.

    Each entry carries a single ``url`` -- the place a client
    should fetch the bytes from. The server resolves it based on
    cache state so the client (e.g. ``bty --catalog URL``)
    does not need to know about catalog manifests, sidecars, or
    cache layout: it just flashes from ``url``.

    Resolution rule:

    * Dir-scan file with ``.sha256`` sidecar (or auto-imported)
      -> ``url`` points at the bty-web server (``/images/<sha>``);
      the server serves the bytes.
    bty-web does not host image bytes. Every entry's
    ``url`` is either the upstream origin URL (https:// or
    oras://) or a withcache ``/b/<token>/`` URL when the plan
    endpoint resolves the entry's bytes to a warm cache. The
    live env flashes from ``url`` directly.

    ``sha_short`` is a 12-char prefix of the entry's content sha
    (``disk_image_sha``) for display only -- used by the browser
    UI to disambiguate same-named entries. Distinct from
    ``bty_image_ref`` (the binding key); operators do not paste
    it anywhere.
    """

    name: str
    format: str
    size_bytes: int
    url: str
    # Stable provenance id (``sha256(canonicalise_src(src))``).
    # Always derivable from ``src``, surfaced here so external
    # scripts can bind a machine to this entry without recomputing
    # the canonicalisation themselves. The PUT /machines/<mac>
    # binding key (``bty_image_ref``) takes exactly this value;
    # the bare ``ref`` name is used in the image-context (where
    # it's obviously the image's ref) to keep the field tight.
    ref: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    sha_short: str | None = None
    # Informational architecture hint (``x86_64`` / ``arm64`` / ...);
    # operator-facing display only.
    arch: str | None = None


class HealthResponse(BaseModel):
    """``GET /healthz`` response body. Liveness check for monitoring
    / smoke tests / Kubernetes-style probes. Always ``{"status":
    "ok"}`` when the worker is up; the absence of a 200 response
    is the signal, not the body content."""

    status: str = "ok"


class VersionResponse(BaseModel):
    """``GET /version`` response body. Carries the running bty-lab
    package version so operators (and CI smoke tests) can verify
    which release the bty-web server has installed without ssh'ing in
    to read ``pip show``."""

    version: str


class ReleaseFetchRequest(BaseModel):
    """``POST /boot/releases`` body: enqueue a release-fetch job
    by tag. ``"latest"`` resolves via GitHub's
    ``releases/latest/download`` redirect; explicit tags use
    ``releases/download/<tag>/`` directly. Tag must be non-empty
    + a reasonable shape: tag identifiers in GitHub release
    URLs are URL-path segments and shouldn't carry slashes.
    """

    model_config = {"extra": "forbid"}

    tag: str = Field(default="latest", min_length=1, pattern=r"^[A-Za-z0-9._-]+$")


class PxeStatus(BaseModel):
    """``POST /pxe/{mac}/status`` body: the live env's terminal flash
    signal. ``status="done"`` records a successful flash (last_flashed_at +
    a ``machine.flashed`` event); ``status="failed"`` records a failure with
    an optional ``reason`` (a ``machine.flash_failed`` event) so the operator
    sees it on the timeline instead of the box sitting at "awaiting flash".
    One endpoint, the body picks the outcome.
    """

    model_config = {"extra": "forbid"}

    status: Literal["done", "failed"]
    reason: str = Field(default="", max_length=500)


class BackupEnqueueRequest(BaseModel):
    """``POST /workers/backups`` body: enqueue a backup job.

    ``trigger`` distinguishes operator-pressed "Back up now" runs
    (``manual``) from the scheduler-loop's cadence-driven runs
    (``scheduled``); only scheduled runs update the cadence anchor
    (``backup.last_run_at``). The UI form only ever POSTs
    ``manual``; the scheduler calls :meth:`BackupManager.enqueue`
    directly and bypasses this model.
    """

    model_config = {"extra": "forbid"}

    trigger: str = Field(default="manual", pattern=r"^(manual|scheduled)$")
