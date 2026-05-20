"""Pydantic models for the bty-web HTTP API.

These describe the wire format. Persistence rows in :mod:`bty.web._db`
are decoded into / encoded from these models on the boundary.
"""

from __future__ import annotations

import re
from datetime import datetime

from pydantic import BaseModel, Field


def _enum_pattern(values: tuple[str, ...]) -> str:
    """Build a Pydantic ``pattern=`` regex matching exactly the given
    string set. Auto-derived from the tuple so a new value can be
    added in one place without the regex drifting out of sync."""
    return "^(" + "|".join(re.escape(v) for v in values) + ")$"


# Canonical lower-case ``aa:bb:cc:dd:ee:ff`` MAC address.
MAC_PATTERN = r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$"

# Boot-policy values: what ``GET /pxe/{mac}`` returns.
#
# - ``local`` emits iPXE ``exit``: control returns to the firmware,
#   which boots the next device in its BIOS/UEFI boot order. Use when
#   STORAGE sits after PXE in the firmware boot order. This is the
#   explicit-PUT default for assigned machines.
# - ``sanboot`` emits ``sanboot --drive <sanboot_drive> || exit``: iPXE
#   boots the local disk itself (default ``0x80`` = first BIOS disk),
#   falling back to ``exit`` (firmware order) if it can't. Use when
#   you'd rather bty drive the local boot than depend on the firmware
#   order. The drive is a per-machine override (``sanboot_drive``);
#   iPXE selects by BIOS drive number, not by Linux serial.
# - ``flash`` returns the live-env chain so the box re-flashes itself
#   on every PXE boot (per-job CI cadence).
# - ``flash-once`` returns the live-env flash chain just like ``flash``,
#   but the completion signal (``POST /pxe/{mac}/done``) flips the
#   policy to the configured settle policy (``flash.settle_policy``,
#   default ``local``) so the box doesn't re-flash on the next boot.
#   For "I want this machine reimaged now, then leave it alone" --
#   distinct from ``flash`` which is "reimage on every PXE boot".
# - ``tui`` returns the live-env chain. ``bty`` on tty1 GETs
#   /pxe/<mac>/plan and (for boot_policy=tui) drops the operator
#   into the wizard so they can pick an image from the server's
#   catalog by hand. This is the auto-discovery default for
#   unknown MACs that PXE-boot through the server.
#
# Completion signal (``POST /pxe/{mac}/done``) updates
# ``last_flashed_at`` regardless of policy; it only mutates
# ``boot_policy`` for ``flash-once`` (-> the settle policy). ``flash``
# stays ``flash`` so the per-job CI cadence reflashes every boot.
BOOT_POLICIES = ("local", "sanboot", "flash", "flash-once", "tui")

# iPXE BIOS drive selector the ``sanboot`` policy boots: ``0x80`` is the
# first disk, ``0x81`` the second, and so on. The sensible default; a
# per-machine ``sanboot_drive`` overrides it.
SANBOOT_DRIVE_PATTERN = r"^0x[0-9a-fA-F]{1,2}$"
DEFAULT_SANBOOT_DRIVE = "0x80"
BOOT_POLICY_PATTERN = _enum_pattern(BOOT_POLICIES)
DEFAULT_BOOT_POLICY = BOOT_POLICIES[0]


class MachineUpsert(BaseModel):
    """Request body for ``PUT /machines/{mac}``.

    All fields are optional except for the implicit ``mac`` from the
    path; ``boot_policy`` defaults to ``"local"``. Binding targets
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
    # RFC-1123-ish: each dot-separated label is alnum, hyphen-
    # internal-only (no leading / trailing / bare hyphen, no
    # consecutive dots). ``max_length=253`` matches DNS.
    hostname: str | None = Field(
        default=None,
        min_length=1,
        max_length=253,
        pattern=r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$",
    )
    boot_policy: str = Field(default=DEFAULT_BOOT_POLICY, pattern=BOOT_POLICY_PATTERN)
    # iPXE BIOS drive the ``sanboot`` policy boots (``0x80`` first disk,
    # ``0x81`` second, ...). ``None`` = use the default (``0x80``);
    # ignored for non-sanboot policies. Distinct from
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


class Machine(BaseModel):
    """A persisted machine record as returned by the API.

    A machine without ``bty_image_ref`` set is *discovered* but
    unassigned - bty-web saw it via ``GET /pxe/{mac}`` and recorded
    it so the operator can claim it. Once the operator ``PUT``s an
    assignment, the machine is *assigned*.
    """

    mac: str = Field(..., pattern=MAC_PATTERN)
    bty_image_ref: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    hostname: str | None = None
    # Set the first time bty-web sees a ``GET /pxe/{mac}`` for this MAC.
    # ``None`` for machines that were created via ``PUT`` and have not
    # yet PXE-booted through bty-web.
    discovered_at: datetime | None = None
    # Updated on every ``GET /pxe/{mac}``.
    last_seen_at: datetime | None = None
    last_seen_ip: str | None = None
    boot_policy: str = Field(default=DEFAULT_BOOT_POLICY, pattern=BOOT_POLICY_PATTERN)
    # iPXE BIOS drive the ``sanboot`` policy boots; ``None`` = default
    # (``0x80``). See ``MachineUpsert.sanboot_drive``.
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
    * Manifest entry that has been fetched + cached -> same as
      above, ``url`` points at the bty-web server.
    * Manifest entry not yet cached -> ``url`` is the upstream
      manifest ``src``; the client streams bytes directly from
      upstream during flash. The operator can hit "Fetch" in the
      browser UI to populate the cache, after which the entry's
      ``url`` flips to the server form on the next listing.
    * Dir-scan file without a sidecar AND no manifest entry ->
      excluded from the listing until bty-web's auto-import
      computes its SHA in the background. The HashManager picks
      these up at startup with a single worker so a Pi 4 does
      not get hammered.

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
    cached: bool = False


class HealthResponse(BaseModel):
    """``GET /healthz`` response body. Liveness check for monitoring
    / smoke tests / Kubernetes-style probes. Always ``{"status":
    "ok"}`` when the worker is up; the absence of a 200 response
    is the signal, not the body content."""

    status: str = "ok"


class VersionResponse(BaseModel):
    """``GET /version`` response body. Carries the running bty-lab
    package version so operators (and CI smoke tests) can verify
    which release the appliance has installed without ssh'ing in
    to read ``pip show``."""

    version: str


class CatalogEnqueueRequest(BaseModel):
    """POST /catalog/downloads body: enqueue a manifest entry by name.

    Also reused for ``POST /catalog/hashes`` (HashManager enqueues
    by basename). Both managers reject path-traversal characters at
    the manager boundary; the Pydantic pattern here mirrors that
    rule so a malformed name surfaces as a clean 422 instead of a
    500 from the manager's ``ValueError``.

    Pattern: any non-empty string that does NOT contain ``/``,
    ``\\``, or NUL. The manager additionally rejects bare ``.``
    and ``..`` (pydantic-core's regex engine does not support
    lookahead, so layering both checks is cleaner than cramming
    the negative match into a single regex).
    """

    model_config = {"extra": "forbid"}

    name: str = Field(
        ...,
        description="image name as declared in the manifest",
        min_length=1,
        pattern=r"^[^/\\\x00]+$",
    )


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


class CatalogEntryAdd(BaseModel):
    """``POST /catalog/entries`` body: add an operator-curated
    catalog entry by URL.

    ``image_url`` is required: the upstream URL the bytes live at.
    Accepts ``http://``, ``https://``, and ``oras://`` schemes.

    - For ``http(s)://`` URLs, ``sha_url`` is optional: if given,
      server fetches and parses the sha256-manifest body, picks the
      digest matching ``image_url``'s filename, and stores it as
      ``disk_image_sha``. Without it the entry is URL-only
      (``disk_image_sha`` stays NULL until the first flash's
      cache-through observes it); still bindable to a machine
      because binding targets ``bty_image_ref``, not the content sha.
    - For ``oras://`` URLs the server resolves the OCI manifest at
      add time, picks the disk-image layer, and uses the layer's
      content-addressed digest as the entry's ``disk_image_sha``.
      ``sha_url`` is ignored (the manifest is authoritative).

    Both URLs must carry a host segment; arbitrary schemes and
    host-less inputs like ``https://?`` are rejected at validation
    time so a typo doesn't land an entry that can never be flashed.

    The host pattern (``[^\\s/?#]+``) requires at least one
    non-separator char before any path / query / fragment, which
    is the same rule WHATWG URL parsing applies to the host
    component.
    """

    model_config = {"extra": "forbid"}

    # http(s) or oras:// scheme; both require a host component.
    # The oras alternative also requires at least one ``/`` in the
    # path (host/owner/repo) since GHCR-style refs need owner+repo
    # under the host. The CLI / API entry point later runs
    # ``bty.oras.parse_ref`` for stricter shape validation.
    image_url: str = Field(
        ...,
        pattern=r"^(?:https?://[^\s/?#]+(?:[/?#]\S*)?|oras://[^\s/?#]+/\S+)$",
    )
    sha_url: str | None = Field(default=None, pattern=r"^https?://[^\s/?#]+(?:[/?#]\S*)?$")
    # Optional client-supplied ref. When set, the server recomputes
    # ``image_ref_for_src(image_url)`` and rejects with 422 if it
    # doesn't match. Trust-but-verify: any client that read an
    # entry's ref from /images and is round-tripping it back here
    # is confirming "yes, this is the entry I think it is" -- a
    # mismatch means our canonicalisation differs from theirs or
    # the data was tampered with mid-flight. Always-derivable so
    # not required; omit to let the server compute fresh.
    ref: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    def verify_ref(self) -> None:
        """Recompute the canonical ref from ``image_url`` and raise
        ``ValueError`` if the inbound ``ref`` (if any) doesn't
        match.

        Called by the endpoint after Pydantic validation; raising
        ``ValueError`` lets the endpoint translate to a clean 422
        with operator-actionable detail (the computed ref + the
        supplied one).
        """
        if self.ref is None:
            return
        from bty.catalog import image_ref_for_src

        expected = image_ref_for_src(self.image_url)
        if self.ref != expected:
            raise ValueError(
                f"ref mismatch: supplied {self.ref!r} but "
                f"image_ref_for_src({self.image_url!r}) = {expected!r}. "
                "The ref must equal sha256(canonicalise_src(image_url))."
            )
