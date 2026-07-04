# AGENTS.md

This file describes the parts of `bty` that are stable surface for
automated agents (LLM tool-callers, scripts, CI runners). It
complements [`PLAN.md`](PLAN.md) (project roadmap) and the
user-facing [documentation](docs/).

## Scope of stability

- The **bty-web HTTP API** (paths, request bodies, response shapes,
  status codes) is the primary agent surface. Documented in detail
  in `docs/src/reference.md`; quick reference below.
- The **`bty` console script** is the operator-facing wizard. Its
  cmdline surface is intentionally narrow:
  - `bty` -- interactive wizard, local image-root only.
  - `bty --catalog URL` -- interactive wizard with the catalog pre-
    loaded.
  - `bty --server X --mac Y` -- server-driven: GETs
    `<X>/pxe/<Y>/plan` and dispatches (auto-flash / interactive /
    no-op). This is the scripted-flash path agents should use to
    drive a target.
  - `bty --version` prints `bty <version>` and exits 0.
  - `bty --help` documents the surface.
- The **Python API** exposed by `bty` (`bty.disks`, `bty.images`,
  `bty.flash`, `bty.catalog`) is stable within a given
  `bty.__version__` minor release. Use these for in-process
  scripting if the HTTP API doesn't fit your use case. The
  oras / OCI helpers live in `withcache.oras` (they moved out of
  bty in v0.59.0 when withcache took over the OCI dance).
- Internal modules (anything starting with `_`, e.g. `bty.tui._app`,
  `bty.web._app`) are not stable and may change without notice.

## Driving a flash from an agent

The end-to-end contract for "make MAC X receive image Y":

1. **Server-side bind** (auth-gated, `Cookie: bty-token=<session>`):

   ```
   PUT /machines/{mac}
   Content-Type: application/json
   { "bty_image_ref":     "<64-hex>",
     "boot_mode":         "bty-flash-always" | "bty-flash-once",
     "target_disk_serial": "<lsblk SERIAL>"  }
   ```

   - `bty_image_ref` is the stable provenance ID:
     `sha256(canonicalise_src(catalog_entry.src))`. List entries
     via `GET /images` (open route, returns JSON) or
     `GET /catalog.toml` (TOML mirror).
   - `target_disk_serial` is the operator's pick from the most
     recent inventory post (`GET /machines/{mac}.known_disks`).
     The match happens at flash time against `lsblk -o SERIAL` so
     a swapped drive refuses rather than risks the wrong target.

2. **Target-side trigger.** Either:
   - **Power-cycle**: the target's firmware PXE-DHCPs, fetches the
     iPXE chain at `/pxe/<mac>`, chains into the live env. The
     live env's `bty-on-tty1.service` exec's `bty --server X
     --mac Y` with the values rendered into the cmdline by the
     server's iPXE template.
   - **Hand-launched** (target already running): run `bty --server
     X --mac Y` directly. Same dispatch.

3. **`bty` GETs `/pxe/<mac>/plan`** (open route, returns JSON):

   ```json
   { "mode": "auto",
     "image": "http://X/images/<ref>/<name>",
     "target_disk_serial": "<serial>" }
   ```

   `mode=auto` means run the flash without prompts. Other modes:
   `interactive` (drop into the wizard with the plan's catalog
   pre-loaded; image pick is NOT reported back), `local` (print a
   notice and exit). The plan endpoint clamps unrecognised modes
   to `interactive`.

4. **Completion signal.** On a successful auto-flash, `bty` POSTs
   `/pxe/<mac>/done` (open route, no body). bty-web updates
   `last_flashed_at` + flips `flash-once` -> `local` (per-job CI
   cadence on plain `flash` stays armed).

## Serial-console markers (PXE chain testing)

`bty` in auto-flash mode (`_run_auto`) writes two stable plain-
text markers to **/dev/console** (which the live env's kernel
cmdline aliases to `ttyS0`):

```
bty: auto-flash starting
bty: flash complete; rebooting
```

These are the contract the cijoe PXE chain test pins against (see
`cijoe/configs/test-pxe.toml`). Agents tailing the BMC serial
log / journalctl can pin them too -- they're plain text with no
Rich markup. Pinned in `bty.tui._app._run_auto`.

## bty-web HTTP API

Backed by SQLite at `${BTY_STATE_DIR}/state.db`. Single uvicorn
worker (the SSE bus is in-process).

**Auth.** The operator UI is gated by `$BTY_ADMIN_PASSWORD` (unset =
open, with a startup warning); rotate by changing the env var and
restarting bty-web. `POST /ui/login` (form-encoded `password=...`)
constant-time-compares the password against `$BTY_ADMIN_PASSWORD` and flips
`request.session["bty_authed"] = True`; the session is a server-
signed cookie via Starlette's `SessionMiddleware` (cookie name
`bty-token`). Protected routes read the session via the auth
dependency; `POST /ui/logout` clears it. Open routes (no cookie)
are reachable by PXE clients and live-env tooling which can't
carry one.

**Routes** (all paths case-insensitive on the MAC; the canonical
form is lower-case `aa:bb:cc:dd:ee:ff`):

| Open | Protected |
|---|---|
| `GET /healthz` | `GET /machines` |
| `GET /version` | `GET /machines/{mac}` |
| `GET /pxe/{mac}` (iPXE chain) | `PUT /machines/{mac}` (body: MachineUpsert) |
| `GET /pxe/{mac}/plan` (JSON plan) | `DELETE /machines/{mac}` |
| `POST /pxe/{mac}/done` | `PUT /images/{name}` (stream upload) |
| `POST /pxe/{mac}/inventory` | `PUT /boot/{name}` (stream upload) |
| `GET /pxe-bootstrap.ipxe` | `POST /catalog/entries` (add by URL) |
| `GET /boot/{name}` | `DELETE /catalog/entries?src=...` |
| `GET /images/{key}[/{name}]` | `POST /catalog/import?source=...` |
| `GET /images` (catalog list, JSON) | `DELETE /catalog/cache/{name}` |
| `GET /catalog.toml` (catalog list, TOML) | `GET /events/machines` (SSE) |
| `GET /static/*` | |

**HTTP status semantics:**
- `200` -- success with body
- `204` -- success, no body (DELETE)
- `400` -- malformed input (e.g. invalid MAC)
- `401` -- missing or invalid session cookie on a protected route
- `404` -- protected resource not found (e.g. machine record)
- `422` -- request body failed Pydantic validation (e.g. malformed
  `bty_image_ref`)

**Schema versioning.** Wire types are documented inline in
`reference.md`; breaking changes to those shapes will land under a
versioned URL prefix (`/v2/...`). Agents key off field names.

## Boot mode

Each machine carries a `boot_mode` (renamed from `boot_policy`
in v0.25.0). The mode is the operator's intent and is never
mutated by the server; the transient post-flash state lives in
the `saw_flasher_boot` bit instead (see the mode/state split
introduced in v0.25.0).

- `ipxe-exit` -- every PXE boot returns the iPXE `exit` chain so
  the firmware boots the local disk. Stable / production stance;
  the explicit-PUT default for assigned machines.
- `bty-flash-always` -- per-PXE-contact alternation: flash chain
  on contact N, one-shot ipxe-exit chain of the just-flashed disk
  on contact N+1 (driven by the `saw_flasher_boot` bit, armed on
  `/boot?mac=` artifact fetch). Plan returns `mode=flash` (if a
  ref + serial are bound). Per-job CI cadence: the box reflashes
  itself every netboot, boots its disk once between reflashes
  under PXE-first firmware so it doesn't loop.
- `bty-flash-once` -- same flash chain as `bty-flash-always`, but
  the `saw_flasher_boot` bit stays armed after the first flash
  completes (terminal). Every subsequent PXE contact serves the
  ipxe-exit chain. For "I want this machine reimaged now, then
  leave it alone". Re-armed only when the operator re-saves the
  machine.
- `bty-tui` -- every PXE boot returns the live-env chain; the plan
  endpoint returns `mode=interactive` so the operator picks at
  the tty1 wizard. **Auto-discovery default for unknown MACs**:
  first PXE contact lands the operator at the wizard without
  prior server-side configuration.
- `bty-inventory` -- alternates a live-env inventory boot (bty
  collects disks + lshw, posts to `/pxe/{mac}/inventory`, reboots)
  with the ipxe-exit chain (disk boot) on the next contact.
  Auto-discovery default for fleet machines; same `saw_flasher_boot`
  bit drives the alternation as `bty-flash-always`.

**Server-vs-client truth asymmetry.** `mode=flash` is the only
plan that makes the server the source of truth for what gets
flashed. `mode=interactive` hands the operator the catalog but
**does NOT** receive the operator's pick back; `bty` posts
`/pxe/<mac>/done` after a successful flash but the
`bty_image_ref` / `target_disk_serial` fields are unchanged.
Agents that want server-tracked flashes must configure
`boot_mode=bty-flash-always` (or `bty-flash-once` for the
single-shot case) with a bound ref + serial.

The completion signal `POST /pxe/{mac}/done` updates
`last_flashed_at` only; it does NOT mutate `boot_mode`. The
post-flash "boot the disk" behaviour comes from the
`saw_flasher_boot` bit (armed on `/boot?mac=` artifact fetch,
consumed by the next `/pxe/{mac}`).

## Auto-discovery

A `GET /pxe/{mac}` for an unknown MAC creates an unassigned
`Machine` record (`bty_image_ref == null`,
`boot_mode == 'bty-tui'`) with `discovered_at` / `last_seen_at` /
`last_seen_ip` set, and returns the live-env iPXE chain so the
operator lands at the wizard on the target's tty1. Agents poll
`GET /machines` to find newly-discovered MACs and claim them with
`PUT /machines/{mac}`. Subsequent `/pxe` contacts update
`last_seen_at` / `last_seen_ip`; agents can use the freshness of
those fields to detect machines that have stopped reporting.

## Live updates

`GET /events/machines` is a Server-Sent Events stream (auth: same
session-cookie dep). Subscribers receive an initial
`machines-update` event with the rendered `<tbody>` snapshot, then
one fresh `machines-update` event after every mutation
(`PUT` / `DELETE` and PXE auto-discovery). Used by the browser UI
to avoid polling. Agents can subscribe instead of polling
`GET /machines`, but the canonical state remains the JSON API --
the event payload is HTML for browser consumption.

## Offline-friendly

All client-side assets (Bootstrap CSS, HTMX, HTMX SSE extension)
are vendored in the wheel and served from `/static/`. The bty
appliance does **not** contact any CDN at runtime; agents and
PXE clients can run on air-gapped networks.

## PXE boot stack (server image)

`bty-media`'s server variant ships dnsmasq + the iPXE BIOS/UEFI
binaries. dnsmasq is configured for **TFTP only** -- it serves
`undionly.kpxe` / `ipxe.efi` from `/var/lib/tftpboot/`. **bty
does NOT run any DHCP role** (full or proxy); the operator's
existing LAN DHCP server is configured to point PXE clients at
this appliance via the canonical option-66 (next-server) +
option-67 (bootfile) tagging plus the option-60 "PXEClient"
vendor-class echo. Two-stage chain: PXE ROM ->
`undionly.kpxe`/`ipxe.efi` -> bty-web's `/pxe-bootstrap.ipxe` ->
per-MAC `/pxe/{mac}` chain (template depends on `boot_mode` +
the `saw_flasher_boot` bit for the alternating modes).

## Conventions agents can rely on

- **No `--json` output from `bty`.** The console script is a
  Rich-based wizard; its stdout / stderr is human-facing. Agents
  drive flashing via the HTTP API + plan endpoint, not by parsing
  wizard output. The Python API (`bty.flash.execute_plan`,
  `bty.images.inspect_image`, ...) is the in-process equivalent.
- **stderr is the marker channel** for the auto-flash path. The
  two pinned plain-text markers (`bty: auto-flash starting` and
  `bty: flash complete; rebooting`) also go to `/dev/console` on
  the live env so BMC serial logs capture them.
- **Idempotent reads.** `GET /images`, `GET /catalog.toml`,
  `GET /machines`, and `GET /machines/{mac}` have no side effects;
  safe to call repeatedly. `GET /pxe/{mac}` and `GET /pxe/{mac}/
  plan` DO mutate `last_seen_at` / `last_seen_ip` and may auto-
  create a `Machine` row for an unknown MAC (audit log fires).

## Don'ts

- Don't parse the wizard's stdout / stderr. Use the HTTP API.
- Don't depend on internal module paths (`bty.tui._app`,
  `bty.flash._flash_compressed`, etc.). Anything with a leading
  underscore is private and may be renamed without notice.
- Don't expect bty to write files outside the configured image
  root, the target block device, or the bty configuration / state
  areas.
- Don't expect `mode=interactive` to capture the operator's image
  pick on the server side -- only `mode=auto` is server-truthful.

## Where to look next

- [`PLAN.md`](PLAN.md) - roadmap, motivation, OS scope.
- [`docs/src/reference.md`](docs/src/reference.md) - full HTTP
  API reference, configuration, and wire-type schemas.
- [`docs/src/concepts.md`](docs/src/concepts.md) - boot policy +
  server-vs-interactive truth asymmetry.
