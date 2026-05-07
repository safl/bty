# AGENTS.md

This file describes the parts of `bty` that are stable surface for
automated agents (LLM tool-callers, scripts, CI runners). It complements
[`PLAN.md`](PLAN.md) (project roadmap) and the user-facing
[documentation](docs/).

## Scope of stability

- The CLI surface (`bty`, `bty-tui`, `bty-web` console scripts, their
  flags, exit codes, and `--json` output schemas) is stable within a
  given `schema_version`.
- The Python API exposed by `bty` (the modules listed under
  *Reference > Python API* in the docs) is stable within a given
  `bty.__version__` minor release.
- Internal modules (anything starting with `_`, e.g. `bty.tui._app`)
  are not stable and may change without notice.

## What every JSON output looks like

Every `--json` output is wrapped in a stable envelope:

```json
{
  "schema_version": "1",
  "command": "<subcommand-name>",
  ...command-specific fields...
}
```

Agents key off `schema_version` and the per-command keys. The format
does not change without bumping `SCHEMA_VERSION` in `bty.cli`. Any
incompatible structural change increments the version.

### Per-command schemas

`bty list disks --json`

```json
{
  "schema_version": "1",
  "command": "list-disks",
  "disks": [
    {
      "path": "/dev/sda",
      "size": "500G",
      "type": "disk",
      "vendor": "ATA",
      "model": "Samsung SSD 870",
      "serial": "S5SUNG0123456",
      "tran": "sata",
      "removable": false,
      "readonly": false,
      "mountpoints": []
    }
  ]
}
```

`bty list images --json`

```json
{
  "schema_version": "1",
  "command": "list-images",
  "image_root": "/var/lib/bty/images",
  "images": [
    {
      "name": "debian.qcow2",
      "path": "/var/lib/bty/images/debian.qcow2",
      "format": "qcow2",
      "size_bytes": 268435456
    }
  ]
}
```

`bty inspect image PATH --json`

```json
{
  "schema_version": "1",
  "command": "inspect-image",
  "image": {
    "path": "/var/lib/bty/images/debian.qcow2",
    "format": "qcow2",
    "size_bytes": 268435456,
    "detail": { ... format-specific tool output ... }
  }
}
```

`bty flash --dry-run --json`

```json
{
  "schema_version": "1",
  "command": "flash",
  "dry_run": true,
  "ok": false,
  "errors": ["target is not a block device: /dev/null"],
  "plan": {
    "image": { ... },
    "target": { ... },
    "provisioning_mode": "none",
    "notes": []
  }
}
```

### Streaming progress events: `bty flash --progress=ndjson`

When `bty flash --yes` runs, `--progress=ndjson` streams one JSON
object per line on stdout for each lifecycle event. Agents tail the
output and dispatch on the `event` key.

```json
{"event": "started", "total_bytes": 134217728}
{"event": "writing", "note": "qcow2"}
{"event": "synced"}
{"event": "partprobed"}
{"event": "provisioning", "note": "cloud-init"}
{"event": "done"}
```

On any failure during the flash:

```json
{"event": "failed", "note": "target is no longer a block device: /dev/sdX"}
```

Stable event names:

| Event          | Meaning                                                      |
|----------------|--------------------------------------------------------------|
| `started`      | Flash beginning. `total_bytes` is the image's virtual size when known. |
| `writing`      | About to invoke the format-specific writer (`dd` / `zstd \| dd` / `qemu-img convert`). `note` carries the format. |
| `synced`       | Kernel buffers flushed.                                      |
| `partprobed`   | Partition table re-read; flash hardware-complete.            |
| `provisioning` | Provisioning step starting. `note` is `cloud-init` or `cijoe`. |
| `done`         | All steps succeeded. End of stream.                          |
| `failed`       | A step raised an error. `note` carries the error message. End of stream. |

Default mode is `--progress=text` (one human-readable line per event
on stderr); `--progress=none` silences lifecycle output entirely.

The same callback shape (`bty.flash.ProgressCallback` /
`bty.flash.FlashProgress`) drives the bty-tui's flash modal - so the
TUI's UI updates and the CLI's NDJSON stream consume the same event
sequence.

## Exit codes

| Code | Meaning                                                            |
|------|--------------------------------------------------------------------|
| 0    | Success.                                                           |
| 1    | Operation failed (validation rejected the plan; subprocess returned non-zero; cloud-init / cijoe step failed). |
| 2    | Misuse - argparse error, missing required flag, missing input file (e.g. `--user-data` not on disk). |
| 3    | Privilege required - operation needs root, run via `sudo`.         |
| 4    | Required external tool is not installed (e.g. `cijoe` missing).    |
| 5    | Target raced - block device became mounted or disappeared between validation and write. |

Agents should treat `0` as success and any other code as failure. Use
the specific code to decide whether retry is meaningful (e.g. retry
on `5` after re-probing; do not retry on `3` or `4`).

## bty-web HTTP API

`bty-web` exposes a small REST surface backed by SQLite. Documented in
detail in `docs/src/reference.md`; quick reference for agents:

**Auth.** Single-tenant PAM against the bty service user (the OS
account ``bty-web`` runs as; ``bty / bty`` by default on the cooked
appliance). ``POST /auth/login`` issues an opaque session token
whose sha256 is persisted in the ``sessions`` table; protected
routes accept it via ``Authorization: Bearer ...`` (API) or the
``bty-token`` cookie (UI). ``POST /auth/logout`` revokes. Open
routes (no token) are reachable by PXE clients which can't carry
one.

**Routes** (all paths case-insensitive on the MAC; the canonical form
is lower-case `aa:bb:cc:dd:ee:ff`):

| Open | Protected |
|---|---|
| `GET /healthz` | `POST /auth/logout` |
| `GET /version` | `GET /machines` |
| `POST /auth/login` | `GET /machines/{mac}` |
| `GET /pxe/{mac}` | `PUT /machines/{mac}` (body: MachineUpsert) |
| `POST /pxe/{mac}/done` | `DELETE /machines/{mac}` |
| `GET /pxe-bootstrap.ipxe` | `GET /images` |
| `GET /boot/{name}` | `PUT /images/{name}` (stream upload) |
| `GET /images/{name}` | `PUT /boot/{name}` (stream upload) |
| `GET /static/*` | `GET /events/machines` (Server-Sent Events) |

**HTTP status semantics:**
- `200` - success with body
- `204` - success, no body (DELETE)
- `400` - malformed input (e.g. invalid MAC)
- `401` - missing or wrong bearer token
- `404` - protected resource not found (e.g. machine record)
- `422` - request body failed Pydantic validation (e.g. unknown
  `provisioning_mode`)

**Schema versioning.** Wire types are documented inline in
`reference.md`; breaking changes to those shapes will land under a
versioned URL prefix (`/v2/...`). Agents key off field names.

**Auto-discovery.** A `GET /pxe/{mac}` for an unknown MAC creates an
unassigned `Machine` record (`image == null`, `boot_policy == 'local'`)
with `discovered_at` / `last_seen_at` / `last_seen_ip` set, and
returns the "boot from local disk" fallback template. Operators (or
agents) poll `GET /machines` to find newly-discovered MACs and claim
them with `PUT /machines/{mac}`. Subsequent `/pxe` contacts update
`last_seen_at` / `last_seen_ip`; agents can use the freshness of
those fields to detect machines that have stopped reporting.

**Boot policy.** Each machine carries a `boot_policy`:
- `local` (default) - every PXE boot returns the sanboot fallback
  even if an image is assigned. Stable / production stance.
- `flash` - every PXE boot returns the live-env chain (kernel +
  initrd over HTTP, with `bty.{server,mac,image_url,provisioning}`
  cmdline params), so the box reflashes itself every time. Per-job
  CI cadence.

The completion signal `POST /pxe/{mac}/done` updates `last_flashed_at`
but **never modifies `boot_policy`** - flipping back to `local` is an
explicit operator action via `PUT /machines/{mac}` so the per-job CI
cadence survives across reflashes.

**Online cijoe (milestone 15).** A machine with
`provisioning_mode='cijoe-online'` and a `cijoe_workflow_ref` gets
its workflow run from bty-web automatically when the live env signals
flash completion. bty-web spawns a daemon thread that ``cijoe
<workflow.yaml> --config <transport.toml>``s with an SSH transport
pointing at `last_seen_ip`; cijoe's transport-retry handles waiting
for SSH to come up. Workflow status (`running` / `success` /
`failed`) is recorded on the machine as `last_workflow_status` and
fans out via the SSE machines-update channel as it changes. Per-run
output dirs accumulate under `/var/lib/bty/workflows/<mac>/<run-id>/`
(holds `transport.toml`, `cijoe.stdout`, `cijoe.stderr`, and cijoe's
own `cijoe-output/`). Operator drops the SSH key at
`/var/lib/bty/keys/id_ed25519` (key generation lands in a future
phase).

**Live updates.** `GET /events/machines` is a Server-Sent Events
stream (auth: same Bearer/cookie dep). Subscribers receive an initial
`machines-update` event with the rendered `<tbody>` snapshot, then
one fresh `machines-update` event after every mutation
(`PUT`/`DELETE` and PXE auto-discovery). Used by the browser UI to
avoid polling. Agents can subscribe instead of polling
`GET /machines`, but the canonical state remains the JSON API - the
event payload is HTML for browser consumption.

**Offline-friendly.** All client-side assets (Bootstrap CSS, HTMX,
HTMX SSE extension) are vendored in the wheel and served from
`/static/`. The bty appliance does **not** contact any CDN at
runtime; agents and PXE clients can run on air-gapped networks.

**Single-worker requirement.** The SSE bus is in-process; run
`uvicorn` with one worker (the default). Multi-worker would need a
real broker (Redis pub/sub, NATS), which is out of scope.

**PXE boot stack (server image).** `bty-media`'s server variant
ships dnsmasq + the iPXE BIOS/UEFI binaries. TFTP serves
`undionly.kpxe` and `ipxe.efi` from `/var/lib/tftpboot/`. The
proxy-DHCP and chain-config block in
`/etc/dnsmasq.d/bty-pxe.conf` is **commented out by default** so a
freshly-imaged appliance never disrupts an existing DHCP server;
operators activate via the `/ui/settings` page (which writes
`/etc/dnsmasq.d/bty-pxe-active.conf` and restarts dnsmasq via the
sudoers-permitted `bty-web-activate-pxe` helper). Two-stage chain:
PXE ROM -> `undionly.kpxe`/`ipxe.efi` -> bty-web's
`/pxe-bootstrap.ipxe` -> per-MAC `/pxe/{mac}` plan.

**Settings (`/ui/settings`).** Operator-facing controls for two
runtime concerns: session revocation (`POST /ui/settings/revoke-
sessions` truncates the `sessions` table so every active CLI
token and browser cookie is invalidated; the credential itself is
rotated out-of-band with `sudo passwd bty` on the appliance) and
PXE activation (writes `/etc/dnsmasq.d/bty-pxe-active.conf` +
restarts dnsmasq via `bty-web-activate-pxe`). The PXE helper
lives in `/usr/local/sbin/` and is invocable by user `bty` via
the `/etc/sudoers.d/bty-web` NOPASSWD entry - the only privileged
operation bty-web is granted.

## Conventions agents can rely on

- **No interactive prompts.** Destructive operations require `--yes`.
  Validation-only runs require `--dry-run`. Without one of those flags
  `bty flash` exits 2.
- **stderr for human-readable errors and notes; stdout for results
  (text or JSON).**
- **`bty --version`** prints `bty <version>` (sourced from package
  metadata) and exits 0.
- **`bty --help`** and `bty <subcommand> --help` document the surface;
  argparse's standard help output.
- **Idempotent reads.** `bty list ...` and `bty inspect ...` have no
  side effects; safe to call repeatedly.

## Don'ts

- Don't parse human-readable table output. Use `--json`.
- Don't depend on stderr message wording - only on exit codes.
- Don't depend on internal module paths (`bty.tui._app`,
  `bty.flash._partition_has_cloud_init`, etc.). They are private.
- Don't expect bty to write files outside the configured image root,
  the target block device, or the bty configuration / state areas.

## Where to look next

- [`PLAN.md`](PLAN.md) - roadmap, motivation, OS scope.
- [`docs/src/reference.md`](docs/src/reference.md) - full CLI
  reference and configuration.
- [`docs/src/quickstart.md`](docs/src/quickstart.md) - operator
  walk-through.
