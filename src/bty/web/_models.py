"""Pydantic models for the bty-web HTTP API.

These describe the wire format. Persistence rows in :mod:`bty.web._db`
are decoded into / encoded from these models on the boundary.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

# Canonical lower-case ``aa:bb:cc:dd:ee:ff`` MAC address.
MAC_PATTERN = r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$"

# Provisioning modes accepted by ``bty flash`` / the API.
# - ``none``         - boot the cooked image as-is.
# - ``cloud-init``   - drop user-data into the seed; OS picks it up on first boot.
# - ``cijoe``        - offline workflow run from the live env after flash.
# - ``cijoe-online`` - online workflow run from bty-web after target boots
#                      (milestone 15). Triggered by POST /pxe/{mac}/done; cijoe's
#                      transport-retry handles waiting for SSH to come up.
PROVISIONING_MODES = ("none", "cloud-init", "cijoe", "cijoe-online")
PROVISIONING_PATTERN = r"^(none|cloud-init|cijoe|cijoe-online)$"

# Status of the most recent online-cijoe workflow run.
WORKFLOW_STATUSES = ("running", "success", "failed")
WORKFLOW_STATUS_PATTERN = r"^(running|success|failed)$"

# Boot-policy values: what ``GET /pxe/{mac}`` returns for an assigned
# machine. ``local`` (the default) returns sanboot - the box boots
# whatever is on its disk. ``flash`` returns the live-env chain so the
# box re-flashes itself on every PXE boot (per-job CI cadence).
# Decoupled from the completion signal: ``POST /pxe/{mac}/done`` updates
# ``last_flashed_at`` regardless of policy and never flips the policy.
BOOT_POLICIES = ("local", "flash")
BOOT_POLICY_PATTERN = r"^(local|flash)$"


class MachineUpsert(BaseModel):
    """Request body for ``PUT /machines/{mac}``.

    All fields are optional except for the implicit ``mac`` from the
    path; ``provisioning_mode`` defaults to ``"none"`` and
    ``boot_policy`` defaults to ``"local"``.
    """

    image: str | None = None
    provisioning_mode: str = Field(default="none", pattern=PROVISIONING_PATTERN)
    hostname: str | None = None
    cijoe_workflow_ref: str | None = None
    boot_policy: str = Field(default="local", pattern=BOOT_POLICY_PATTERN)


class Machine(BaseModel):
    """A persisted machine record as returned by the API.

    A machine without an ``image`` set is *discovered* but unassigned -
    bty-web saw it via ``GET /pxe/{mac}`` and recorded it so the
    operator can claim it. Once the operator ``PUT``s an assignment,
    the machine is *assigned*.
    """

    mac: str = Field(..., pattern=MAC_PATTERN)
    image: str | None = None
    provisioning_mode: str = Field(default="none", pattern=PROVISIONING_PATTERN)
    hostname: str | None = None
    cijoe_workflow_ref: str | None = None
    last_known_good: dict[str, Any] | None = None
    # Set the first time bty-web sees a ``GET /pxe/{mac}`` for this MAC.
    # ``None`` for machines that were created via ``PUT`` and have not
    # yet PXE-booted through bty-web.
    discovered_at: datetime | None = None
    # Updated on every ``GET /pxe/{mac}``.
    last_seen_at: datetime | None = None
    last_seen_ip: str | None = None
    boot_policy: str = Field(default="local", pattern=BOOT_POLICY_PATTERN)
    last_flashed_at: datetime | None = None
    last_workflow_run_at: datetime | None = None
    last_workflow_status: str | None = Field(default=None, pattern=WORKFLOW_STATUS_PATTERN)
    last_workflow_output_path: str | None = None
    created_at: datetime
    updated_at: datetime


class ImageEntry(BaseModel):
    """A discovered image in the server-side image catalog."""

    name: str
    path: str
    format: str
    size_bytes: int


class HealthResponse(BaseModel):
    status: str = "ok"


class VersionResponse(BaseModel):
    version: str
