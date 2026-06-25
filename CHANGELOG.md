# Changelog

This file follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
The format reflects what actually matters to an operator running bty
(the `bty-lab` PyPI package + `bty-web` container) -- behaviour the
operator perceives, defaults that survived a `pip install -U`, and
gates that landed in CI.

Per-release commit history lives in `git log`; this file captures the
operator-facing summary.

## [0.60.0] - 2026-06-25

### Removed

- **`/images/{key}[/{name}]` route + the `_stream_remote_image`
  oras stream-proxy.** The route's historical role was "let
  bty-web do the OCI manifest dance for the live env on cold
  withcache". Both backstops are gone:

  - withcache 0.6.0 (v0.59.0's hard dep) is oras-aware on the
    cache-host side, so a withcache deploy absorbs the OCI work
    transparently;
  - The live env's bty TUI handles `oras://` itself via
    `withcache.oras` (resolve + bearer + curl) when the plan
    endpoint hands it the raw URL.

  `pxe_plan` therefore ships the original `src` (oras:// or
  https://) on cold-cache or no-withcache paths now; no more
  bty-web in the bytes path for oras. Operators on the default
  withcache deploy see no change. Operators running bty-web
  without withcache who had oras catalog entries: the live env
  now needs egress to the OCI registry directly (previously the
  bty-web host did the registry talk, the live env stayed
  LAN-only). Configure withcache (the default deploy stack) to
  keep the LAN-only property.

### Changed

- `cache_decision.served_from` no longer carries `"bty-web-proxy"`
  as a value (the proxy is gone). The two surviving values are
  `"withcache"` and `"origin"`.

## [0.59.0] - 2026-06-25

### Changed

- **OCI handling moved upstream to `withcache.oras`.** `bty.oras`
  was deleted. The OCI registry adapter (parse_ref,
  fetch_anonymous_token, fetch_manifest, pick_image_layer,
  resolve_ref, sidecar + mediaType filters) now lives in withcache
  0.6.0+ and bty imports it as a library. Same surface, same
  semantics, just one implementation across the bty ecosystem.

  `bty-lab` now declares `withcache>=0.6.0` as a hard runtime
  dependency. withcache is stdlib-only so the install graph stays
  light, and the default `bty-lab init` deploy stack has shipped
  withcache as a sidecar since v0.40, so most operators won't
  notice. The mypy override for the un-typed import lives in
  `pyproject.toml` until withcache ships a `py.typed` marker.

- **`/catalog.toml` rewrites every remote src through withcache
  when configured.** Previously cached entries went through
  `<bty-web>/images/<sha>/<name>` and uncached entries shipped the
  raw upstream URL (oras + https alike). With withcache 0.6.0+
  oras-aware, the rewrite is uniform: every remote entry becomes
  `<withcache>/b/<b64(origin)>/<basename>` regardless of original
  scheme. The live env consuming the catalog sees only plain HTTPS
  URLs on the LAN cache; withcache absorbs the ghcr.io stream-cut
  class internally via its Range-resume loop. Falls back to the
  pre-existing direct-origin behaviour when `BTY_WITHCACHE_URL`
  is not configured.

- **Withcache HEAD probes drop bty-side bearer minting.**
  `/pxe/{mac}/plan` and `/ui/catalog/entries/check` used to
  pre-resolve an OCI bearer and pass it on the HEAD's
  `Authorization` header so withcache 0.4.0's background fetch
  worker could forward it. Withcache 0.6.0+ mints its own bearer
  when the cache key is `oras://...`, so both endpoints now HEAD
  against the original `src` (oras or https) with no bearer
  plumbing. The `head_headers` + `fetch_anonymous_token` paths in
  `_app.py` and `_ui.py` were removed.

### Fixed

- HTTP/2 stream-resets on multi-GiB `oras://ghcr.io/...` flashes
  now have two backstops: the surgical `--http1.1` from v0.58.3
  (already shipped) on the bty client side, and withcache's
  Range-resume on the cache-host side once the catalog routes
  through it (this release). Operators on the default withcache
  deploy get the resume path transparently.

## [0.58.3] - 2026-06-25

### Fixed

- **`oras://` flash failed with `curl exited 92` after roughly the
  same N minutes on every retry.** On bty-usbboot, large
  `oras://ghcr.io/...` images aborted mid-stream with a
  `CURLE_HTTP2_STREAM` framing-layer reset. GHCR's blob CDN
  (`pkg-containers.githubusercontent.com`) RST_STREAMs long-running
  HTTP/2 blob transfers once the pre-signed redirect URL's TTL
  expires, and our pipeline streams directly into `dd` with
  `--retry` deliberately disabled (retrying from byte 0 would
  corrupt the target), so the stream death was terminal. Forcing
  HTTP/1.1 on every streaming fetch sidesteps the framing-layer
  reset; HTTP/2 multiplexing buys nothing for a single large
  stream-to-dd transfer, so the cost is zero. Local-pre-download
  isn't an option in the live env (no writable disk, RAM
  too small for multi-GiB blobs), so HTTP/1.1 is the right
  trade-off for now. (`src/bty/flash.py:_CURL_BASE`.)

## [0.58.2] - 2026-06-23

### Changed

- **Docs: aligned components / flows / reference with current routes
  and event kinds.** Several pages had drifted post-v0.57:

  - `flows.md`'s audit-log table listed a TFTP-control kind pair
    that was removed (`netboot.tftp.controlled` / `.control_failed`),
    two "kinds" that are actually `netboot.pxe.offered` rows with
    `details.reason` flags (`orphan_ref` / `no_target_disk`), and
    used the old underscore form on `.failed` kinds; new
    `.requested` / `.started` / `.cancelled` lifecycle kinds + the
    `system.schema.reset` tripwire were missing. Operator-UI table
    pointed at the removed `/ui/settings/tftp-control` and was
    missing the new `/ui/settings/{upstream,backup,config/edit}`
    rows.
  - `components.md`'s "Failure symmetry" listed underscore-form
    kinds and event kinds that the code never emits (`image.upload*`,
    `image.hash*`, `settings.tftp.control_failed`); "Filtering"
    described the retired multi-dropdown form instead of the
    `?q=` substring search.
  - `reference.md` documented two routes that don't exist
    (`POST /ui/settings/flash`, `POST /ui/settings/tftp-control`)
    and described the old single Upstream-sources card layout.

- **Operations docs: corrected the bty-web export description.** The
  doc called the export tool "selective" with "image bindings,
  catalog, and local image files". The actual export shape per
  `_portability.py` is just `mac` + `lshw` + `known_disks`; the
  catalog and bindings reset on import. The "full tar backup"
  example was also claiming it captured cached image bytes --
  bty-web exited the bytes plane in v0.40, so cached blobs live in
  withcache's own data dir now. Both rewritten.

- **Images table: cleaner explanation of the "unset" sha badge.** The
  title-attr tooltip was promising "an opt-in v0.41 verifier will
  hash the stream and compare against this column when set" -- the
  verifier shipped in v0.41 (`bty.flash._spawn_hash_tee`); the
  tooltip now describes what actually happens (the live env
  `curl | tee | sha256sum | dd` refuses on a mismatch when this
  column is set, skips the check when unset).

- **Stale env-var alias removed.** `_app.py` + the Backup-schedule
  card comment claimed `BTY_BACKUP_DIR` was a "legacy" alias for
  `BTY_PATHS_BACKUP_DIR`; the legacy var is not read anywhere. An
  operator who tried it would have seen no effect. Mention dropped
  so the documented surface matches the consumed surface.

- **Audit-log docstrings + comments dropped references to event
  kinds the code never emits** (`image.upload.failed`,
  `image.hash.failed`, `settings.tftp.controlled`). Only
  `image.upstream.truncated` is a real image-namespaced kind;
  uploads + hashing left the bty-web scope when v0.40 took it out
  of the bytes plane, and the TFTP-control route was removed.

## [0.58.1] - 2026-06-23

### Changed

- **Docs: corrected the backup-bundle "Travels" table.** The
  `operations.md` table was overstating what a v3 bundle carries
  (claiming image binding / `target_disk_serial` / `labels` /
  `catalog_entries` travel) -- the actual export shape per
  `_portability.py` is just `mac` + `lshw` + `known_disks`. Operators
  reading the doc would have expected their bindings + catalog to
  survive an export/import; the code has always reset all of that
  on import. Table rewritten to match the export shape.

- **Search-input placeholder spells out what matches.** The
  `/ui/machines` Filter input now reads `MAC / label / image / IP
  (any field, substring)` so it's clear that typing one term hits any
  field of any row, including any of the machine's labels.

## [0.58.0] - 2026-06-22

### Changed

- **Machine record: `hostname` replaced by plural `labels`.** The
  field has always been described as "cosmetic; not consumed by the
  boot chain", but the column was a singular `hostname` with an
  RFC-1123 validator. In practice operators want to tag a box with
  several non-overlapping things at once (rack location, hardware
  vendor, ad-hoc notes), and the DNS regex rejected reasonable
  inputs (underscores, spaces, mixed case). Reshaped end-to-end:
  the `machines.hostname` column is gone, replaced by a `machine_labels`
  side table; the JSON API takes `labels: list[str]`; the UI form
  is a comma-separated input; the row renders chip badges; each
  chip is a `/ui/machines?q=<tag>` link. Per-label shape is
  alnum-leading + alnum/space/`-`/`_`/`.`, max 64 chars; cap of 16
  labels per machine. **Breaking, pre-1.0:** `state.db` auto-rotates
  on upgrade (the existing schema-mismatch path) so machine bindings
  reset; export with `bty-web export` before upgrading if you want
  to preserve the hardware-inventory side.

- **Machines table: new "Last IP" column + column reordering.** The
  last-seen source IP was already captured (`last_seen_ip`, populated
  on every `GET /pxe/{mac}`) but only shown on the detail page. It
  now has its own sortable column, and the overall order is identity
  (MAC, Labels) -> configuration (Image, Boot) -> timeline (Last
  seen, Last IP, Last flashed) so the eye scans left-to-right in
  the same "which box / what is it set up to do / what has it done"
  order operators ask. Each IP cell deep-links to
  `/ui/events?q=<ip>` so the "this MAC is on .42 -- what else has
  .42 done?" pivot is one click.

## [0.57.1] - 2026-06-19

### Changed

- **Subnav strip vertical rhythm + text uniformity.** v0.57.0
  bumped the strip padding to give the new page-level forms
  breathing room, but the result read as too generous against
  the rest of the chrome. Cut top / bottom padding to 0.25rem,
  dropped the inner container's ``min-height`` to 2rem, and
  set every text-bearing descendant
  (``form-control-sm`` / ``btn-sm`` / ``a`` / ``label`` /
  ``.small`` / ``small``) to ``font-size: inherit`` so the
  strip's single 0.85rem applies to anchors, labels, inputs,
  and buttons alike. Before the fix three slightly-different
  sizes (subnav-jumps anchors at 0.82rem, Bootstrap form
  controls at their own 0.875rem, ``.small`` Filter labels at
  0.875em of the parent) lived in the same row.

## [0.57.0] - 2026-06-19

### Changed

- **Settings page: the upstream sources panel is now two visually
  distinct cards.** The single mixed "Upstream sources" form was
  hard to scan because netboot artifacts and the image catalog
  share nothing operationally. They now live in
  ``#netboot-release`` (netboot repo + tag) and ``#catalog-source``
  (catalog URL); the Settings sub-nav has matching jump links.
  Both cards still submit through one POST handler so saving from
  either side persists all three fields in one round-trip.

- **Catalog setting collapses to a single URL field.** The
  ``catalog_repo`` + ``catalog_tag`` pair (and the URL-composition
  logic on top of them) is gone; the operator now sets one
  ``catalog_url`` value, defaulting to
  ``https://github.com/safl/nosi/releases/latest/download/catalog.toml``.
  The ``Fetch latest catalog`` button GETs whatever string the
  Settings page holds: pointing at a fork or a private catalog
  server is a single-field edit, not a repo-and-tag puzzle. Pre-1.0
  break-freely: any existing ``upstream.catalog_repo`` and
  ``upstream.catalog_tag`` rows in ``state.db`` are now dead keys
  (harmless, ignored on read).

## [0.56.0] - 2026-06-19

### Added

- **Paginated + sortable tables on /ui/machines and /ui/images, plus
  free-text search across both.** Operators running a real lab fleet
  end up with hundreds of `machines` rows and a catalog of dozens of
  images; the old "render everything in one scroll" approach was
  becoming painful. Now:

  - Every column header is a sort toggle (click once to sort, click
    again to flip direction). The active column shows an arrow; the
    others show a faded up-down glyph to advertise that they're
    clickable.
  - A per-page selector (25 / 50 / 100) plus a Prev / 1..N / Next /
    Last pagination strip lives under each table. The footer also
    shows `Showing 26-50 of 178 machines`.
  - A free-text search box (`?q=`) above each table narrows by
    substring across the columns the operator usually pivots on:
    MAC / hostname / image-ref / last-seen IP for machines, name /
    format / arch / source / sha256 for images. Typing `freebsd`
    shows only the freebsd images; typing the last hex of a MAC
    finds that one machine.
  - All state (sort, direction, page, per_page, q, filter) lives in
    the URL, so the view is bookmarkable and survives a refresh.
  - The `?sort=` column is allowlisted per-page; an out-of-list
    value silently falls back to the default rather than being
    interpolated into SQL.
  - SSE auto-refresh on /ui/machines stays on only for the default
    view (no filter / no search / default sort / page 1); operators
    drilling into a slice of the fleet now get a stable static view
    instead of having the SSE push wipe their sort.

  The existing categorical `?filter=` (discovered / assigned) keeps
  working and composes with the new search + sort + pagination.

## [0.55.12] - 2026-06-19

### Fixed

- **Auto-flash milestone markers no longer stack the Rich progress
  bar -- the residual case from v0.55.11**. The v0.55.11 fix
  removed the visible stderr text leak by routing milestones
  through `/dev/kmsg` only, but `/dev/kmsg` writes go through
  `printk` and fan out to every registered console -- including
  `/dev/tty0`, where the kernel-timestamped line landed on the
  framebuffer between Rich repaint cycles. The text itself was
  covered by Rich's next paint within ~100 ms (so invisible to
  the operator's eye), but the framebuffer cursor had already
  advanced one line and Rich's internal cursor tracker was now
  out of sync with the screen. Each milestone shifted the next
  bar render one line down: three milestones, three stacked
  pairs. The new path writes milestones **directly** to
  `/dev/console`, which resolves to the LAST `console=` cmdline
  target (`ttyS0` on every bty cmdline -- USB, PXE, chain test).
  That hits the serial UART only: SoL / IPMI observers and the
  chain test still capture the heartbeat through the same UART
  they watch for everything else, and `/dev/tty0` is never
  touched. The local HDMI screen finally stays a single clean
  bar pair through 25/50/75/100%.

## [0.55.11] - 2026-06-18

### Fixed

- **Auto-flash milestone markers no longer scramble the on-screen
  Rich progress bar**. `bty-on-tty1.service` routes
  `StandardError=tty` to `/dev/tty1`, so the v0.55.5 milestone
  emitter's `print(..., file=sys.stderr)` injected raw
  `bty: download NN%` lines into the same TTY Rich was painting
  on; each line shifted the cursor and stacked a duplicate
  `writing` / `downloading` bar pair below. The milestone path
  now writes only to `/dev/kmsg`, which still fans out via
  `printk` to every registered serial console (SoL / IPMI
  observers keep their 25/50/75/100% heartbeats, the local
  HDMI operator sees a single clean progress bar). Lifecycle
  bookends (`bty: entered`, `bty: exiting`,
  `bty: auto-flash starting`, `bty: flash complete; rebooting`)
  still write to all three sinks: they fire outside the Rich
  Live context and SHOULD appear on the local tty.

## [0.55.10] - 2026-06-18

### Changed

- Internal housekeeping: dropped redundant empty-string arg from
  three `print("", file=sys.stderr)` calls in `src/bty/deploy.py`
  (FURB105 auto-fix). Same behaviour. Only finding from a fresh
  tech-debt sweep across all code added since v0.55.3; the rest
  of the codebase came back clean.

## [0.55.9] - 2026-06-18

### Fixed

- **Netboot wait for the missing BTY_IMAGES device is now zero,
  not 20 seconds**. 0.55.8 capped
  `bty-usb-grow.service`'s `JobTimeoutSec` at 20 s; the
  `var-lib-bty-images.mount` unit's own auto-generated `Wants=`
  on the same device still triggered a 20 s wait. A tiny systemd
  generator at
  `/usr/lib/systemd/system-generators/bty-skip-usb-only-units-on-netboot`
  now masks **both** units when `fetch=` is on the kernel
  command line (the reliable netboot discriminator). With nothing
  Wanting= the device, systemd never activates it and the
  `Expecting device dev-disk-by-label-BTY_IMAGES.device` line is
  gone from the boot log. Empirically measured on a local QEMU
  PXE boot: `bty: entered` moved from ~33 s (0.55.8) to ~13 s
  (this fix), an 87 s improvement vs 0.55.7. The generator
  self-traces via `/dev/kmsg` so an operator can verify in
  `dmesg` whether it ran and what verdict it reached. USB-boot
  path unchanged (no `fetch=` cmdline -> generator no-ops ->
  units run normally).
- `.gitignore` anchored `lib/` and `lib64/` to the repo root.
  Unanchored, the Python setup.py boilerplate rules also matched
  `bty-media/.../includes.chroot/usr/lib/` and silently dropped
  the new systemd generator from `git add`.

## [0.55.8] - 2026-06-17

### Added

- **`bty-trace` boot-phase markers** for empirical timing on slow
  boots. A tiny helper at `/usr/local/sbin/bty-trace` writes one
  line via `/dev/kmsg` per invocation (same fanout as the
  existing `_emit_console_marker`), so the line shows up on
  every registered console -- SoL on ttyS0/ttyS1, framebuffer on
  tty0 -- carrying the kernel's `[ T ]` boot-time prefix for
  free. Wired as `ExecStartPre` / `ExecStartPost` on
  `bty-usb-grow.service`, `bty-clock-from-http.service`,
  `bty-images-discover.service`, and `bty-on-tty1.service`.
  Subtracting consecutive `[ T ]` timestamps in dmesg / SoL now
  tells the operator which unit actually ate a slow boot's
  wall-clock budget -- triangulating between the existing
  `bty-banner-{early,mid,late}` phase boundaries (sysinit /
  network-online / multi-user). Visible on every PXE / USB boot
  because bty's cmdline deliberately omits `quiet`.

### Fixed

- **PXE-only boots no longer waste 67 seconds waiting for a USB
  stick that doesn't exist**. Empirically confirmed via the new
  `bty-trace` markers on a local QEMU PXE boot: an 86.8 s gap
  between `bty-clock-from-http ready` and `bty-images-discover
  starting` -- which is systemd's default 90 s
  `DefaultDeviceTimeoutSec` ticking down on
  `dev-disk-by-label-BTY_IMAGES.device`. On a PXE-only boot the
  BTY_IMAGES label never appears; the .device unit sits at
  "expected" until the global timeout, holding up everything
  ordered after it. `bty-usb-grow.service` now sets
  `JobTimeoutSec=20s` so the job aborts at 20s when the device
  never materialises; downstream units (which depend on
  bty-usb-grow's job completion, not its success) proceed
  immediately. Predicted in memory
  `project_ventoy_90s_boot_delay.md` but never landed.

## [0.55.7] - 2026-06-17

### Fixed

- **Upstream truncation during oras:// image streaming now fails
  loudly server-side**. Field failure on a Supermicro H12SSL-I
  flashing `nosi-fedora-44-desktop`: ghcr.io closed the TCP
  connection 1,504 MiB into a multi-GB oras blob, bty-web's
  `_chunks()` treated the empty read as clean EOF, the
  StreamingResponse finished tidily, and the live env's curl
  detected the Content-Length mismatch only client-side as exit
  18. The bty-web journal had no record of which blob truncated.
  `stream_src` now tracks emitted bytes and raises `CatalogError`
  when the upstream stops before `Content-Length` is reached;
  the call site in `_stream_remote_image` catches it, records
  an `image.upstream.truncated` event against the offending src
  URL, and propagates so the live env still detects the failure
  via curl exit 18 (so a partial image never reaches `dd`).
  Same shape as safl/withcache#7's `TruncatedDownload` guard,
  but for the bty-web -> oras-registry hop which bypasses
  withcache entirely. Mitigation: the operator should
  pre-cache via `/ui/images` "Download" before flashing
  upstream-unstable images so the LAN-only path is taken.

## [0.55.6] - 2026-06-17

### Added

- **Lifecycle bookends on every bty run**. The 0.55.5 milestone
  markers only fire on the auto-flash path; an operator
  following IPMI SoL through an interactive wizard, a USB-local
  run, or a hand-driven `bty --catalog X` had no marker to
  follow. `bty.tui:main` now emits `bty: entered v<X>` right
  before the wizard launches and `bty: exiting v<X>` in a
  `finally` so it fires for every exit path (clean run,
  `sys.exit` deep in the wizard, KeyboardInterrupt, post-flash
  reboot, unhandled exception). Same `/dev/kmsg` + `/dev/console`
  fanout as the rest of the markers, so the pair lands on every
  registered console regardless of mode.

## [0.55.5] - 2026-06-17

### Added

- **Auto-flash now emits 25 / 50 / 75 / 100% milestone markers**.
  Operators watching a Supermicro / Aspeed-BMC PXE flash over
  IPMI SoL previously only saw the `auto-flash starting` and
  `flash complete; rebooting` bookends, with nothing between them
  while the multi-gigabyte download + write was running on
  `/dev/tty1` (the framebuffer VT, invisible to a serial
  console). bty now fires `bty: download NN%` and
  `bty: write NN%` lines via the same `/dev/kmsg` + `/dev/console`
  fanout the bookends use, once per 25 / 50 / 75 / 100 crossing.
  At most 8 extra kmsg writes per flash. Skipped silently when
  the total is unknown (some write paths can't pre-compute the
  decompressed size). Identical behavior for interactive flashes
  on tty1: the markers also land in `journalctl -u bty-on-tty1`
  so a remote operator tailing the journal gets the same
  heartbeat.

## [0.55.4] - 2026-06-17

### Fixed

- **Supermicro / Aspeed BMC USB Virtual NIC no longer hijacks
  live-boot's network**. Boards with an Aspeed BMC (Supermicro
  H12SSL-I, X11/X12 generations, several others) expose a "USB
  Virtual NIC" gadget that presents over RNDIS. The kernel
  claimed it via `rndis_host`, udev renamed it to `enxbe...`,
  live-boot saw two connected ethernets, picked the
  alphabetically-first one (the BMC virtual NIC, sorts before
  `eno*`), tried to DHCP+fetch the squashfs over the BMC's
  internal USB net, timed out, and panicked init with
  `Unable to find a live file system on the network`.
  `modprobe.blacklist=rndis_host` now sits next to the existing
  `nouveau` blacklist on every live-env cmdline (both iPXE
  templates and both live-build BOOTAPPEND lines), so the
  virtual NIC never registers as a network device and live-boot
  only sees the real wire. Real USB-Ethernet dongles (Realtek
  r8152, ASIX AX88179) are unaffected; they use chip-specific
  drivers, not the generic `rndis_host`.

## [0.55.3] - 2026-06-17

### Fixed

- **Respawn cap on `bty-on-tty1.service` actually applies again**.
  `StartLimitIntervalSec=60` + `StartLimitBurst=5` were in the
  `[Service]` section, where systemd 257 silently ignores them
  (visible in the CI log capture as `Unknown key
  'StartLimitIntervalSec' in section [Service], ignoring`). Moved
  to `[Unit]` so a wedge in the first bty frame no longer
  respawns the splash forever and bury the actual error.

### Changed

- Internal housekeeping: dropped 19 redundant local imports that
  shadowed module-level imports in the test suite, switched
  `_suppress_oserror.__enter__` / `__exit__` annotations to
  `Self` + `TracebackType | None` per the textbook
  context-manager pattern, and addressed three ruff findings
  (D413 docstring spacing, PLR1730 `max()` over `if`, PT022
  fixture `return` vs `yield`). No behavior change in production
  code; the full pytest suite stays green.

## [0.55.2] - 2026-06-16

### Fixed

- **PXE kernel console now reaches Supermicro BMC SoL**. The PXE
  templates only emitted to `ttyS0`, which matches Dell / iLO
  (they bridge COM1 to SoL) but not boards that wire SoL to the
  BMC's dedicated UART (Linux sees it as `ttyS1`). On those
  boards the BMC web UI sometimes exposes only "COM1 or SOL" with
  no usable COM1 wiring, so the operator can't redirect from the
  BMC side either; the cmdline is the only place to fix it.
  `ipxe_flash.j2` and `ipxe_tui.j2` now emit
  `console=ttyS1,115200 console=ttyS0,115200`: the kernel writes
  to both UARTs regardless of order, so whichever the BMC bridges
  sees the boot stream, while `/dev/console` (which follows the
  last-listed console) stays on ttyS0 so the cijoe chain test's
  marker scan still works. Also adds `earlyprintk=ttyS0,115200`
  so the kernel's own console-registration messages reach the
  captured serial log before the printk console-list is
  reshuffled, leaving a diagnostic trail for any future ordering
  regression.

### Changed

- **PXE chain test now binds QEMU COM2 explicitly to a null
  backend**. The i440FX SuperIO emulation has two UARTs
  unconditionally, but a single `-serial` directive only binds
  the first. The second was previously left without a chardev,
  which made the kernel-side 8250 probe of ttyS1 behave
  inconsistently when the PXE templates listed both
  `console=ttyS0` and `console=ttyS1`. An explicit
  `-serial null` for COM2 gives ttyS1 a defined "writes go to
  /dev/null" backend, so the chain test is deterministic
  regardless of how the cmdline orders the consoles.

## [0.55.1] - 2026-06-16

### Fixed

- **Netboot page no longer renders a permanently-grey "Local
  dnsmasq.service" row on container deploys**. bty-web running in a
  container can't see the host's `dnsmasq.service` (different mount
  namespace) and the bty-tftp sidecar's dnsmasq lives in yet
  another container, so the local-unit probe was always `unknown`
  there. The network probe above the row is the canonical signal
  in that mode, so the subsection is now hidden when bty-web
  detects it is running inside a container. Bare-metal installs
  see the same row as before.

### Changed

- **Docs landing-page title aligned with the README**. The Sphinx
  index page carried a longer, descriptive title; the README and
  `pyproject.toml` description used the shorter elevator pitch.
  All three surfaces now read the same one-liner so the project's
  identity is consistent wherever a new operator lands first.

## [0.55.0] - 2026-06-15

### Fixed

- **Flash write progress bar no longer freezes at 100%**. The write
  total is fundamentally unknowable in advance (gzip wraps its
  uncompressed-size trailer mod 2^32, qcow2 virtual_size need not
  equal the bytes dd ends up writing, the compressed-size fallback
  is smaller than the decompressed write count). Init the write task
  with `total=None` so Rich's `BarColumn` draws a pulsing scanner
  (a KITT / Knight Rider look) and add a `TransferSpeedColumn` so
  the operator still sees the running byte count, live bandwidth,
  and elapsed time. The download bar stays determinate because
  `Content-Length` is reliable. Hardware-spotted on v0.54.0 against
  a debian-13-headless.img.gz served via piKVM virt-mount.
- **"Back up now" button on `/ui/backups` now visibly does
  something**. A v3 metadata-only backup completes in milliseconds,
  faster than the active-jobs poll cadence, so the page never
  registered the active-to-inactive transition that drives the
  auto-reload; the server-rendered "Backups on disk" card did not
  pick up the new bundle. The click handler now reloads the page
  after a successful enqueue so the new bundle and refreshed
  cadence indicators surface immediately.

## [0.54.0] - 2026-06-15

### Added

- **Two progress bars during a URL flash**: download and write run in
  parallel as a streaming pipeline (curl, optional decompressor, dd to
  target). Before this release a single bar tracked the writer side
  only, so a slow upstream and a slow target disk looked identical to
  the operator and compressed images underreported network throughput
  by the compression ratio. A small dd is now interposed between curl
  and the rest of the pipeline; its progress feeds a new
  `downloading_progress` event family alongside the existing
  `writing_progress`. The TUI lazily adds a second Rich progress task
  on the first download tick, so local-file flashes keep the
  single-bar layout while URL flashes get a download bar above the
  write bar.

### Fixed

- **GitHub Pages docs deploy survives a job re-run**. A re-attempt of
  the `Deploy to GitHub Pages` job inside the same workflow run left
  the prior `github-pages` artifact in place; the next
  `upload-pages-artifact` added a second copy and `deploy-pages`
  aborted with "Multiple artifacts named github-pages were
  unexpectedly found for this workflow run". The deploy job now
  deletes any pre-existing `github-pages` artifact for the current
  run before uploading, so a re-run is idempotent.

## [0.53.0] - 2026-06-15

### Added

- **Architecture column on the catalog / image listings**. The bty TUI's
  image table and bty-web's `/ui/images` page now show a best-effort
  arch hint (`x86_64`, `arm64`, `i386`, `arm`, `riscv64`, etc.) for each
  catalog entry. Operator-facing display only -- bty never restricts or
  filters flash eligibility on it; the column just makes it visible at
  a glance which images target which platform. Sources, in order:
  explicit `arch = "..."` field in catalog manifests (publishers like
  nosi should populate it for accuracy), then a filename heuristic
  fallback that recognises the common token spellings. `?` / `-` when
  nothing resolves.

### Changed (BREAKING)

- **Variant naming streamlined along two axes**: boot source
  (`netboot` over PXE, `usbboot` from a stick) and hardware family
  (`pc` for generic x86 BIOS / UEFI, `rpi` for Raspberry Pi SBCs).
  Previously `netboot-x86` mixed an arch suffix with the boot source
  while `usb-rpi` used a HW-family suffix, hiding that the `rpi`
  variant is platform-specific (RPiOS + Pi firmware + per-SoC kernels)
  rather than just arch-specific. New variant names:

  | Old | New |
  |-----|-----|
  | `netboot-x86` | `netboot-pc` |
  | `usb-x86`     | `usbboot-pc` |
  | `usb-rpi`     | `usbboot-rpi` |

  Operators with custom `BTY_VARIANT=...` env settings (e.g. CI scripts
  invoking `make build VARIANT=...`) must update them. The release
  artifact filenames change in lockstep: `bty-netboot-pc-x86_64-v*.{vmlinuz,initrd,squashfs}`,
  `bty-usbboot-pc-x86_64-v*.iso`, `bty-usbboot-rpi-arm64-v*.img.gz`.
  Existing files in `BTY_PATHS_BOOT_DIR` from prior releases are
  ignored by the new bty-web (which now looks for the `-pc-` prefix);
  upgrade workflow is "fetch netboot artifacts" from the UI after
  bumping the container, same as any other release upgrade.

- **Wheel asset dropped from GitHub releases**. `bty-lab` is
  distributed via PyPI; the wheel on the GH release was redundant
  with `pipx install bty-lab` and nothing in the tree consumed it
  from there. The sdist (`bty_lab-X.Y.Z.tar.gz`) is kept as the
  archival source release for distro packagers and offline mirrors.

## [0.52.0] - 2026-06-14

### Fixed

- **Five environment variables in the deploy template / docs were dead
  names** the server never reads. Operators following `bty-lab init`'s
  `envvars` (or the docs) were setting variables bty-web ignored:
  `BTY_TRUSTED_PROXY` -> `BTY_SERVER_TRUSTED_PROXY` (real client IP in
  audit logs behind a proxy), `BTY_MAX_UPLOAD_BYTES` ->
  `BTY_TUNING_MAX_UPLOAD_BYTES` (raise the upload cap; the 413 error
  message named the wrong var too), `BTY_SESSION_SECRET` ->
  `BTY_SERVER_SESSION_SECRET`, `BTY_BACKUP_MAX_PARALLEL` ->
  `BTY_TUNING_BACKUP_MAX_PARALLEL`, and `BTY_TFTP_PROBE_HOST` ->
  `BTY_NETBOOT_TFTP_PROBE_HOST`. The session-secret one was emitted
  uncommented, so a pinned secret was silently dropped, breaking cookie
  continuity across a multi-instance / blue-green deployment.

### Security

- Redact `Bearer` tokens from subprocess (curl) stderr before it reaches
  the flash progress UI / logs, so a short-lived oras registry token in a
  captured stream can't be replayed.

### Documentation

- Add an "Integrity and trust model" section (what bytes are verified vs
  what is trusted; the unauthenticated `/pxe/*` surface and trusted-LAN
  assumption) and a "Recovering from a failed or interrupted flash"
  section.

## [0.51.0] - 2026-06-14

### Added

- **Integrity verification during flash**: when an image source
  commits to a content digest, bty now verifies the streamed bytes
  against it on the wire and aborts with an error on mismatch, instead
  of silently writing a corrupted or tampered download. The hash is
  computed in the pipeline (`curl | tee | sha256sum | dd`), so it adds
  no measurable overhead and the payload never passes through Python.
  Two sources are covered: `oras://` references (the layer digest,
  frozen at resolve time) and plain-HTTP catalog / bty-web images
  carrying a declared `sha256` (the interactive catalog entry's field
  and the PXE plan's new `disk_image_sha`). Sources with no declared
  digest keep the existing zero-copy stream unchanged.

## [0.50.0] - 2026-06-13

### Changed

- **usb-rpi**: the Raspberry Pi USB flasher is now built by
  customizing Raspberry Pi OS Lite (arm64) in place (download +
  loop-mount + chroot) instead of Debian live-build. RPiOS ships
  every Pi kernel + every `bcm*.dtb` (incl. the CM5 / CM5IO device
  trees) + firmware + bootloader, so the image boots Pi 4 / CM4 /
  Pi 5 / CM5 with no per-board branching. This fixes the previous
  live-build image, which shipped a boot partition with no device
  trees at all and could not boot a CM5 ("Device-tree file
  bcm2712-rpi-cm5-cm5io.dtb not found"). The bty TUI, services, and
  hooks are unchanged; they are grafted onto the RPiOS rootfs via the
  same `includes.chroot/` tree and `config/hooks/`.

## [0.49.0] - 2026-06-13

End-to-end audit sweep covering every surface (web endpoints, TUI +
flash pipeline, ORAS resolver, live-build, CI workflows, server
appliance, docs). Three classes of fix landed: real data-loss
guards, defensive-depth fixes against silent failures, and a
pre-1.0 cruft cleanup.

### Fixed

- **flash**: every ``dd`` invocation now passes ``oflag=direct``
  in addition to ``conv=fsync``. With only conv=fsync the kernel
  page cache shadowed the writes when the target happened to be
  the disk we booted from; the running OS's binaries kept
  executing the OLD content until the page was evicted while the
  on-disk side took the new image. O_DIRECT bypasses the page
  cache so the in-RAM and on-disk states stay consistent through
  an in-place reflash.
- **flash**: gzip's uncompressed-size trailer wraps mod-2^32. For
  any ``.img.gz`` >= 4 GiB the reported number is a lie;
  ``validate_plan`` would have happily greenlit flashing a 5 GiB
  image onto a 4 GiB disk because gzip -l reported 1 GiB.
  ``_parse_gzip_listing`` now detects the wrap (uncompressed <
  compressed) and returns None so the size-fits-target check is
  skipped with a note rather than fooled.
- **tui**: the wizard's flash worker now passes a cancel callback
  through to ``execute_plan``. Pre-fix, Ctrl+C during a flash
  interrupted t.join() in the main thread but the daemon worker's
  dd / curl / zstd subprocesses kept writing -- the operator's
  "abort" left a partial-write in flight on the target disk with
  no cleanup. The wizard now SIGTERMs the pipeline and renders a
  dedicated "Cancelled -- target holds a partial write" panel.
- **oras**: ``pick_image_layer`` previously filtered sidecar layers
  only by title-annotation suffix, then took the largest remaining
  layer. A Helm OCI chart, Cosign signature, in-toto attestation,
  or SBOM lacks the sidecar suffix and would be returned as the
  "image" layer; bty.flash would then dd the chart's tar+gzip
  headers or the signature bytes into the target's MBR. Add an
  explicit mediaType refusal pass before the size-pick covering
  Helm / Cosign / in-toto / DSSE / SPDX / CycloneDX / OCI-empty.
- **web**: ``ReleaseFetchManager.cancel(tag)`` now validates against
  ``_TAG_RE`` symmetrically with ``enqueue``. Previously cancel
  fell through to the base manager's plain ``_states.get(key)``,
  so attacker-controlled text in a DELETE /boot/releases/{tag}
  request returned a 404 + logged the literal string as the audit
  row's subject_id; cancel now raises 422 on malformed tags.
- **web**: ``fetch_release_catalog`` is an async route that called
  ``urllib.request.urlopen(timeout=30)`` inline on the event loop.
  An unreachable release host stalled every other request handler
  (including the SSE event stream's heartbeat) behind it for the
  full 30s; wrap in ``asyncio.to_thread``.
- **web**: ``_release_mgr._backfill_from_events`` soft-failed on
  any exception with no log line at all. Replace the bare ``return``
  with a ``log.debug(..., exc_info=True)`` so a real schema/data
  problem at startup leaves a breadcrumb when debug logging is on;
  the soft-fail behaviour itself is unchanged.
- **media**: ``bty-clock-from-http``'s URL loop was ``candidate_urls
  | while read``: POSIX sh runs the right side of a pipe in a
  subshell, so every ``exit 0`` inside the loop body only exited
  the subshell. The trailing ``log "no candidate URL produced a
  Date: header"`` then ran unconditionally, including after a URL
  had successfully stepped the clock -- an operator chasing a
  phantom NTP failure read this as proof the script had no
  effect when it had actually done its job.
- **media**: ``pack_rpi_img._locate_lb_output`` used ``rglob`` and
  took the first match; on filesystems that listed
  ``binary/EFI/vmlinuz.efi`` before ``binary/live/vmlinuz`` the
  Pi image would have shipped the wrong kernel against the
  squashfs. Prefer ``binary/live/`` explicitly.
- **media**: ``bty-on-tty1.service`` had ``Restart=on-failure /
  RestartSec=5`` with no ``StartLimit``. A first-frame crash
  (corrupted catalog stick, malformed bty.toml) would have
  respawned the bty splash on tty1 every 5 seconds forever with
  no way for the operator to see the real error short of
  switching ttys. Add ``StartLimitIntervalSec=60`` +
  ``StartLimitBurst=5``.

### Changed

- **ci**: ``publish-tftp`` previously needed only ``[test,
  build-ipxe]`` so a tag whose PXE chain test or USB Ventoy boot
  test was red still pushed ``ghcr.io/safl/bty-tftp:vX.Y.Z`` and
  moved ``:latest``. Gate it on the same release-blocking pipeline
  as ``attach-to-release`` / ``tag-release`` so a red test
  anywhere blocks the tftp container too.
- **deploy**: ``bty-lab upgrade`` no longer migrates v0.41
  ``envvars`` files to v0.42 ``bty.toml``. The migration code
  (``_envvars_to_bty_toml`` + helpers) is gone; ``upgrade`` now
  hard-errors with a pointer at ``bty-lab deploy --force`` when
  ``bty.toml`` is missing.
- **tui**: drop the no-op ``_print_source_summary`` /
  ``_print_selection_so_far`` shims and their three call sites.

### Docs

- Replace every legacy ``BTY_STATE_DIR`` / ``BTY_BOOT_DIR`` /
  ``BTY_BACKUP_DIR`` / ``BTY_CATALOG_FILE`` / ``BTY_WEB_*``
  reference in doc sources with the canonical section-prefixed
  names (``BTY_PATHS_STATE_DIR`` etc., ``BTY_SERVER_*``).
- ``quickstart.md``'s "Flash a USB stick" code block was two
  ``dd`` invocations spliced together. Replaced with a single
  ``sudo dd ... oflag=direct conv=fsync`` that matches the
  in-code rule and copy-pastes cleanly.
- Catch up to v0.40-bytes-less + v0.46-catalog-source +
  ``boot_mode`` renames across walkthrough-catalog,
  walkthrough-server-docker, tutorials/bty-netboot, README,
  reference, operations, concepts, and bty-media README.
- Add ``usb-rpi`` to dependencies.md's build-deps table;
  tutorials/bty-usb-rpi.md's QEMU smoke-test block now creates
  ``fake-emmc.qcow2`` before referencing it.

### Tests

- Add ``tests/test_pack_rpi_img.py`` -- regression coverage on
  the pure pieces of the arm64 Pi-image assembler (config.txt
  invariants, cmdline.txt format, partition-size constants,
  ``_locate_lb_output``'s preference for binary/live/).
- Gzip-wrap rejection test, ORAS Helm/Cosign refusal tests, TUI
  cancel-key validation test.

### Removed

- ``vN.NN+:`` historical version-prefix markers stripped from
  in-code comments + docstrings + template comments. Per
  pre-1.0 break-freely the codebase doesn't owe readers a
  comparison to a prior shape that no longer exists.

## [0.48.0] - 2026-06-12

Doc + observability sweep after v0.47.0. The /reference release-
artifacts table and the bty-media README now advertise the
``usb-rpi`` variant explicitly; a worker-loop safety net in
``_BaseAsyncManager`` keeps a buggy ``_run_one`` from wedging a
slot; an event-log backfill that bailed silently now leaves a
debug breadcrumb; and a dead CI matrix branch is removed.

### Added

- ``_BaseAsyncManager._worker`` (used by ReleaseFetchManager +
  BackupManager) wraps the per-key dispatch in a worker-loop
  safety net: if ``_run_one`` leaks any exception, the worker
  logs it, marks the state ``failed`` with a typed ``error``,
  and stays alive to pull the next key. Production subclasses
  already catch their own exceptions; this is defence-in-depth
  for the day one doesn't.
- ``_release_mgr.ReleaseFetchManager.backfill_from_events`` now
  emits a ``log.debug`` (with traceback) when it soft-fails. The
  soft-fail behaviour itself is unchanged (a corrupt events row
  or a freshly-created DB without the table is not fatal at
  startup); the breadcrumb just makes a real schema/data
  problem visible when debug logging is on.

### Fixed

- ``/reference``'s release-artifacts table now lists the
  ``bty-usb-rpi-arm64-v*.img.gz`` asset that v0.47.0 added.
  Previously operators browsing /reference saw only the x86
  artifacts and had no signal that the arm64 image existed.
- ``bty-media/README.md`` re-headers as "three variants",
  extends every ``make build VARIANT=...`` usage line to include
  ``usb-rpi``, retitles the "Build prerequisites" block from
  "usb-x86 + netboot-x86" to "All three variants", and corrects
  a stale ``--binary-images netboot`` caption to ``tar``
  (v0.47.0 dot-release switched arm64 away from ``netboot`` to
  dodge lb's x86-only tftpboot pipeline).

### Removed

- Dead ``Enable KVM access for the runner`` step from
  ``.github/workflows/ci-cd.yml``'s ``build-media`` job. The
  step gated itself on ``matrix.kind == 'disk'`` but the matrix
  only carries ``kind: live``; the step never ran.

## [0.47.0] - 2026-06-12

New ``usb-rpi`` arm64 image variant: a USB-bootable Raspberry-Pi
flasher (CM5 on IO-board, Pi5, Pi4) that runs the bty TUI on the
Pi itself and writes the operator's chosen catalog image onto
local eMMC or NVMe.

### Added

- ``bty-usb-rpi-arm64-v<version>.img.gz`` release asset. ``dd`` it
  to a USB stick, plug into a CM5 / Pi5 / Pi4, USB-boot, pick a
  catalog image + target disk in the wizard. Headline workflow:
  reflashing a CM5 in a closed IO-case (eMMC) without the
  jumper-rpiboot-Etcher disassembly dance, or reflashing a Pi5
  NVMe HAT in situ.
- ``BTY_VARIANT`` tri-state environment knob driving
  ``bty-media/live-build/auto/config``: ``netboot-x86`` (default,
  amd64 PXE trio), ``usb-x86`` (amd64 hybrid ISO), or
  ``usb-rpi`` (arm64 netboot trio + Pi-image packaging
  post-process).
- ``bty-media/scripts/pack_rpi_img.py`` assembles a 3-partition
  raw disk image from the lb arm64 output: FAT32 ``RPIBOOT``
  with the ``raspi-firmware`` blobs + kernel + initrd +
  ``config.txt`` (``dtparam=pciex1`` for Pi5 NVMe enumeration) +
  ``cmdline.txt``, ext4 ``BTY_LIVE`` holding the squashfs at
  ``/live/filesystem.squashfs``, exFAT ``BTY_IMAGES`` scratch
  (auto-grows on first boot via ``bty-usb-grow.service``).
- New CI job ``build-usb-rpi`` on a native arm64
  ``ubuntu-24.04-arm`` runner, gating ``tag-release`` and
  ``attach-to-release`` the same way ``build-usb-x86`` does.
- New tutorial ``docs/src/tutorials/bty-usb-rpi.md`` covering
  download, dd-to-USB, the CM5 / Pi5 / Pi4 BOOT_ORDER prereq,
  the on-Pi flow, and local QEMU-virt verification for
  developers.

### Fixed

- **TUI's ``[d] default`` catalog shortcut no longer 404s.** v0.46
  stopped publishing a bty-side ``catalog.toml`` mirror but the
  TUI's ``_BTY_DEFAULT_CATALOG_URL`` (the wizard's ``[d] default``,
  the ``make tui CATALOG=default`` developer shortcut, and the
  operator-facing reference docs) was missed and still pointed at
  the now-absent ``safl/bty/releases/latest/download/catalog.toml``.
  Repointed at the upstream image-builder's catalog
  (``safl/nosi/releases/latest/download/catalog.toml``) so the TUI
  matches the bty-web side.

### Changed

- ``BTY_USB_ISO=1`` env knob removed (pre-1.0 break-freely; the
  two ``cijoe/scripts`` callers are updated). Replaced by
  ``BTY_VARIANT=<variant>``.
- ``bty-media/live-build/config/package-lists/bty-base.list.chroot``
  split into arch-agnostic +
  ``bty-base.list.chroot_amd64`` (x86 kernel + microcode + DKMS
  build env) + ``bty-base.list.chroot_arm64`` (arm64 kernel +
  ``raspi-firmware`` + Pi WiFi firmware). live-build only
  consumes a suffixed file when the matching ``--architectures``
  is active.
- The r8125 DKMS hook
  (``0600-bty-r8125-dkms.hook.chroot``) early-exits on
  non-amd64; the bootloader-menu suppression binary hook
  (``0500-bty-skip-bootloader-menu.hook.binary``) early-exits
  when neither ``binary/isolinux/`` nor ``binary/boot/grub/``
  exists (the case on every non-iso-hybrid build).
- ``make build VARIANT=usb-rpi`` route added to the Makefile;
  ``make help`` advertises it.

## [0.46.0] - 2026-06-11

bty stops publishing its own ``catalog.toml`` mirror and consumes
the upstream image-builder's auto-generated catalog directly.
Default catalog source flips from ``safl/bty`` (a 7-variant hand-
maintained mirror) to ``safl/nosi`` (the 16-variant canonical
catalog that nosi's CI publishes on every release).

### Added

- ``Settings > Upstream sources`` gains a separate
  ``Catalog repo`` field (default ``safl/nosi``) alongside the
  existing ``Netboot repo`` field (default ``safl/bty``). The
  two are independent: an operator can fork bty for custom
  netboot artifacts while still pulling the upstream catalog,
  and vice versa.

### Changed (operator-visible)

- **Default catalog is now nosi's**, so the variants nosi
  publishes but bty's stale mirror did not (Proxmox, the lxc
  variants, ubuntu-2604-wsl, ubuntu-2604-docker, rpios-13
  headless + desktop, debian-13-desktop) appear out of the box.
- **Settings page**: the ``Release repo`` field is gone; in its
  place the two new fields above. An operator who had pinned the
  legacy ``upstream.release_repo`` override needs to re-pin
  through the two new knobs (pre-1.0 break-freely, no migration
  shim).

### Removed

- ``scripts/generate_catalog_toml.py`` and
  ``scripts/starter_catalog.toml.in``. The CI step that ran them
  and the ``catalog.toml`` release-asset upload. Bty's release
  pages drop one asset; operators pointing
  ``--catalog https://github.com/safl/bty/releases/latest/download/catalog.toml``
  at the bty release switch to
  ``https://github.com/safl/nosi/releases/latest/download/catalog.toml``.
- ``_settings_store.KEY_RELEASE_REPO`` /
  ``resolve_release_repo`` / ``default_release_repo`` and the
  ``_releases.DEFAULT_REPO`` alias. Their replacements are
  ``KEY_NETBOOT_REPO`` / ``KEY_CATALOG_REPO`` /
  ``resolve_netboot_repo`` / ``resolve_catalog_repo`` /
  ``default_netboot_repo`` / ``default_catalog_repo``, with
  ``DEFAULT_NETBOOT_REPO`` and ``DEFAULT_CATALOG_REPO`` as the
  built-in constants.

## [0.45.1] - 2026-06-11

Technical-debt sweep across six rounds: 15 separate findings, none
operator-visible on its own, all aimed at trimming the surface that
had accumulated since the v0.40/v0.42 cleanups.

### Fixed

- **Dashboard's "TFTP daemon running" row** no longer cross-flags
  a container deploy where bty-web has no visibility into the
  bty-tftp sidecar (advisory blue ``i`` when ``running_in_container()``
  + state is ``unknown``, kept as a warning on bare-metal).
- **Container deploys with ``-e BTY_SERVER_PORT=...`` overrides**
  now stay healthy: the Dockerfile's HEALTHCHECK probes
  ``BTY_SERVER_PORT`` (the canonical name) instead of the legacy
  ``BTY_WEB_PORT`` it had been pinning at the stale 8080 default.

### Changed (cleanup)

- **Legacy v0.42 env-alias table removed.** ``_LEGACY_ENV_ALIASES``
  (mapping ``BTY_WEB_PORT`` / ``BTY_STATE_DIR`` / ``BTY_MAX_UPLOAD_BYTES``
  / ... onto the canonical ``BTY_<SECTION>_<KEY>`` convention) was a
  one-release migration shim; it had outlived its window (we're now
  three releases past v0.42). Direct ``BTY_STATE_DIR`` /
  ``BTY_SESSION_SECRET`` / ``BTY_BACKUP_MAX_PARALLEL`` fallback reads
  in early-boot bootstrap paths are also gone. Operators upgrading
  from a v0.41 envvars deploy still get the one-shot
  ``bty-lab upgrade`` migration that translates envvars → bty.toml.
- The container Dockerfile + tests now consistently use the
  canonical names (``BTY_PATHS_STATE_DIR``, ``BTY_PATHS_BOOT_DIR``,
  ``BTY_SERVER_HOST``, ``BTY_SERVER_PORT``).
- ``docs/src/dependencies.md`` env-vars table rewritten for the
  v0.45 bty.toml shape; the obsolete ``BTY_CATALOG_MAX_PARALLEL`` /
  ``BTY_HASH_MAX_PARALLEL`` / ``BTY_MAX_UPLOAD_BYTES`` /
  ``BTY_IMAGE_ROOT`` rows are gone.
- Schema-rotation event summary in ``state.db`` (``system.schema.reset``)
  no longer claims to preserve "images under BTY_IMAGE_ROOT"; it
  describes the actual v0.40+ bytes-plane layout (withcache volume
  / oras registry).
- Stale comment cross-refs to v0.40-removed endpoints
  (``/catalog/downloads``, ``/catalog/hashes``, ``/catalog/cache/{name}``,
  ``PUT /images/{name}``) scrubbed from ``_events.py``, ``_app.py``,
  ``_security.py``, and ``_backup.py``.
- Em-dash-substitute ``--`` removed from inline prose comments in
  ``oras.py``, ``_portability.py``, ``_releases.py``, ``_withcache.py``,
  and ``_reqctx.py`` (per the repo's "no em-dashes, no ASCII --"
  prose rule).

### Defensive

- ``bty.deploy._detect_host_addr``'s LAN-IP UDP probe gains an
  explicit ``settimeout(5)`` matching the sibling probe in
  ``bty.web._config``; a pathological resolver cannot hang
  ``bty-lab init``.
- ``bty.oras._urlopen_retry`` (the OCI token / manifest fetcher)
  caps the response body at 10 MiB. A hostile registry returning
  a 500 MiB "manifest" now raises ``OrasError`` instead of
  consuming heap.

## [0.45.0] - 2026-06-11

Oras catalog entries now warm withcache the same way https entries
always have. The plan-endpoint and the dashboard's Check button
converge on one stored "canonical URL" per catalog row.

### Added

- New `catalog_entries.resolved_src` column carrying the canonical
  plain-HTTPS URL the row actually fetches from. For https sources
  this equals `src`; for `oras://` sources it's the registry blob
  URL (`https://<host>/v2/<repo>/blobs/sha256:<digest>`) resolved
  once at catalog import via `bty.oras.resolve_ref`. Pre-1.0
  schema change; existing DBs auto-rotate per `_db.py`'s
  schema-mismatch handler, so operators upgrading get a clean
  schema and re-import (or auto-import re-seeds from
  `catalog.toml`).
- `_withcache.is_cached` gains an optional `headers=` kwarg
  matching the new sibling withcache-0.4.0 contract.

### Changed

- **Dashboard "Check" button warms withcache for oras entries.**
  Previously the Check on an `oras://` row reported
  `withcache n/a` because withcache spoke plain HTTP only. The
  Check now reads the row's `resolved_src`, mints a fresh
  anonymous OCI bearer just-in-time, and HEADs withcache with
  `Authorization: Bearer ...`. Withcache 0.4.0+ forwards that
  header into its background fetch worker so a 401-gated blob
  actually fills the cache on the first probe.
- **PXE plan rewrite uses `resolved_src` for the cache key.**
  On a cache hit the plan returns withcache's `/b/<token>/` URL
  regardless of the original scheme; on a cold cache an https
  origin still flows direct, while oras stays on bty-web's
  `/images/{ref}` proxy (the live env can't carry an OCI bearer
  per fetch). The `cache_decision` recorded for `/ui/events` now
  distinguishes `served_from = withcache | origin | bty-web-proxy`.
- The "withcache n/a" badge survives only for catalog rows with a
  NULL `resolved_src` (file:// or a failed import).

### Requires

- The container deploy needs `ghcr.io/safl/withcache:0.4.0`
  (or `:latest` once the release rolls). The Authorization
  forwarding added there is what makes the oras warm-cache flow
  actually fetch instead of 401ing anonymously. Stock
  `bty-lab upgrade` pulls the latest tag.

## [0.44.3] - 2026-06-11

Deploy hotfix: bty-web and withcache containers now start on hosts
without the `nftables` package, plus a small dead-knob doc trim.

### Fixed

- **`bty-lab deploy` and `upgrade` now drop a
  `/etc/containers/containers.conf.d/zz-bty-firewall.conf` that pins
  podman/netavark to the iptables backend.** Without it, a freshly
  imaged Debian trixie host that ships only iptables (and not the
  optional `nftables` package) hit `netavark: nftables error: unable
  to execute nft: No such file or directory` and the bty-web /
  withcache containers exited 127 with no useful surface in their
  own logs. bty-tftp escaped because it uses host networking and
  doesn't go through netavark. `purge` removes the drop-in.

### Changed

- The dead `BTY_CATALOG_MAX_PARALLEL` and `BTY_HASH_MAX_PARALLEL`
  knobs are gone from `envvars.example` and the live envvars
  `bty-lab deploy` renders. The DownloadManager / HashManager
  subsystems they tuned were removed back in v0.40; the env vars
  outlived them in the docs only. `BTY_BACKUP_MAX_PARALLEL` stays.

## [0.44.2] - 2026-06-11

Live-env hotfix: r8125 module-loading shape on Secure-Boot hardware,
plus a small at-the-console triage tooling bump.

### Fixed

- **Live ISO on Secure-Boot hardware now keeps the NIC.** The previous
  `zz-bty-r8125-prefer.conf` aliased the RTL8125 modalias to the
  unsigned DKMS r8125 module, which the kernel rejects under
  `[integrity]` lockdown with EKEYREJECTED. With no fallback, the NIC
  stayed dark for the whole boot (observed on ASUS PN51-E1 stock
  firmware). The new shape is `softdep r8169 pre: r8125`: udev still
  loads the signed in-tree r8169, and r8125 is attempted as a soft
  pre-dep so it can still win the bind on a non-Secure-Boot G10.

### Added

- `pciutils` and `usbutils` baked into the live env so an operator at
  the console has `lspci -k` and `lsusb -t` for "why isn't this
  hardware visible?" triage without falling back to a /sys walk.

## [0.44.1] - 2026-06-11

Maintenance release: technical-debt sweep, no behaviour change for
operators beyond the items below.

### Changed

- The per-MAC boot-plan request (`pxe_plan`) now opens the state DB
  once per flash request instead of four times (catalog binding +
  withcache lookup share one connection).
- The live-env `systemctl reboot` and the host-IP autodetect socket are
  now bounded by timeouts, so a wedged systemd / resolver can't hang
  them.

### Internal

- Deduped `_normalise_mac` / `_client_ip` into a shared `_reqctx`
  module; `_safe_path` now routes its basename check through
  `_security.validate_basename`.
- Dropped dead code: the unused `host_addr` Quadlet param and the
  pre-1.0 `resolve_release_tag` / `DEFAULT_RELEASE_TAG` aliases.
- Doc / Makefile fixups (default password is `bty-lab`; `.PHONY`
  completeness).

## [0.44.0] - 2026-06-10

### Fixed

- **withcache was silently bypassed on container deploys.** The flash
  path's `resolve_withcache_url` read a DB override then
  `$BTY_WITHCACHE_URL` then nothing -- it never consulted
  `cfg.withcache.url`. v0.42 moved the URL into `bty.toml` and the slim
  compose/Quadlet stopped setting the env var, so a stock deploy
  resolved no URL: images streamed from origin, withcache got no HEAD
  and stayed empty even though `bty.toml` had the URL. The resolver now
  layers DB override > `cfg.withcache.url` > `$BTY_WITHCACHE_URL` > none.

### Added

- **Per-entry "Check" action on the Images page.** Each catalog row
  gets a Check button that probes, point-in-time, whether the origin is
  reachable (and how big) and whether withcache already holds it. The
  withcache HEAD also warms an auto-fetch withcache, so Check on a miss
  doubles as a one-click "start caching this" -- check again shortly and
  it flips to cached. A dead origin is reported inline, not as an error.
- **The withcache decision is now observable on the flash path.**
  `is_cached` logs each HEAD (info on hit/miss, warning on an
  unreachable cache), and the per-machine `netboot.pxe.plan` event
  records `withcache = {configured, hit, served_from}` -- visible in
  `/ui/events`.

## [0.43.1] - 2026-06-10

### Changed

- **The well-known default password is now `bty-lab` (was `bty`) for
  BOTH bty-web and withcache.** A stock `bty-lab deploy` writes it to
  `envvars` / `bty.toml` and prints it; change it before exposing the
  host. bty-web's runtime fallback (when nothing is configured) is the
  same value.

### Fixed

- **`bty-lab deploy --systemd` baked the wrong withcache password into
  the Quadlet unit.** The generated `withcache.container` hardcoded
  `WITHCACHE_ADMIN_PASSWORD=change-me` while the deploy summary +
  `envvars` advertised a different password, so operators following the
  printed credential were locked out of withcache. The deploy/upgrade
  paths now bake the chosen password into the unit; the stand-alone
  `init --systemd` reference unit keeps the editable placeholder.
- **Settings-page edits failed on container deploys.** Saving config
  rename-replaced onto the `bty.toml` bind mount, which fails with
  `EBUSY`; added an in-place-write fallback. The generated compose also
  mounted `bty.toml` read-only, contradicting the Quadlet's RW mount --
  dropped the `:ro`. Reference `deploy/compose.yml` +
  `deploy/quadlet/bty-web.container` refreshed to the v0.42 bty.toml
  shape (they still showed the old envvars-era env lines).
- Documentation corrected: the env-var tables and `/ui/login` text said
  `BTY_ADMIN_PASSWORD` unset meant "open access"; auth has been always-on
  since v0.41.3.

### Internal

- The two `/ui/images` form-post paths now record `catalog.entry.add.failed`
  on a duplicate add, matching the JSON endpoint (audit-trail parity).
- Tests for the v0.41 legacy env-alias contract; `actions/checkout@v6`
  across all CI jobs; assorted dead-code / stale-comment cleanup.

## [0.43.0] - 2026-06-10

### Added

- ``bty-lab purge [DEST]`` -- the inverse of ``deploy``: stops + removes
  the stack (auto-detects compose vs Quadlet/systemd, like ``upgrade``).
  Keeps ``data/`` and the deploy dir by default; ``--data`` deletes host
  state, ``--all`` also removes the deploy dir (implies ``--data``),
  ``--images`` drops the pulled images. Destructive flags are gated by a
  ``y/N`` confirm (skip with ``--yes``); teardown tolerates an
  already-gone service / container so a half-removed deploy still purges.
  Completes the ``deploy`` / ``upgrade`` / ``purge`` operator lifecycle.

### Fixed

- **The ``/ui/netboot`` TFTP probe no longer hard-codes ``127.0.0.1``.**
  It now resolves its target from config -- an explicit ``[netboot]
  tftp_probe_host`` if set, otherwise the host of the withcache URL (the
  LAN address clients reach, where the ``network_mode: host`` ``bty-tftp``
  sidecar serves udp/69). Previously the probe read a separate
  ``$BTY_TFTP_PROBE_HOST`` env var that the v0.42 slim-down dropped,
  silently falling back to loopback and reporting an otherwise-healthy
  TFTP server as unreachable. The ``[netboot] tftp_probe_host`` config
  key is now actually consulted (it was only displayed on the Settings
  page before), and the field default changed from ``"127.0.0.1"`` to
  ``""`` (= derive).
- The Settings-page DHCP/PXE cheatsheet suggests the configured advertised
  host (withcache URL host) for ``Next-Server`` instead of a sniffed
  container-internal interface, which inside a bridge-network container
  pointed at the wrong address.

## [0.42.0] - 2026-06-10

**bty-web operator config moves from env vars to ``bty.toml``.**

The v0.41-era ``envvars`` shell file and the ~17 ``$BTY_*`` env vars
it plumbed through ``compose.yml`` / Quadlet were a 12-factor relic
that didn't fit a long-running daemon. v0.42 makes ``bty.toml`` the
canonical config: one structured file the operator edits (or the
Settings page round-trips edits through), layered with per-key env
overrides for k8s Secrets / one-shot dev runs.

### Added

- ``src/bty/web/_config.py`` -- ``Config`` dataclass + nested section
  dataclasses covering every operator-tunable knob, with a layered
  loader (defaults < TOML files < env vars), per-key provenance
  tracking, and a ``tomlkit``-based writer that preserves operator
  comments + ordering across round-trips.
- ``--config PATH`` CLI flag for ``bty-web`` (repeatable; each later
  one overrides earlier per-key). Plus ``$BTY_CONFIG_FILE`` /
  ``$BTY_CONFIG_DIR`` env conventions.
- Drop-in config directories (``/etc/bty/conf.d/*.toml`` loaded in
  lexicographic order) following the nginx / sshd convention.
- ``BTY_<SECTION>_<KEY>`` env-override convention. Setting one key
  via env doesn't force the rest to be set.
- Default config search list when nothing is passed: ``/etc/bty/conf.d/``,
  ``/etc/bty/bty.toml``, ``<state_dir>/bty.toml``.

### Changed

- ``bty-lab deploy`` writes a populated ``bty.toml`` alongside
  ``envvars``. ``envvars`` shrinks to the compose-substitution
  basics (``HOST_ADDR``, ``WITHCACHE_ADMIN_PASSWORD``); the BTY_*
  knobs move to ``bty.toml``.
- ``compose.yml`` ``bty-web`` env block shrinks from ~10 entries
  to one (``BTY_CONFIG_FILE=/etc/bty/bty.toml``) + a bind-mount
  of ``./bty.toml`` into the container at ``/etc/bty/bty.toml``.
- Quadlet ``bty-web.container`` likewise: a single ``Environment=``
  + a ``Volume=`` of the absolute ``<dest>/bty.toml`` path.
- Settings page rows now surface the canonical ``BTY_<SECTION>_<KEY>``
  env names (e.g. ``BTY_PATHS_STATE_DIR`` instead of ``BTY_STATE_DIR``).
- ``bty-web --help`` description rewritten: documents the layered
  resolution + the override convention, drops the per-knob list.

### Compatibility

- **Legacy env names still work as aliases for one release.** The
  loader's ``_LEGACY_ENV_ALIASES`` table maps the v0.41 flat names
  (``BTY_STATE_DIR`` -> ``[paths] state_dir`` etc.) onto the new
  schema so an unmodified ``envvars`` keeps working. Removal
  scheduled for v0.43; new deploys + the Settings page surface only
  the canonical names.
- ``bty-lab upgrade`` preserves both ``envvars`` AND the existing
  ``bty.toml`` if present.

### Removed

- The per-knob ``Environment=`` lines in compose / Quadlet
  (``BTY_ADMIN_PASSWORD`` / ``BTY_SESSION_SECRET`` /
  ``BTY_MAX_UPLOAD_BYTES`` / ``BTY_BACKUP_MAX_PARALLEL`` etc.).
  Operators wanting per-key env overrides set them directly on the
  container (``-e BTY_<SECTION>_<KEY>=...``) rather than going
  through ``envvars``.

## [0.41.5] - 2026-06-09

**TFTP probe + withcache URL now reach the right host on container deploys.**

### Fixed

- ``BTY_TFTP_PROBE_HOST`` defaulted to ``127.0.0.1``, which on a
  container deploy resolves to bty-web's own loopback -- not the
  ``bty-tftp`` sidecar (which uses ``network_mode: host`` and binds
  udp/69 on the host's LAN address). The Netboot page's TFTP probe
  always reported "unreachable". The bty-web compose service +
  Quadlet unit now bake ``BTY_TFTP_PROBE_HOST=${HOST_ADDR}`` by
  default; operators can still override in envvars.
- ``_quadlet_bty_web`` previously left ``HOST_ADDR_HERE`` as a
  literal placeholder in the emitted Quadlet, since Quadlets don't
  expand env-file references the way Compose does. ``deploy_main``
  now detects ``host_addr`` BEFORE emitting and bakes the real LAN
  IP into the unit body; ``upgrade_main`` reads it back from the
  preserved ``envvars`` file. The stand-alone ``bty-lab init
  --systemd`` path (no host_addr known) still emits the placeholder
  + a hand-edit note in the header.

### Added

- Both ``BTY_WITHCACHE_URL`` and ``BTY_TFTP_PROBE_HOST`` are now
  override-able via envvars (compose entries use the ``${VAR:-
  default}`` form). envvars.example carries commented-out hints for
  each. ``bty-web --help`` documents both.
- Settings -> Network card surfaces ``withcache base URL`` +
  ``TFTP probe target`` rows so operators can see where bty-web
  is pointing without dredging the env.

### Removed

- Settings -> Network card drops the static ``TFTP systemd unit:
  dnsmasq.service`` row -- meaningless on container deploys, and
  the TFTP probe (now correctly targeted) is the canonical signal.

## [0.41.4] - 2026-06-09

**Polish pass on the v0.41.3 UI work.**

### Added

- ``make web`` -- fast iterate-locally target. Runs ``uv run
  bty-web`` straight from the source tree with state under
  ``/tmp/bty-web-dev``; skips the container entirely so rootless-
  Docker's uid-namespace woes don't block the day-to-day loop.
  Log in with the well-known default password ``bty``.

### Fixed

- The ``Fetch '<tag>' catalog`` / ``Fetch '<tag>' artifacts``
  buttons rendered the tag inside ``<code>``, which Bootstrap
  colours pink-red at ~2:1 contrast against the primary-blue
  button background -- effectively unreadable. Use ``<strong>``
  instead (plain bold, inherits the button's white text).

### Changed

- Settings page's compact read-only cards (Identity / Storage
  paths / Network / Background workers) reorder each row from
  ``LABEL`` / ``VALUE`` / ``ENV_VAR`` to ``LABEL`` / ``ENV_VAR``
  / ``VALUE`` -- matching the natural ``VAR=value`` shape an
  operator types into a shell.

## [0.41.3] - 2026-06-09

**Always-on auth + tag/repo separation + live TFTP probe + Settings grid.**

### Security

- **BREAKING**: auth is now always on. The prior "no
  ``$BTY_ADMIN_PASSWORD`` -> open UI" behaviour was a footgun --
  a fresh deploy or a forgotten env var left every mutating route
  open. ``$BTY_ADMIN_PASSWORD`` still overrides, but with the env
  unset the active password is the literal string ``"bty"``
  (well-known default, logged on startup + flagged in a yellow
  alert on the login form). Deploys that relied on the open-access
  default must set ``$BTY_ADMIN_PASSWORD`` to a real value AND/OR
  accept that operators log in with ``bty`` until they do.
- ``_auth.auth_enabled()`` is removed (auth is always on);
  ``admin_password()`` now returns a non-None string;
  ``using_default_password()`` powers the UI warning.

### Changed

- Settings: **release repo** + **catalog release tag** +
  **netboot release tag** replace the prior single "catalog URL"
  + "netboot release tag" pair. The catalog URL is derived from
  the repo + the catalog tag; pin them independently to flow
  catalog updates while keeping netboot artifacts on a known
  release (or vice-versa). The fetch buttons reword to
  **Fetch '<tag>' catalog** and **Fetch '<tag>' artifacts** so
  the active tag is visible at the click site.
- Settings layout: two-column row of editable cards
  (Upstream sources | Backup schedule), then a four-column row
  of read-only diagnostic cards (Identity | Storage paths |
  Network | Background workers), then the full-width DHCP /
  Network Boot cheatsheet. Compact list-group rendering replaces
  the wide 4-column tables in the read-only cards.
- ``/ui/netboot`` TFTP card now ships a live network probe:
  bty-web sends a TFTP RRQ for ``ipxe.efi`` to
  ``$BTY_TFTP_PROBE_HOST`` (default ``127.0.0.1:69``) and
  reports reachable / file-present as two independent signals.
  Localhost is the default probe target; override the env var to
  point at a TFTP daemon hosted elsewhere.

### Removed

- ``_settings_store.KEY_CATALOG_URL`` + ``default_catalog_url``
  (URL is derived now, not stored). ``KEY_CATALOG_TAG`` +
  ``KEY_NETBOOT_TAG`` take their place. ``resolve_release_tag``
  + ``DEFAULT_RELEASE_TAG`` survive as one-release aliases for
  ``resolve_netboot_tag`` + ``DEFAULT_TAG``; remove after v0.42.

## [0.41.2] - 2026-06-09

**UI consolidation + open-access nav fix.** Three things ship together:

### Fixed

- ``logged_in`` in the layout context was False on every page when
  ``$BTY_ADMIN_PASSWORD`` was unset -- so an open-access install
  (the default for a fresh deploy) rendered with NO nav buttons
  (Machines / Images / Netboot / Settings all gone). The render
  helper now treats "auth disabled" as logged-in so the nav cluster
  renders for the open-access path. The Logout button stays hidden
  on auth-disabled installs (no session to clear).

### Changed

- ``/ui/netboot`` absorbs the active release-fetch table + the
  Fetch artifacts trigger that used to live on ``/ui/downloads``.
  The legacy ``/ui/downloads`` route is removed; bookmarks that
  pointed there now 404. The navbar drops its standalone Downloads
  worker pill (release-fetch progress is on /ui/netboot directly).
- ``/ui/images`` header reorders to ``Add image | Upload catalog |
  Fetch latest catalog`` -- the most common operator add-path
  (single URL) lands first.
- ``/ui/dashboard`` Images Summary drops the ``Uploaded`` and
  ``Local copy`` rows. Total / HTTP / ORAS are the surviving
  counts now that bty-web is out of the image-bytes plane.
- TFTP daemon card on ``/ui/netboot`` is observation-only: status
  badge + a short triage hint pointing at ``systemctl status
  bty-tftp`` (container deploys) or ``journalctl -u dnsmasq.service``
  (host installs). The Start / Stop / Restart buttons + the
  ``/ui/settings/tftp-control`` POST route + the sudo'd
  ``bty-web-tftp`` helper concept are removed; daemon lifecycle is
  a systemd / Podman concern, not an operator click target.

### Removed

- ``/ui/downloads`` route + ``downloads.html`` template
- ``/ui/settings/tftp-control`` route
- ``_sysconfig.control_tftp`` + ``tftp_controllable`` + ``SysConfigError`` + ``TFTP_HELPER`` + ``TFTP_ACTIONS``
- ``netboot.tftp.controlled`` + ``netboot.tftp.control.failed`` event kinds

## [0.41.1] - 2026-06-09

**Hot-fix: ``bty-lab deploy`` in root mode left compose-managed
containers holding the ports the Quadlet services needed.**

Symptom on the lab box: ``[11/15] starting stack`` succeeded
(compose brought up ``bty_withcache_1`` / ``bty_bty-web_1`` /
``bty_tftp_1``), then ``[14/15] starting systemd services``
failed with ``Job for withcache.service failed`` + ``Job for
bty-web.service failed`` because the compose containers were
still holding ``:3000`` + ``:8080``.

### Fixed

- ``deploy_main`` in root mode no longer ``compose up``s. The
  ``compose pull`` stays (warms the registry cache); ``compose up
  -d`` is replaced by ``compose down`` (clears leftover compose-
  managed containers from a prior install -- idempotent on a fresh
  host). Quadlet + systemd then own the lifecycle.
- Non-root mode is unchanged: ``compose up -d`` remains the
  lifecycle there (no Quadlets to hand off to).
- ``upgrade_main`` was already correct -- only ``deploy_main`` had
  the bug.

Regression test ``test_deploy_as_root_does_system_install`` asserts
``compose up`` never appears in the root-mode runtime call sequence.

## [0.41.0] - 2026-06-09

**Cleanup release: ~2300 LoC of dead code + stale references removed
in the wake of v0.40's catalogs-not-bytes refactor.** No behavioural
changes for operators -- everything still works exactly as v0.40 did.

### Removed (code)

- ``/ui/images`` **Fetch / Update / Cache-delete buttons + their JS
  handlers** -- the underlying ``/catalog/downloads`` and
  ``/catalog/cache/{name}`` endpoints were deleted in v0.40; the
  buttons silently 404'd.
- ``/ui/downloads`` **Upload-image trigger** + ``uploadSelectedFile()``
  JS -- posted to ``PUT /images/{name}`` (deleted in v0.40).
- **Navbar worker polling** of ``/catalog/downloads`` +
  ``/catalog/hashes`` -- both 404 since v0.40; collapsed to the
  surviving ``/boot/releases`` + ``/workers/backups`` sources.
- ``bty.catalog`` orphans: the entire ``catalog-<ref:12>-<slug>.<ext>``
  naming machinery (``_CATALOG_PREFIX``, ``_CATALOG_REF_LEN``,
  ``_slugify``, ``local_filename_for``, ``is_catalog_cache_filename``,
  ``ref_prefix_from_cache_filename``), the storage-format marker
  (``StorageFormatMismatch``, ``check_or_write_storage_marker``,
  ``STORAGE_FORMAT_VERSION``, ``_STORAGE_MARKER_FILENAME``), the
  DownloadManager byte-pump (``is_cached``, ``fetch_to_cache``,
  ``fetch_src_to_cache``, ``_stream_with_digest``, ``CatalogCancelled``,
  ``ProgressCallback``, ``CancelCheck``),
  ``CatalogEntry.local_filename`` / ``.cached_path`` methods.
- ``bty.images`` orphans: ``merge_with_catalog``, ``ensure_sha256``,
  ``HashCancelled``, ``HashProgressCallback``, ``HashCancelCheck``.
- ``bty.web._app`` orphans: ``_lookup_db_catalog_entry`` (the
  DownloadManager DB-only fallback), the ``image_root`` no-op kwarg
  on ``create_app``.

### Removed (deploy / Makefile / Dockerfile)

- ``docker/Dockerfile``: ``ENV BTY_IMAGE_ROOT=/var/lib/bty/images``
  + the ``install -d`` line that pre-created the directory.
- ``Makefile`` ``docker-run`` target: stopped pre-creating
  ``bty-data/images/`` (now creates ``bty-data/boot/`` +
  ``bty-data/backups/``).

### Documentation

- ``walkthrough-catalog.md`` rewritten end-to-end for the v0.40
  model (no dir-scan, no Hash/Fetch buttons, no DownloadManager).
- ``flows.md`` audit-log + actions + safety-gates tables trimmed
  for deleted endpoints + event kinds.
- ``walkthrough-image-store.md`` + ``operations.md`` +
  ``walkthrough-server-docker.md`` had their first-pass rewrites
  in PR #11; this round finishes the long-tail.
- ``reference.md`` rewritten: deleted-endpoint rows out of the
  protected-routes table, ``BTY_IMAGE_ROOT`` scoped to the ``bty``
  CLI only, ``CatalogEntry.disk_image_sha`` comment realigned to
  "populated only when the publisher pinned it".
- Module / function docstrings across ``bty.catalog``,
  ``bty.images``, ``bty.flash``, ``bty.web._app``, ``_jobs``,
  ``_releases``, ``_backup``, ``_models``, ``_db`` updated to drop
  references to deleted symbols (``HashManager``, ``DownloadManager``,
  ``merge_with_catalog``, ``ensure_sha256``, ``fetch_to_cache``,
  ``fetch_src_to_cache``, ``local_filename_for``).

### Test churn

834 (v0.40) -> 803 tests. Net -31, all dead-test deletions
(``test_fetch_to_cache_*``, ``test_is_cached_*``,
``test_local_filename_for_*``, ``test_storage_marker_*``,
``test_recognised_filenames_*``, ``test_merge_with_catalog_*``,
``test_ensure_sha256_*``). Two UI-test assertions updated for the
trimmed navbar poll endpoints + the gone-Upload-image trigger.

## [0.40.0] - 2026-06-09

**bty-web is out of the image-bytes plane.** Image bytes now live
exclusively in [withcache](https://github.com/safl/withcache); bty-web
holds the catalog (URL -> manifest entry), the machine inventory, the
audit log, and the netboot artifacts. One rule: *bty has catalogs;
withcache has bytes.*

Released as a ~3500-line subtraction across five refactor commits.

### Removed

- **DownloadManager + ``/catalog/downloads`` endpoints.** The Fetch
  button on ``/ui/images``, the explicit-download lifecycle, the
  ``catalog.cache.populated`` / ``catalog.fetch.*`` audit events --
  all gone. The live env's flash request warms withcache directly via
  the HEAD probe in the plan endpoint.
- **HashManager + ``/catalog/hashes`` endpoints.** The background
  SHA-256 worker, the ``image.hashed`` / ``image.hash.*`` events, and
  the ``/ui/hashing`` page. ``catalog_entries.disk_image_sha`` stays
  for catalog-declared shas; no more late backfill.
- **Image upload** (``PUT /images/{name}``). No drag-and-drop in the
  UI, no curl-PUT, no ``image.uploaded`` events. Ad-hoc images: host
  on your own nginx / GHCR / S3 and add a catalog entry pointing at
  it. ``PUT /boot/{name}`` survives for netboot artifacts.
- **``BTY_IMAGE_ROOT``.** No image-store directory. bty-web's
  lifespan no longer dir-scans the filesystem; ``/var/lib/bty/`` is
  now ``state.db + boot/ + catalogs + session-secret`` only.
  ``BTY_CATALOG_MAX_PARALLEL`` and ``BTY_HASH_MAX_PARALLEL`` envvars
  retired alongside their managers.
- **``/ui/images`` Fetch / Hash buttons and the ``data-cached`` status
  badge.** Catalog entries render with their src URL and the catalog
  metadata only.

### Changed

- **Plan endpoint URL contract.**
  - HTTPS + withcache configured + warm -> withcache's
    ``/b/<urlsafe-b64(origin)>/<basename>`` URL (unchanged).
  - HTTPS + withcache cold or unconfigured -> the **origin URL
    directly** (was: ``/images/{ref}/{name}`` stream-proxy). bty-web
    is out of the bytes path for https sources entirely.
  - ORAS -> ``/images/{ref}/{name}``; bty-web proxies for the
    bearer-token resolve (withcache doesn't speak oras yet; v0.41
    follow-up).
- ``GET /images/{key}`` shrinks to oras-only stream-proxy. Unknown
  keys, https-only catalog entries, and literal filename lookups all
  404 now.

### Migration

Upgrade in place with ``bty-lab upgrade /opt/bty`` then re-deploy.
Existing files under ``./data/bty/images/`` are no longer read or
written; ``rm -rf`` them to reclaim disk after confirming the
withcache deploy serves the same URLs (HEAD them at
``http://<host>:3000/b/<urlsafe-b64(origin)>/<basename>``).

Operator-uploaded images that didn't live at a URL on the old deploy
need re-homing: drop them onto an HTTP server, push to GHCR via
``oras``, or any other URL-addressable store, then add a catalog
entry.

state.db survives untouched -- the ``catalog_entries.disk_image_sha``
column stays nullable; rows without it just lose the sha display.

### Test impact

922 -> 834 (-88). Dir-scan, sha-by-filename, local-file serving,
image-upload, image-store-survives-upgrade, mixed-shape-no-dupes,
and the entire DownloadManager + HashManager test files gone.

## [0.39.1] - 2026-06-08

**Hot-fix: two unrelated stock-Ubuntu-host gotchas that made v0.39.0's
``bty-lab deploy`` unusable in real-world reproduction.** Both surface
on a stock Ubuntu host with ``podman-compose`` installed but no other
podman / netavark deep-dive done.

### Fixed

- **``deploy`` and ``upgrade`` pre-create the bind-mount targets writable
  for any container UID.** withcache's image runs as USER ``app``,
  bty-web's as USER ``bty``. When podman auto-created ``./data/withcache``
  and ``./data/bty`` they came up root-owned mode 0o755 -- not writable
  for those non-root container UIDs. withcache crashed on
  ``Permission denied: '/data/blobs'`` and bty-web stuck at ``Created``
  via ``depends_on``. Fix: ``mkdir -p`` + ``chmod 0o777`` BEFORE
  ``compose pull/up``. World-writable is the right tradeoff here
  (single-tenant appliance host, the dir already holds operator-trusted
  bytes), and image USER directives stay respected.
- **Container DNS hardcoded to sidestep the systemd-resolved /
  missing-aardvark-dns combo.** Containers got ``nameserver 10.89.0.1``
  (podman's bridge gateway, where ``aardvark-dns`` is supposed to forward
  from -- but it isn't installed by default on Ubuntu). Result: every
  outbound lookup failed:
  ``release fetch failed: <urlopen error [Errno -3] Temporary failure in name resolution>``.
  Compose + Quadlets now emit ``dns: 1.1.1.1`` (overridable via the new
  ``BTY_DNS`` envvar for internal-resolver LANs). Bty's inter-service
  traffic already routes via host IP, so the earlier
  "aardvark-dns binary not found" warning is now genuinely cosmetic and
  ``apt remove aardvark-dns`` works without breaking the stack.
- Step counters bump 14 -> 15 (sudo-root path) and the equivalent in
  ``upgrade`` to include the new ``prepared data dirs`` step.

## [0.39.0] - 2026-06-07

**Polish pass on the v0.38.0 ``bty-lab deploy``, from real-world
reproduction on a fresh nosi-built lab host.** The headline fix is
the broken container-tag pin (``:v0.38.0`` vs the actual GHCR tag
``:0.38.0``) that made every deploy land in "manifest unknown".
Bundled with that: the deploy/upgrade UX gets quieter, more
auto-detecting, and self-cleaning so the canonical install is a
single ``sudo uvx bty-lab deploy /opt/bty``.

### Fixed

- **GHCR tag pin matches what CI publishes.** ``compose.yml`` and the
  Quadlet ``Image=`` lines previously emitted ``ghcr.io/safl/bty-web:v{version}``
  with a leading ``v``, but the publish job strips that prefix (the
  201 historical container tags are all ``0.x.y``, no ``v``). Every
  ``deploy`` failed with "manifest unknown" on bty-web + bty-tftp;
  only ``withcache:latest`` came up. Drop the ``v`` from all four
  template positions.

### Changed

- **`bty-lab deploy` auto-detects install mode from euid** (BREAKING:
  the previous ``--systemd`` flag is removed). Run as root: full
  system install (TFTP sidecar + Podman Quadlet units installed to
  ``/etc/containers/systemd/`` + systemctl autostart). Run as a
  regular user: compose-only install -- no TFTP, no autostart, with
  a loud "limitations" block at the end naming exactly what was
  skipped and the ``sudo`` re-run command to promote. The
  privileged side is what ``sudo`` already implies; ``--systemd``
  was redundant.
- **`bty-lab deploy` handles the deploy-dir prep itself** -- no more
  ``sudo mkdir -p /opt/bty && sudo chown "$USER:$USER" /opt/bty``
  preamble. Run as root, the dest is created if missing and the
  whole emitted tree is chowned back to ``$SUDO_USER`` so the
  operator can ``vim envvars`` without sudo afterwards.
- **`[N/M]` step numbering** on every ``deploy`` / ``upgrade`` step.
  Totals are computed up front from the auto-detected mode flags --
  14 steps for a root+sudo deploy, 10 for a user-mode deploy, etc.
  Operator sees position-in-run as the install streams.
- **`upgrade` follows the same root/user auto-detect.** Refuses
  cleanly when the stack is Quadlet-managed but the upgrade was
  invoked without root -- a plain ``podman compose up -d`` would
  race the systemd-managed containers.

### Docs

- **Sidebar wordmark removed** -- Furo's ``sidebar_hide_name: True``
  drops the redundant "bty" text next to the mascot logo. The H1 on
  the landing page also drops the ``bty -`` prefix; the logo + alt
  text identify the project on their own.
- **Tutorials fetch the release asset directly.** Ventoy / piKVM /
  JetKVM / BMC tutorials all referenced ``~/system_imaging/disk/...``
  (a build-host artifact path) -- now use the standard
  ``release.toml`` discovery pattern that ``quickstart.md`` already
  uses. Also fixes a stale ``.iso.gz`` filename in ``bmc.md`` --
  current releases ship uncompressed ``.iso`` only.
- **Docs landing CI badge fixed** -- ``ci.yml`` -> ``ci-cd.yml`` to
  match the actual workflow file. Was 404'ing in the sidebar.

## [0.38.0] - 2026-06-07

**`bty-lab deploy` + `upgrade` subcommands so first-boot of a bty
server is one command instead of a five-step chain.** The deploy
emits compose files, auto-fills `envvars` (HOST_ADDR detected from
the host's outbound-route IP; admin passwords default to ``bty``
matching the historic PAM convention; session secret stays random),
and runs ``podman compose --profile tftp pull`` + ``up -d``. With
``--systemd``, also installs Podman Quadlet units to
``/etc/containers/systemd/`` and starts the services (requires root).
``upgrade`` is the in-place version-bump path: regenerates compose
against the CLI's bty version, preserves ``envvars`` + ``data/``,
pulls + restarts -- auto-detecting Quadlet-managed vs compose-managed
stacks.

- ``uvx bty-lab deploy /opt/bty`` -- bring up bty-web + withcache in
  one command. Visual ``==> step: detail`` phase headers throughout;
  subprocess output (pull, compose up, systemctl) streams between
  boundaries.
- ``uvx bty-lab upgrade /opt/bty`` -- in-place upgrade. Detects
  Quadlet-managed stacks from installed units under
  ``/etc/containers/systemd/`` and uses ``systemctl daemon-reload``
  + ``restart`` in that case; otherwise ``podman compose pull`` +
  ``up -d``.
- ``bty-lab init`` (existing) now surfaces ``PermissionError`` from
  the dest mkdir with a ``sudo mkdir + chown`` hint instead of a
  bare traceback inside the ``uvx`` wrapper. Probes for a compose
  backend on PATH; warns if missing instead of failing with the
  cryptic "looking up compose provider failed" later. Runtime
  ``Next:`` block trimmed to three lines with ``--profile tftp``
  baked in, plus a pointer to ``bty-lab deploy`` for the one-shot
  path.
- Docs landing page (``docs/src/index.md``) simplified nosi-style:
  mascot moves to the sidebar via ``html_logo``, body trims to a
  two-paragraph tagline + toctrees.
- New Tutorials section: ``ventoy.md``, ``pikvm.md``, ``jetkvm.md``,
  ``bmc.md`` (Supermicro / iDRAC / iLO virtual media, with the
  license-paywall caveats up front).

### Operator notes

- The default admin password is ``bty``. Change it in
  ``/opt/bty/envvars`` before exposing the host past a trusted LAN.
- The deploy dir needs to be writable by the operator. The docs
  lead with ``sudo mkdir -p /opt/bty && sudo chown "$USER:$USER"
  /opt/bty`` so first-boot doesn't trip the silent-fail mode where
  ``$EDITOR envvars`` opens an empty buffer.
- ``init`` stays available for inspect-before-apply control: same
  files, no side effects.

## [0.37.0] - 2026-06-06

**Polish pass on the v0.36.0 ``bty-lab init`` bootstrap, from
real-world reproduction on a fresh nosi-built lab host.** All
operator-facing; nothing breaks for hosts already running
v0.36.0 except the values-file rename below.

- **BREAKING (early adopters only): the values file is now
  ``envvars``, not ``.env``.** ``bty-lab init`` writes
  ``envvars.example`` and the rendered compose / README walk the
  operator through ``cp envvars.example envvars && export
  COMPOSE_ENV_FILES=envvars && podman compose up -d``. Reason:
  ``.env`` is a dotfile -- invisible in ``ls`` -- and an operator
  scanning the deploy directory after the bootstrap couldn't tell
  whether the file existed. ``envvars`` (the Apache convention)
  shows up in plain ``ls`` and is self-describing.

  **Migration for an existing v0.36.0 deploy:** ``mv .env
  envvars`` then either ``export COMPOSE_ENV_FILES=envvars``
  once per shell or pass ``--env-file envvars`` on every
  ``podman compose`` invocation. Or re-run ``uvx bty-lab init
  --force .`` to regenerate the deploy directory with the new
  layout (state in ``data/`` survives).

- **Every operator-facing env var is now documented in
  ``envvars.example`` and plumbed through the compose env block.**
  Previously only ``HOST_ADDR`` / ``WITHCACHE_ADMIN_PASSWORD`` /
  ``BTY_HOST_DATA_DIR`` were surfaced; now ``BTY_ADMIN_PASSWORD``
  (gates the bty-web UI -- previously you had to grep the source to
  find this), ``BTY_BOOT_RELEASE_REPO``, ``BTY_TRUSTED_PROXY``,
  ``BTY_SESSION_SECRET``, ``BTY_MAX_UPLOAD_BYTES``, and the three
  ``BTY_*_MAX_PARALLEL`` knobs are documented (commented-out, with
  default values + a one-line rationale each) and the compose
  references each with ``VAR: ${VAR:-}`` so uncommenting in
  ``envvars`` immediately reaches the container. Tests pin both
  sides of the contract.

- **Quickstart chain no longer dies on an unset ``$EDITOR``.**
  Previously the rendered ``Next:`` hint and docs read ``cp
  envvars.example envvars && $EDITOR envvars && podman compose
  up -d``; on a fresh shell with ``EDITOR`` unset bash expanded
  that to ``envvars`` and tried to exec the values file,
  reporting ``-bash: envvars: command not found``. The chain now
  uses ``"${EDITOR:-vi}"`` so ``vi`` is the universal fallback.

- **Operator-friction hints in the runtime ``Next:`` line:** the
  hint now mentions the ``--profile tftp`` variant (BIOS-PXE
  clients; UEFI HTTP-Boot doesn't need it) and the
  ``pipx install podman-compose`` prereq (``podman compose`` is a
  wrapper that requires an external compose backend on PATH;
  with none installed the bootstrap errors with a seven-line
  "looking up compose provider failed" trace).

## [0.36.0] - 2026-06-05

**One-command container deploy: `uvx bty-lab init`.** No more cloning
the repo to grab `deploy/compose.yml`; bty now ships a dedicated
``bty-lab`` console script that emits a ready-to-run compose stack
pinned to its own version.

- **New ``bty-lab init [DEST]`` console script.** Writes
  `compose.yml`, `.env.example`, and a per-deploy `README.md` for a
  `bty-web` + `withcache` stack on any host that has `uv` (or `pipx`)
  installed -- no clone, no `--from` indirection. `bty-web` and
  `bty-tftp` image tags are pinned to the bty CLI version that
  produced the file, so the compose and the image bytes always match.
  Re-running with `--force` refreshes an existing deploy against a
  newer bty release. `--systemd` additionally emits Podman Quadlet
  units for boot-autostart. `--data-dir` re-roots state onto a chosen
  disk; default is `./data/{bty,withcache}` bind-mounted next to
  `compose.yml`. `--print` streams the compose to stdout for pipeline
  use.
- **``bty-lab`` is a standalone script, separate from ``bty``.** The
  flash wizard stays single-purpose (and a bare ``uvx bty-lab`` does
  NOT pull in Rich or FastAPI); the lab-init module imports nothing
  from the [tui] / [web] extras. A bare ``bty-lab`` (no subcommand)
  prints usage pointing at ``bty`` for the wizard, so somebody running
  ``pipx run bty-lab`` blind learns about the sibling commands.
- **First-boot needs no UI configuration step.** The emitted compose
  passes `BTY_WITHCACHE_URL=http://${HOST_ADDR}:3000` to bty-web,
  which auto-discovers withcache on every request -- the operator
  edits only `HOST_ADDR` + `WITHCACHE_ADMIN_PASSWORD` in `.env`.
- **Operator-visible state directories.** The deploy now uses host
  bind-mounts (`./data/bty/`, `./data/withcache/`) instead of named
  volumes; state is where the operator put it, easy to back up and
  migrate.
- **Documentation re-anchored around the new flow** -- the
  quickstart, walkthroughs, and `deploy/README.md` lead with
  `uvx bty-lab init` instead of "clone + `podman compose -f
  deploy/compose.yml`". The "lowest-barrier docker trial" framing is
  replaced by the canonical container deploy.

## [0.34.0] - 2026-05-28

**Robustness pass: clearer flash failures, sturdier disk discovery,
and a hardened image bake.** No behaviour change for a working
appliance; the wins show up when something goes wrong.

- **Failed qcow2 flashes now report the real reason.** A failed
  ``qemu-img convert`` previously surfaced only a numeric exit code;
  bty now captures qemu-img's diagnostic (``Could not open ...``,
  permission denied, corrupt-image) and includes it in the error,
  which is what an operator needs when a block-device write fails.
- **Disk discovery degrades gracefully** if ``lsblk`` returns
  unparseable JSON (a zero-exit-with-empty-output edge on cut-down
  busybox builds) instead of crashing the disk picker.
- **The server-image bake fails loudly instead of shipping a broken
  image.** ``diskimage_build`` now verifies cloud-init actually
  completed (gated on a marker echoed only if the whole ``set -eu``
  runcmd succeeded) and dumps the offending command on failure --
  and the r8125 DKMS build now targets the kernel whose headers are
  installed (the trixie-backports kernel the appliance boots) rather
  than a blind ``ls | head -1``, which on a kernel-version drift had
  silently shipped an appliance whose bty-web never started.
- Internal: corrected two inaccurate docstrings/comments (the audit
  ``record()`` commit contract and the backup-cancel event note) and
  strengthened /ui/images action-button + scheduler-audit test
  coverage.

## [0.33.30] - 2026-05-28

**Starter catalog refreshed for nosi's renamed variants.** nosi
renamed its images from ``<distro>-sysdev`` to numbered
``<distro>-<version>-<shape>``, so the shipped starter catalog
pointed at repos that no longer publish. The catalog now lists the
seven flashable variants (Debian / Ubuntu / Fedora / FreeBSD
headless, plus a Fedora desktop); docs and the USB-ISO build were
updated to match. No behaviour change to bty itself -- a fresh
install just gets a catalog whose entries resolve again.

## [0.33.29] - 2026-05-26

**Audit-log lifecycle for deferred operations + /ui/images UX
refinements.** Eleven commits split into two themes.

### Audit-log: requested -> started -> terminal lifecycle

The audit log used to capture only terminal events
(``image.hashed``, ``backup.created``, ``catalog.cache.populated``,
``netboot.artifacts.fetched``). For deferred ops (worker-backed),
this meant /ui/events showed nothing between the operator's click
and the worker's eventual outcome -- which can be minutes for
catalog fetches and release downloads.

Now each deferred op writes three lifecycle phases:

- ``.requested`` -- HTTP handler accepted the request and
  enqueued. Actor=``operator`` when the operator clicked
  (carries source_ip); actor=``system`` for internal triggers
  (the backup scheduler tick).
- ``.started`` -- Worker pulled the job off the queue and
  began work. Actor=``system``.
- ``.cancelled`` -- Operator-initiated stops now land in the
  audit log; pre-fix the manager flipped ``_states`` + fired
  SSE but wrote nothing to /ui/events. Both the HTTP DELETE
  handler (actor=operator + source_ip) AND the worker
  (actor=system, observed the cancel flag) emit it, so the
  operator can distinguish "I cancelled it" from a shutdown
  drain.

Concrete new kinds:

- ``catalog.fetch.requested`` / ``.started`` / ``.cancelled`` /
  ``.failed`` (also replaces the misleading
  ``catalog.fetch.sha_mismatch``, which fired for ALL fetch
  failures, not just sha mismatches)
- ``image.hash.started`` / ``.cancelled`` (no ``.requested``:
  the per-row Hash button was removed in this batch; every
  hash now arrives via auto-import or DownloadManager
  back-fill, both system-initiated)
- ``netboot.artifacts.fetch.requested`` / ``.started`` /
  ``.cancelled``
- ``backup.create.requested`` / ``.started`` / ``.cancelled``

Plus a one-time rename pass for consistency:

- ``image.hash_failed`` -> ``image.hash.failed``
- ``image.upload_failed`` -> ``image.upload.failed``
- ``catalog.entry.add_failed`` -> ``catalog.entry.add.failed``
- ``netboot.artifacts.fetch_failed`` -> ``netboot.artifacts.fetch.failed``
- ``netboot.tftp.control_failed`` -> ``netboot.tftp.control.failed``
- ``system.schema_reset`` -> ``system.schema.reset``

Pre-1.0 break-freely (no migration / back-compat shim).
Operators with custom event-log scrapers will need to grep
for the dotted names.

Additional fix: ``catalog.cache.populated`` now fires for BOTH
sha-pinned AND un-sha'd fetches. Pre-fix it only fired for
un-sha'd entries because the gate was tied to the
disk_image_sha backfill UPDATE. Sha-pinned downloads had no
terminal success in the audit log -- an operator scrolling
/ui/events saw .requested + .started with no closure. The
backfill UPDATE itself remains gated on entry.sha256 is None
(sha-pinned rows already carry the sha).

### /ui/images UX

**Always-render action buttons + new Update.** The per-row
action column used to hide buttons whose preconditions weren't
met. Now all four (Fetch, Update, Cache delete, Entry delete)
always render with consistent placement; each ``disabled`` when
its applicability gate is false, with a tooltip explaining
why. The operator scanning the column sees the same shape
every row.

**New "Update" button:** enabled iff a catalog entry has a
remote source AND a local copy exists. Click chains
DELETE /catalog/cache/{name} + POST /catalog/downloads -- the
natural workflow for rolling oras tags whose upstream changed.
Relies on the new ``catalog.fetch`` lifecycle so /ui/events
clearly shows the re-pull.

**Row-busy disable:** when ANY worker job (download or hash)
is queued/running for a row's name, ALL buttons on that row
disable. Cancellation goes through /ui/downloads or /ui/hashing
where each manager exposes its dedicated Cancel button.

**Hash button dropped.** Every path into image_root already
auto-enqueues a hash (PUT /images upload, lifespan
auto-import, DownloadManager fetch). The per-row Hash button
only helped a niche "ssh + scp into image_root at runtime"
workflow where "restart bty-web" is the accepted answer.

**Content SHA clickable to copy.** The cell renders the first
8 chars + a clipboard icon; click copies the full sha256 to
the clipboard via the modern Clipboard API (with a hidden-
textarea fallback for non-HTTPS contexts). A brief check-icon
swap confirms the copy. Pre-fix operators had to triple-click,
copy, and prune the trailing ellipsis manually.

**Local copy badge** clearer wording: column header renamed
``Cached`` -> ``Local copy``, cell values ``cached`` /
``available`` -> ``yes`` / ``no``. Dashboard pill renamed
the same way; the separate "Local" pill (which counts
operator-uploaded entries by source-kind, distinct from
"has local bytes") renamed to "Uploaded" to disambiguate.

### Bug fix

**``DownloadManager.enqueue`` re-runs when the cache file is
missing.** Operator-reported (v0.33.15): deleting the cached
local copy of an oras-src image, then clicking Fetch, briefly
flipped the button to "Downloading..." but no worker actually
ran -- the dedup branch returned the stale "completed" state.
Now the dedup branch verifies the cache file still exists for
``completed`` entries; if it's gone, fall through to a fresh
enqueue.

Suite 879 -> 884.

## [0.33.28] - 2026-05-26

**Deep-pass cleanup on PXE state machine, audit log honesty, and
HashManager.** Seven distinct findings from a state-changes deep
audit, batched into one release.

### Crashed-flasher / crashed-live-env self-healing (F1)

The `/pxe/{mac}` consume of `saw_flasher_boot` now gates on the
completion signal, not the arm bit alone. Pre-fix, the bit alone
gated the sanboot serve. If the live env crashed between fetching
`/boot/...?mac=` (which arms the bit) and posting its completion
signal (`/pxe/{mac}/inventory` for inventory mode, `/pxe/{mac}/done`
for flash modes), `bty-web` happily sanbooted the (empty /
half-flashed) disk. Two failure modes followed:

- `bty-flash-always` and `bty-inventory`: one wasted sanboot cycle
  per crashed live env -- the box couldn't boot the disk,
  power-cycled, the next `/pxe` cleared the bit, then re-served
  the chain. Self-recovered, but a visible operator-facing burp.
- `bty-flash-once`: TERMINALLY stuck on the half-flashed disk.
  The mode's "stop after one flash" contract made the next `/pxe`
  STILL serve sanboot of the bad disk. Required operator
  intervention (re-save the machine) to re-arm the flash.

Post-fix: `armed && completion_signal` gates the sanboot.
Armed-without-completion treats the live env as crashed and
re-serves the chain. Self-healing without operator intervention;
`bty-flash-once` retries until `/done` lands. The retry serve is
distinguishable in the audit log via `netboot.pxe.offered`
details: `retry_after_armed_no_done: true` (flash modes) or
`retry_after_armed_no_post: true` (inventory).

### Race-safe `is_new` discriminator for discovery upsert (F2)

Pre-fix, the discovery handler used
``INSERT ... ON CONFLICT DO UPDATE RETURNING *, (created_at = ?) AS is_new``.
The row race itself was safe (v0.33.6), but the ``is_new``
discriminator did a timestamp compare -- which could TIE on hosts
with low-resolution clocks (some VMs, slow virtualised guests).
Two concurrent requests whose ``_now_iso()`` produced the same
string both saw ``is_new=1`` and both logged a
``machine.discovered`` event for the same MAC.

Post-fix: split into ``INSERT ... ON CONFLICT DO NOTHING RETURNING
1`` + unconditional UPDATE. The RETURNING row materialises iff
the insert actually fired (DO NOTHING suppresses it on conflict)
-- timestamp-independent, the canonical race-safe "did I create
the row?" signal in SQLite. The UPDATE then refreshes
``last_seen_*`` and ``COALESCE``s ``discovered_at`` so PUT-created
rows still backfill on first /pxe contact.

### /pxe handler: 6 sqlite connections -> 2 (F3)

The /pxe/{mac} handler used to open up to six separate sqlite
connections per request: discovery upsert, ``saw_flasher_boot``
clears in two policy branches, two flash-failure events, and the
always-runs ``pxe.offered`` event. Each ``open_db()`` runs schema-
init / PRAGMA setup and creates an implicit transaction; six per
PXE hit was gratuitous on the hottest server route.

Refactored to gather any saw_flasher_boot clear via a flag set
during the policy decision, then apply it alongside the offered
event in one final transaction. Two connections per request.

### `machine.discovered` event payload symmetry (F4)

The audit log's ``machine.discovered`` event used to land without
a ``details`` payload. Sibling events (``machine.created`` /
``machine.upserted``) carry a five-key payload (bty_image_ref,
boot_mode, sanboot_drive, hostname, target_disk_serial); an
operator pivoting on a MAC across the audit log saw a missing-keys
surprise on the discovery row. Now: the discovery emit (both
``/pxe/{mac}`` and ``/pxe/{mac}/plan``) carries the same five
keys. At discovery time only ``boot_mode`` has a value (the
auto-default ``bty-inventory``); the rest are explicitly NULL.

### Drop redundant flash-failure events (F5)

``netboot.pxe.flash.orphan_ref`` and ``netboot.pxe.flash.no_target_disk``
used to land as standalone events. But the always-runs
``netboot.pxe.offered`` event already carries ``reason:
orphan_ref`` / ``reason: no_target_disk`` in its details payload
-- the standalone events were duplicates. Dropped both from
``KNOWN_EVENT_KINDS``; operators tracking flash failures should
pivot on ``netboot.pxe.offered`` events where ``details.reason``
is set.

### HashManager: skip catalog cache files on startup (F12)

The lifespan walks ``BTY_IMAGE_ROOT`` and queues a hash job for
every file without a ``.sha256`` sidecar. Catalog-fetched cache
files (``catalog-<ref:12>-<slug>.<ext>``) are special: the
DownloadManager computes the sha while bytes flow during fetch
and writes it straight to ``catalog_entries.disk_image_sha`` (no
sidecar). They were getting re-hashed on every startup, wasting
I/O and (on a Pi-class box with multi-GiB images) blocking the
operator binding flow behind a redundant queue. Now skipped:
``is_catalog_cache_filename`` gates the enqueue.

### HashManager: backfill catalog row on manual hash (F13)

When an operator manually triggers a hash of a catalog-cache file
(``POST /catalog/hashes/<name>``), the HashManager terminal
callback's previous UPDATE matched ``WHERE src = 'file://<name>'``
-- which wouldn't find the owning catalog row (its src is the
upstream URL, not ``file://catalog-...``). A second UPDATE
matches by the 12-hex ``bty_image_ref`` prefix encoded in the
cache filename when the first one returns rowcount=0. Lands the
sha on the right row even when the row's src has nothing to do
with the local cache filename. New helper
``bty.catalog.ref_prefix_from_cache_filename``, mirror of
``local_filename_for``.

### /pxe/{mac}/inventory heartbeat (F6)

The inventory POST used to update ``known_disks_at`` without
touching ``last_seen_at`` / ``last_seen_ip``. A machine in
``bty-inventory`` mode that POSTed inventory and then sat at the
wizard showed a stale ``last seen X minutes ago`` on
/ui/machines, even though the live env was clearly alive minutes
ago. Now the UPDATE refreshes the heartbeat alongside the
completion signal.

### /boot/{name} heartbeat on every fetch (F7)

The ``/boot/{name}?mac=`` arm path's UPDATE was gated on the
0->1 transition (which is correct for ``saw_flasher_boot``) but
that also meant idempotent re-arms (kernel + initrd + squashfs
in one boot) didn't touch ``last_seen_at``. Same for machines in
``ipxe-exit`` / ``bty-tui`` mode whose policy filter blocks the
bit UPDATE entirely -- their /boot fetches were heartbeat-
invisible. Now split into an unconditional ``last_seen``
UPDATE + the existing bit-gated transition UPDATE so every
fetch refreshes the operator's view.

### Operator rebind clears completion signals (F8)

PUT /machines/{mac} (and the matching UI form upsert) already
reset ``saw_flasher_boot`` on policy-affecting changes
(boot_mode, bty_image_ref, target_disk_serial) via a CASE WHEN
expression. The completion signals (``last_flashed_at``,
``known_disks_at``) used to survive the same edit, which re-opened
the failure mode F1 closed: stale ``last_flashed_at`` + a future
crashed flasher cycle = the /pxe consume gate saw armed=True AND
has_flashed=True (from the OLD cycle) and sanbooted a
half-flashed disk. Now the same CASE WHEN clears both completion
signals on policy change; hostname / sanboot_drive remain
cosmetic edits that preserve the signals.

### Audit events for orphan /done and /inventory (F9)

A live env POSTing /done or /inventory for a MAC bty-web has no
row for (operator deleted mid-cycle, MAC from a foreign live env,
direct endpoint poke) used to 404 silently with no audit trail.
Now both 404 paths log a ``pxe.client.orphan`` event with
``details.signal`` in ``{"done", "inventory"}`` so the operator
can correlate "this MAC tried to report; we have no row" on
/ui/events.

### Round 3 (uncovered ground)

### DownloadManager backfill keys on src not name (F10)

The catalog.cache.populated backfill UPDATE matched by
``WHERE name = ?``. ``name`` is a free-text display label with
NO UNIQUE constraint in catalog_entries -- two operator-curated
rows for different upstream URLs that happened to share a
display name (``debian.iso`` from different mirrors) would BOTH
have their disk_image_sha clobbered by a single completed
fetch. Now keyed on ``src`` (the immutable source URL the
CatalogEntry was built from). Regression test pins the
name-collision scenario.

### settings.upstream.updated captures old + new (F11)

The settings.upstream.updated event used to record only the
post-change values (in the summary string, not details). An
operator auditing "what was the catalog URL before?" or a
drift-tracking script comparing successive events had no
before/after visibility. Now the event's details dict carries
``{release_repo, catalog_url, release_tag}`` -> ``{old, new}``
for every save.

### Lifespan teardown order: drain workers before closing the bus (F14)

The lifespan finally block used to call ``event_bus.close()``
FIRST, then await ``stop()`` on each manager. Final worker
state-changes (a hash that completes 100ms before SIGTERM)
saw ``loop.is_running()`` still True and
``call_soon_threadsafe`` succeeded, but the loop was already
past the point where SSE subscribers would drain it -- the
event got dropped silently. Reordered: stop the four managers
(download / hash / release-fetch / backup) FIRST, THEN close
the bus. The backup scheduler task still wakes first via its
event so the loop body exits before SIGKILL window.

Suite 861 -> 879.

## [0.33.23] - 2026-05-26

**Audit-log event for the saw_flasher_boot 0->1 transition.** The
last silent state transition gets an event.

Pre-fix, every state change in the machine lifecycle landed an
audit-log event EXCEPT the `/boot/{name}?mac=X` arm of
`saw_flasher_boot`. Operators following a machine's timeline on
`/ui/events` had to correlate raw /boot artifact fetches (not
machine events) with the next `/pxe` contact's offer_kind to
deduce when the live env actually ran.

`/boot/{name}?mac=X` now logs `netboot.flasher.armed` -- ONLY on
the 0->1 transition. The UPDATE WHERE clause restricts the write
to `saw_flasher_boot = 0` so idempotent re-arms (kernel + initrd
+ squashfs all hit the route in one live-env boot) are no-ops on
the bit AND don't spam the event log. Combined with the v0.33.22
state-label honesty fix, the timeline now reads:

  - `machine.discovered`       (first /pxe contact)
  - `netboot.pxe.offered`      (per /pxe hit, with offer_kind)
  - `netboot.flasher.armed`    (live env booted into the box)
  - `machine.inventory`        (live env POSTed disks)
    OR `machine.flashed`       (live env POSTed /done)
  - `netboot.pxe.offered`      (next /pxe, served sanboot)

Two new regression tests in `test_web.py`:

- one /pxe arm cycle that hits /boot three times must emit
  exactly ONE armed event (not three)
- machines in `ipxe-exit` / `bty-tui` modes that don't consume
  the bit MUST NOT log an armed event (the arm WHERE clause
  skips them)

Suite 859 -> 861.

## [0.33.22] - 2026-05-26

**Two state-machine fixes operator-pointed in.**

### State-label honesty (the operator's report)

Pre-fix: `bty-inventory` machines showed "inventoried; booting
disk" the moment `saw_flasher_boot` flipped to 1 -- i.e. when
the iPXE chain pulled `/boot/kernel?mac=X`, BEFORE the live env
had a chance to run `bty` or POST `/pxe/{mac}/inventory`. The
label lied for the seconds-to-minutes between iPXE chainload
and the actual inventory POST.

Same bug shape for flash modes: "flashed; booting disk" fired
on the iPXE arm, not on `/pxe/{mac}/done`.

`bty.web._app._boot_state()` now requires the matching
COMPLETION signal:

- bty-inventory + armed + `known_disks_at IS NOT NULL`
  -> `inventoried; booting disk`
- bty-flash-* + armed + `last_flashed_at IS NOT NULL`
  -> `flashed; booting disk`
- armed but no completion signal yet -> new honest label
  `live env running; awaiting <inventory|flash>`
- not armed -> `pending <flash|inventory>` / `ready to flash`

### Selective `saw_flasher_boot` reset on upsert

Pre-fix: ANY `PUT /machines/{mac}` (or the UI form upsert) reset
`saw_flasher_boot` to 0 -- including pure-cosmetic changes like
hostname or sanboot_drive. An operator renaming a box mid-flash
silently interrupted the in-flight cycle; next /pxe served the
flash chain instead of the post-flash sanboot.

Post-fix: the reset is gated by a `CASE` that fires only when
a CYCLE-INVALIDATING field changes:

- `boot_mode` (intent changed)
- `bty_image_ref` (bound image changed; sanbooting the disk
  holding the OLD image is wrong)
- `target_disk_serial` (target changed; sanbooting an
  unflashed-by-this-cycle disk is wrong)

`hostname` and `sanboot_drive` are display / boot modifiers that
don't invalidate the cycle. The same fix applies to the JSON
`PUT /machines/{mac}` and the UI form `POST /ui/machines/{mac}`
paths -- both must stay in lockstep.

### Tests

Five new tests in `test_web.py` covering the upsert matrix
(boot_mode change, ref change, target change all reset; hostname
change + sanboot_drive change preserve), plus two new tests in
`test_web_ui.py` pinning the three-state label transitions
(pending -> live env running -> done) for both inventory and
flash modes.

Suite 854 -> 859.

## [0.33.21] - 2026-05-26

**Add storage-marker assertion to the QEMU PXE chain test.** The
v0.33.19 storage-format marker is written by bty-web's lifespan;
unit tests verify the helper, but no QEMU-level test confirmed the
marker actually lands in a real server VM after a real boot.

The cijoe `pxe_run_chain_test.py` now SSHes back into the server
VM after the chain succeeds, reads
`/var/lib/bty/images/.bty-storage.json`, and asserts
`format_version == 1`. A regression in the lifespan write (skipped
on certain paths, written to the wrong directory, malformed JSON)
fails the PXE chain test loudly rather than just the unit tests --
the operator-visible failure mode is "appliance boots but rejects
its own image_root on the NEXT restart", which is exactly the
class of thing only a real-boot test catches.

The expected version (1) is hardcoded in the cijoe script; if the
on-disk layout actually changes the operator-bumps
`STORAGE_FORMAT_VERSION` in `bty.catalog` AND the literal in the
PXE test in lockstep.

## [0.33.20] - 2026-05-26

**`bty-state-init` tool.** Sibling of `bty-state-migrate`: wipes,
partitions (GPT), formats (ext4, label `BTY_IMAGE_STORE`), and
mounts a target disk at `/var/lib/bty`. Differs from migrate by
NOT copying the existing state -- it's for starting fresh.

### Why two tools

- `bty-state-migrate /dev/sdX` -- relocate the current state to a
  separate disk. Copies `/var/lib/bty` contents onto the new disk
  before mount.
- `bty-state-init /dev/sdX` -- prepare a fresh disk, discard any
  existing state. Leaves the mount empty; bty-web's first-start
  lifespan stamps `state.db` + writes `images/.bty-storage.json`
  so the populate path lives in one place (the v0.33.19 storage
  marker logic).

### Safety rails (same as migrate)

- refuses to format the rootfs disk
- refuses a device with currently-mounted partitions
- refuses when `/var/lib/bty` is already a separate mount
  (`bty-state-init` is more conservative than migrate's no-op:
  unmount first if you really want a reset)
- confirmation prompt unless `--yes`

### Tests + docs

- `tests/test_state_init.py` -- subprocess smoke tests for the
  argument parser + non-destructive validation rails
  (help, no-args, unknown-flag, non-root / non-block-device)
- `docs/src/walkthrough-image-store.md` carries a new
  "Starting from scratch with a fresh disk" section explaining
  when to use which tool

Suite 850 -> 854.

## [0.33.19] - 2026-05-26

**Storage format version + inventory-format decoupling.** Operator-
pointed: the on-disk layout needs a version number independent of
`bty.__version__`, and unconventional files in image_root should
warn loudly so the operator notices.

### Added

- **`bty.catalog.STORAGE_FORMAT_VERSION` = 1.** Constant naming
  the on-disk layout / filename grammar version. Independent of
  the bty package version; bumped ONLY when the convention
  changes (filename pattern, sidecar shape, etc.). v1 covers the
  v0.31.0+ scheme (`catalog-<ref:12>-<slug>.<ext>` for cache files,
  operator-typed for the rest, `.sha256` sidecars, `.partial`
  upload-in-progress, mid-fetch tempfiles).
- **`.bty-storage.json` marker.** Written into `image_root` on
  first bty-web start; carries `{format_version, created_at,
  created_by_bty_version}`. Read on every subsequent start.
- **`bty.catalog.check_or_write_storage_marker(image_root)`.**
  Idempotent on matching version. On mismatch raises
  `StorageFormatMismatch` with operator-facing remediation text
  (drop to a shell with Alt+F2, archive image_root, restart). On
  malformed JSON (interrupted write, etc.) same error.
- **`bty.catalog.is_recognised_image_store_filename(name)`.**
  Predicate listing every legal name shape. Used by the lifespan
  startup to scan image_root + log warnings for unconventional
  files (operator notes, half-downloaded tools, stray scripts) so
  they're visible rather than silently ignored.

### Clarified

- **`bty_export_version` in inventory.json is independent of
  `bty.__version__`.** Documented in `_portability.py`: the
  number bumps ONLY when the `inventory.json` shape changes,
  NOT on every bty release. A v3 bundle written by bty v0.33.2
  must remain importable by any future bty release that still
  understands v3. Pre-1.0 policy: bundles don't migrate across
  major-format bumps; import refuses any version != current.

### Tests

Six new in `tests/test_catalog.py`:

- marker written on fresh image_root
- marker idempotent on matching version (doesn't drift created_at)
- StorageFormatMismatch on stamp mismatch (+ remediation text)
- StorageFormatMismatch on malformed JSON
- recognised-filename predicate accepts documented shapes
- recognised-filename predicate rejects operator-droppings

Suite 844 -> 850.

### Deferred

`bty-state-init` CLI tool (operator-typed: wipe + format + populate
a fresh state disk) is bigger; queued for a follow-up round once
the storage-format-version pattern proves out under hardware
testing.

## [0.33.18] - 2026-05-26

**/pxe/{mac}/plan contract tests for the operator-facing edge cases.**
Operator framing: most hardware bugs are QEMU-testable; the
appliance contract surfaces (plan endpoint) is exactly that.

### Pinned three plan-shape invariants

- **Extensionless catalog name -> URL filename synthesis.** An oras
  catalog entry's title is the layer annotation, typically a
  descriptive string with no file extension
  (`"nosi fedora-sysdev (x86_64, rolling)"`). The live env's bty
  detects format from the URL's last segment; an extensionless
  URL gets "format not recognised" + flash refused. The handler
  synthesises `image.<fmt>` for the URL while keeping the
  descriptive title in the plan's `name` field. Pinned.
- **Real filename round-trips unchanged.** When the catalog name
  HAS a detectable extension (`demo.img.gz`), the URL keeps it
  verbatim. The synthesis triggers ONLY on the extensionless case.
- **Orphan-ref plan falls back to interactive.** Operator deletes
  a catalog entry while a machine is bound to its ref. The bound
  machine's `/pxe/{mac}/plan` MUST NOT 500 -- it returns
  `mode=interactive` so the live env's wizard lets the operator
  pick another image.

### Coverage

Suite 841 -> 844.

## [0.33.17] - 2026-05-26

**Appliance upgrade path: integration test caught a real bug.**
Operator-pointed: test the path where an existing appliance gets
reflashed but the state disk (state.db, images/, backups/) survives.

### The bug

Writing the end-to-end integration test surfaced a fourth contract
that nothing pinned:

- Old appliance ran for a while; image_root has a
  `catalog-<ref:12>-<slug>.<ext>` cache file the old release fetched
  + a `.sha256` sidecar the old HashManager wrote.
- OS gets reflashed; the state disk survives untouched.
- New bty-web starts: state.db rotates (contract #1 -- pinned).
- Operator re-adds the catalog entry by URL (the catalog table got
  rotated out with state.db).
- New row's `disk_image_sha` is NULL because the operator didn't
  give a sha_url. The cached file is on disk; the sidecar carries
  its actual sha.
- **Pre-this-fix**: `merge_with_catalog` populated the
  `UnifiedImage.sha256` from `entry.sha256` (NULL) -- and
  `/images` defensively drops `cached=True + sha=None` rows
  because it can't build the `/images/<sha>/<name>` URL without
  the sha. The cached entry vanished from `/ui/images`. The
  operator would think the upgrade lost their cached images and
  re-download them.

### The fix

`bty.images.merge_with_catalog` pass 2 now reads the cached file's
`.sha256` sidecar when `cache_hit=True` and `entry.sha256 is None`.
The sidecar is the canonical content hash; the catalog row's NULL
is just "we haven't observed it via our own download path." After
the fix, the upgraded appliance recognises every cached image the
old release left behind.

### Added tests

- `test_appliance_upgrade_with_persistent_state_disk` -- the full
  E2E scenario in `test_web.py`. Old appliance state seeded with
  v0.30.0 marker + a bound machine + a cached catalog file +
  operator-typed image + pre-upgrade export bundle. New bty-web
  starts, rotates state.db, auto-imports operator-typed, refuses
  to auto-import catalog-prefixed (v0.33.1), import bundle restores
  hardware identity, operator re-adds catalog entry, /images
  recognises the cached file via sidecar (the bug above).
- `test_merge_with_catalog_picks_up_sidecar_for_uncached_entry` --
  unit-level regression test in `test_images.py` pinning the
  sidecar-read invariant so a refactor can't silently regress.

### Operator impact

Upgrade the appliance + reflash the OS disk. After bty-web starts,
re-add catalog entries by URL (no sha_url needed). The previously-
cached images surface as `cached=True` in `/ui/images` because
their sidecars carry the canonical hash. No re-downloads.

## [0.33.16] - 2026-05-26

**Full reflash-cycle state-machine tests.** Operator-pointed:
"how about the challenging re-flash-application-path?". This IS
the reason bty exists, and only one slice (operator-upsert resets
the bit) had explicit coverage. v0.30.2 fixed a real "flash-once
behaved like flash-always" bug; nothing pinned that contract.

### Added (6 tests)

End-to-end state-machine coverage in `tests/test_web.py`:

- **`test_reflash_lifecycle_bty_flash_always_alternates`** -- full
  cycle: PXE serves flash chain (bit=0) -> `/boot/{name}?mac=`
  arms bit -> next PXE serves sanboot AND clears bit -> next PXE
  serves flash chain again. The clear-on-consume is what makes
  the alternation work; the bit re-arms on the next /boot fetch.
- **`test_reflash_lifecycle_bty_flash_once_is_terminal`** -- the
  v0.30.2 regression test: after one flash + /boot arm, every
  subsequent /pxe must serve sanboot WITHOUT clearing the bit.
  Terminal state. Operator re-saves the machine to start a fresh
  flash cycle.
- **`test_pxe_done_does_not_mutate_boot_mode`** -- the v0.25
  mode/state contract: `/pxe/{mac}/done` updates last_flashed_at
  + logs `machine.flashed` but MUST NOT mutate boot_mode (mode is
  intent; state is the bit).
- **`test_boot_fetch_does_not_arm_sanboot_machine`** -- the WHERE
  clause in `_arm_flasher_boot` confines arming to flash + inventory
  policies. A stray `/boot?mac=` for an ipxe-exit machine MUST
  NOT leak the bit (would surprise the operator if they later
  switched policy).
- **`test_boot_fetch_arms_bty_inventory`** -- inventory mode
  also consumes the bit (boot live env, post inventory, sanboot,
  recycle). Pin that `/boot?mac=` does arm for bty-inventory.
- **`test_reflash_lifecycle_pxe_offered_event_per_iteration`** --
  two full cycles produce exactly 4 `netboot.pxe.offered` events
  in the audit log with alternating offer_kind. The operator's
  visibility into "did the box come back?" is the audit timeline;
  a refactor that drops events on the sanboot branch is caught.

### Coverage

Suite 833 -> 839. The reflash state machine -- the actual
appliance contract -- is now pinned end-to-end.

## [0.33.15] - 2026-05-26

**Edge-case audit + targeted 4xx-path tests.** Operator-pointed
question: are edge cases and variations tested? Honest audit of
all 72 (method, path) pairs:

- 71/72 had at least one status-code assertion
- 41/72 had at least one 4xx error path tested
- 29/72 had ONLY success-path coverage (some legitimately have no
  4xx -- `/healthz`, `/version`, static UI renders behind 303
  auth redirects)

Closed the realistic operator-triggerable error paths in this
batch:

- `POST /workers/backups` invalid trigger -> 422 (was untested;
  Pydantic enum rejection now pinned)
- `HEAD /boot/{name}` missing artifact -> 404 with empty body
  (UEFI HTTP-Boot firmware HEADs before GET; a 500 here would
  break boot order fallback)
- `HEAD /images/{key}` missing -> 404 (same UEFI HEAD-probe story)
- `DELETE /catalog/entries` unknown src -> 404 (stale UI tab
  clicking delete on a deleted row gets a clean error, not 500)
- `DELETE /catalog/entries` no auth -> 401

Suite 828 -> 833.

### What's still uncovered

Routes with no 4xx test left (less realistic / lower risk):

- Most read-only GETs that auth-redirect on 401 already (these
  have implicit error coverage via the auth gate)
- `/healthz`, `/version`, `/pxe-bootstrap.ipxe` -- legitimately
  success-only
- `POST /ui/settings/{upstream,backup,tftp-control}` form-validation
  edge cases (invalid retention, bogus tag)
- `POST /ui/catalog/entries` http(s) branch edge cases (empty
  image_url, malformed sha_url)

These are real but lower-blast-radius than the ones closed here.
Continue tackling per concrete operator hits.

## [0.33.14] - 2026-05-26

**UI catalog-entry add: oras:// branch tested.** The JSON
`POST /catalog/entries` oras path is tested; the parallel Form
endpoint at `POST /ui/catalog/entries` had only its http(s)
branch covered. The oras branch (lines 913-975 of `_ui.py` --
~60 lines) was previously the largest uncovered block.

Three new tests in `test_web_ui.py`:

- happy path: oras URL resolves via `oras.resolve_ref`, row
  inserts with digest / name / format / size_bytes from the
  manifest, 303 to `/ui/images`
- resolve failure: `OrasError` from `resolve_ref` redirects with
  `?error=oras+resolve+failed` rather than 500-ing
- duplicate src: re-submitting the same oras URL hits
  `UNIQUE(src)` and redirects with `?error=already+exists`

### Coverage

- `_ui.py` 91% -> 94%
- Total suite: 825 -> 828 tests
- Overall: 91% -> 92%

## [0.33.13] - 2026-05-26

**Backup manager helper coverage.** Two small utility-function
gaps closed:

- **`_resolve_max_parallel`**: tests for `BTY_BACKUP_MAX_PARALLEL`
  env-var parsing (numeric, out-of-range, non-numeric fallback,
  unset). Same shape as the v0.33.10 hash-manager addition --
  three managers all read their own env var; operators who set
  one might typo a similar one.
- **`_suppress_oserror`**: tests that the context manager swallows
  ``OSError`` subclasses (used around best-effort rmtree cleanup),
  propagates non-OSError exceptions unchanged, and exits cleanly
  on success. Pinned semantics so a future "simpler" rewrite that
  also swallows unrelated bugs would fail loudly.

Suite 823 -> 825.

## [0.33.12] - 2026-05-26

**Operator-facing edge cases get explicit tests.**

### `_verify_sha256_manifest` error paths (5 tests)

The sha256-manifest verifier runs after every netboot fetch. The
success path was tested via `test_fetch_release_round_trips`, but
each operator-facing failure mode had no dedicated test:

- malformed manifest line (corrupted upstream / partial CDN upload)
- referenced file missing from files_dir
- self-reference + blank lines skipped (some sha256sum
  invocations emit these)
- `*` / `./` filename prefix stripped (binary-mode and
  operator-edited manifests)
- empty manifest rejected (an empty file would otherwise "pass"
  verification silently)

### `/pxe/{mac}` flash-mode-without-ref branch (1 test)

`PUT /machines/{mac}` accepts `boot_mode=bty-flash-always` without
a `bty_image_ref` bound -- the machine is in a "policy picked but
image not yet selected" state. The PXE handler's response to this
state landed in the ``ipxe_unknown.j2`` fallback branch, which had
no test. Now pinned: the audit event records ``offer_kind="unknown"``.

### Coverage

- `_releases.py` 83% -> ~95% (rough)
- Total suite: 817 -> 823 tests
- Overall: 90% -> 91%

## [0.33.11] - 2026-05-26

**Two integration-level tests for behaviors that had no end-to-end
coverage.**

### Schema-rotation runs on real app startup

The init_db-level rotation tests (`test_web_db.py`) prove the SQL
primitive works. But nothing pinned that `create_app` actually
invokes the rotation on its way up. A refactor that moved init_db
behind a flag, or skipped it during app build, would have left the
unit tests passing while real bty-web silently failed to rotate.

`test_create_app_rotates_stale_state_db_end_to_end` stamps a
state.db with a fake-old version, builds the app via `create_app`,
hits `/healthz`, and asserts:

- the `state.db.<oldver>.<ts>.bak` file exists alongside the fresh DB
- the fresh DB carries `bty.__version__` and has no leftover rows
- exactly one `system.schema_reset` event landed
- the `.bak` still contains the pre-rotation operator row

### DownloadManager: vanished-catalog-entry

`_catalog.py` had no test for the "operator deleted the catalog
entry mid-fetch" branch. Pre-this-round the worker would have
silently AttributeError'd on `entry.sha256` if `_lookup_entry`
returned None at the worker's second lookup. The branch has a
proper handler (status=failed with "catalog entry vanished")
but no test pinned it.

`test_run_handles_catalog_entry_vanished_mid_download` enqueues a
job, monkey-patches `_lookup_entry` to return None after the
enqueue-time lookup, asserts the state lands at
status=failed with `error="catalog entry vanished"`.

### Coverage

- `_catalog.py` 81% -> 85%
- Total suite: 815 -> 817 tests
- Overall: 88% -> 90%

## [0.33.10] - 2026-05-26

**Continue closing test gaps.** Two specific holes:

### `/workers/backups` HTTP layer

The BackupManager has direct tests, but the three HTTP routes that
wrap it (`GET / POST / DELETE /workers/backups`) had zero
end-to-end coverage. The `/ui/backups` page polls these three
endpoints; if the HTTP layer drifted, the UI would surface a
confusing 500 / 422 instead of the JSON shape the JS expects.

Added 5 tests in `test_web.py`:

- `test_workers_backups_get_requires_auth` -- unauth -> 401
- `test_workers_backups_get_empty_returns_stable_shape` -- fresh
  fixture returns parseable envelope
- `test_workers_backups_post_runs_to_completion` -- POST enqueue
  -> 202; follow-up GET reflects the job; backup terminates
- `test_workers_backups_delete_unknown_returns_404` -- cancel
  against unknown id -> 404, not 500
- `test_workers_backups_delete_requires_auth` -- cancel needs cookie

### `HashManager` enqueue / cancel / parallelism

`_hash.py` was at 89%. The before-start error, env-var-driven
parallelism resolution, and explicit `HashCancelled` cancel path
had no tests. Added 3 tests in `test_web_hash_manager.py`:

- `test_enqueue_before_start_raises` -- RuntimeError if not started
- `test_resolve_max_parallel_env_var` -- `BTY_HASH_MAX_PARALLEL`
  parsing, including out-of-range and non-numeric fallback
- `test_enqueue_explicit_hash_cancelled_lands_cancelled` -- the
  worker raising `HashCancelled` directly flips status=cancelled
  (vs the cancel-during-IO-error race already covered)

### Coverage

- `_hash.py` 89% -> 96%
- `_app.py` 90% -> 91% (workers/backups + side-effect coverage)
- Total suite: 807 -> 815 tests

## [0.33.9] - 2026-05-26

**Targeted test coverage for the worst gap.** Operator-pointed
critique: every API endpoint / function / UX experience must be
tested, otherwise it's broken. The three real bugs found in the
v0.33.x arc (PXE INSERT race, netboot fetch URL, session secret)
all lived in code paths that lacked dedicated tests.

A `coverage run pytest` pass showed `_release_mgr` at 66% -- the
worst gap. The manager wraps `_releases.fetch_release` (where
v0.33.7's bug lived) in an asyncio worker pool. The 66% was
incidental coverage from `/ui/netboot` integration tests that
don't reach the failed / cancelled / dedup / backfill branches.

### Added

- **`tests/test_web_release_mgr.py`** -- 14 dedicated tests
  covering:
  - enqueue input validation (path-traversal tag, before-start
    error)
  - happy path: queued -> running -> completed with per-artifact
    state propagation + terminal audit event
  - same-tag dedup while running (operator double-clicks
    "Fetch artifacts")
  - FetchError -> failed with error preserved + audit event
  - unexpected exception -> failed with typed prefix
  - FetchCancelled -> cancelled with NO audit event (operator-
    initiated, not a failure)
  - cancel-vs-IO-error race: cancel flag set + FetchError ->
    cancelled (the manager re-classifies)
  - backfill from completed event
  - backfill from failed event
  - backfill dedups per-tag to newest verdict
  - backfill soft-fails on corrupt DB
  - `ReleaseArtifactState.to_dict` shape pin
  - `ReleaseFetchState.to_dict` includes artifacts sub-array

### Coverage

`_release_mgr` 66% -> 93%. Total suite 793 -> 807 tests.

### Acknowledgement of scope

This closes one gap. The remaining 12 percentage points (88% ->
100%) is multi-day work and would require coverage on async SSE
streams, lifespan teardown, and several rare error branches in
`_app.py`. Future rounds tackle specific gaps as they get
correlated with operator-reported failures.

## [0.33.8] - 2026-05-26

**Harden session-secret resolution against empty / corrupt values.**
Third real bug this evening, found by reasoning rather than survey.

### The bug

`_resolve_secret_key` in `bty.web.__init__` silently passed an empty
string through to `SessionMiddleware` when:

- `BTY_SESSION_SECRET=""` was set in env (operator "clearing" the
  override by setting it to empty)
- the on-disk `session-secret` file existed but was empty / pure
  whitespace (e.g. a half-written file from a crashed first boot
  -- `Path.write_text` isn't atomic, so a `SIGKILL` between open
  and write left an empty truncated file that the next start
  loaded as the HMAC key)
- an operator `touch`-ed the file expecting bty-web to populate it

`SessionMiddleware` happily accepts `secret_key=""` and signs every
session cookie with a predictable HMAC. On a LAN segment with even
one curious user, forging an admin session cookie is trivial.

### The fix

`_resolve_secret_key` now treats any empty / whitespace value from
either env or file as "not set" and falls through to the
generate-and-persist path. Generation uses a same-dir tempfile +
`Path.replace` (atomic rename) so a crash mid-write either leaves
the previous file intact or no file at all -- never a truncated /
empty one. The empty file (if present) gets overwritten by the
rename.

### Tests

Five regression tests in `test_smoke.py`:

- `test_resolve_secret_key_rejects_empty_env`
- `test_resolve_secret_key_rejects_whitespace_env`
- `test_resolve_secret_key_rejects_empty_file`
- `test_resolve_secret_key_rejects_whitespace_file`
- `test_resolve_secret_key_persist_is_atomic` -- no `.tmp` debris
  after a successful generate

### Operator impact

If your appliance has an empty `session-secret` file, the next
bty-web start regenerates one. All existing session cookies
invalidate -- operators re-log in once, with `bty-web` now signing
cookies under an actual key. Subsequent restarts reuse the
persisted key as before.

## [0.33.7] - 2026-05-25

**Fix the netboot fetch button when the operator clicks
"Fetch netboot artifacts".** Second real hardware-reported bug
this evening.

### The bug

Operator running v0.33.4 clicked the fetch button and got:

```
boot release 'latest' fetch failed: GET https://github.com/safl/bty/releases/latest/download/bty-netboot-x86_64-v0.33.4.vmlinuz returned HTTP 404 Not Found
```

The asset filename embeds the running bty-web's version
(intentional: multiple versions need to coexist in
`BTY_BOOT_DIR` during an upgrade, and the iPXE template refs
the matching version). GitHub's `/releases/latest/download/`
redirects to whatever release is current -- in this case v0.33.6,
whose asset list contains `...v0.33.6.vmlinuz`, not
`...v0.33.4.vmlinuz`. The redirect target has the wrong asset
names; every fetch 404'd.

### The fix

`tag="latest"` (the UI form's default) is now normalised to
`v<bty.__version__>` inside `fetch_release`. The
`releases/latest/download/...` URL form is dropped entirely --
it never worked given version-pinned asset names. Operators
running v0.33.X always pull from the v0.33.X release's tag,
which is the only release whose asset names this server can use.

### Tests

Two regression tests in `test_web_releases.py`:

- `test_fetch_release_normalises_latest_to_running_version` -- the
  end-to-end success path: pass `tag="latest"`, assert the trio
  lands.
- `test_fetch_release_url_construction_pins_to_running_version` --
  invariant pin: a future refactor can't reintroduce the broken
  `/releases/latest/download/...` URL form.

### Operator impact

Click the "Fetch netboot artifacts" button. It works now.

## [0.33.6] - 2026-05-25

**Fix INSERT race in PXE auto-discovery.** This is the kind of bug
that bites on hardware and is hard to reproduce -- exactly what the
operator-pointed-out shortcoming of the prior polish rounds.

### The bug

`/pxe/{mac}` and `/pxe/{mac}/plan` did:

```text
row = SELECT * FROM machines WHERE mac = ?
if row is None:
    INSERT INTO machines (mac, ...) VALUES (?, ...)
    log machine.discovered
    commit
```

bty-web runs the handler in a thread pool (sync `def`, not
`async def`). Two PXE requests for the same fresh MAC arriving
nearly simultaneously -- iPXE retry, dnsmasq retransmit, BMC
twitching -- both passed the `SELECT` with `row=None`. Both fired
the `INSERT`. The second one hit `UNIQUE(mac)` on the PK and
raised `sqlite3.IntegrityError`, FastAPI returned 500. iPXE's own
retry would succeed on the next attempt, so the operator saw
intermittent 500s in the journal without a reliable repro path.

### The fix

`/pxe/{mac}` and `/pxe/{mac}/plan` now do an atomic
`INSERT ... ON CONFLICT(mac) DO UPDATE ... RETURNING ..., (created_at = ?) AS is_new`.
The upsert is idempotent under contention; the `is_new` flag from
`RETURNING` tells the handler whether to log the `machine.discovered`
event. `_now_iso()` is microsecond-precise, so the
created_at-equality check distinguishes inserts from updates
reliably (two real PXE arrivals don't collide on the microsecond).

### Tests

Three regression tests in `tests/test_web.py`:

- `test_pxe_concurrent_discovery_no_race` -- N parallel `/pxe/{mac}`
  hits via `ThreadPoolExecutor`, asserts all 200 and exactly one
  `machine.discovered` event.
- `test_pxe_plan_concurrent_discovery_no_race` -- same shape for
  `/pxe/{mac}/plan`.
- `test_pxe_discovery_returning_clause_is_race_safe_under_direct_sqlite_repro`
  -- pins the SQL shape against a real sqlite DB with two
  connections, so a future "simplify the upsert" refactor can't
  silently regress to plain INSERT.

### Operator impact

If you're running bty-web on hardware with PXE-retry-happy iPXE
firmware or a flaky lab switch, the intermittent 500s on /pxe go
away. No data migration needed: the SQL upsert against an existing
machine row is a no-op no-op (last_seen_at + last_seen_ip update,
discovered_at preserved via COALESCE).

## [0.33.5] - 2026-05-25

Round 5: error-message hygiene + CLI help drift.

### Fixed

- **404 detail leaked `repr` quotes.** `POST /catalog/downloads`
  with a missing name returned a 404 whose `detail` field was
  `"'no catalog entry named ...'"` -- with literal single quotes,
  because `str(KeyError("msg"))` is `"'msg'"` (KeyError applies
  `repr` to its arg, unlike other built-in exceptions). The handler
  now reads `exc.args[0]` so the operator-visible detail is plain
  text. Regression test pinned in `test_web.py`.

### Changed (CLI help text)

- **`bty-web export --help`** previously claimed "write machines +
  catalog + image files to a bundle directory" -- post-v0.33.2
  metadata-only, the bundle holds only `inventory.json` with
  per-machine hw identity. Updated help to "write a metadata-only
  inventory bundle (mac + lshw + known_disks)"; the `dest`
  argument help now says "bundle directory to create (holds
  inventory.json)".
- **`bty-web import --help`** updated to "load an inventory bundle"
  (was "load a bundle directory").
- Subparser preamble comment in `bty.web.__init__._run_portability`
  updated to match: no longer claims to move catalog or image
  files, only inventory.

### Operator impact

`bty-web export --help` now describes the post-v0.33.2 reality.
404 details on the catalog-downloads enqueue path no longer carry
extra single-quotes around the message.

## [0.33.4] - 2026-05-25

Two more rounds following Round 1+2 of v0.33.3. Different angles:
operator-facing doc truth, and cross-manager consistency.

### Round 3: doc-truth fixes

A survey of operator-visible text caught two drifts from the
v0.33.x reshape:

- **`docs/src/operations.md`** -- the storage-classification table
  used to claim the `bty-web export` bundle "covers exactly" the
  precious paths (state.db + images/). It now distinguishes the
  two precious classes: state.db carries via the v3 metadata
  bundle; image bytes do NOT travel in the bundle and move via
  `rsync` / disk-copy / catalog re-fetch.
- **`docs/src/reference.md`** -- the "State export / import
  format" section was a stub ("populated alongside the feature").
  Now documents v3: shape of `inventory.json`, version semantics,
  what import restores.

### Round 4: unify byte counters to `bytes_done`

Four manager classes (Download / Hash / ReleaseFetch / Backup)
each tracked progress under a different field name:
`bytes_downloaded` / `bytes_hashed` / `bytes_done` /
`bytes_written`. The `downloads.html` template already carried a
shim normalising `bytes_downloaded -> bytes_done` so the single
progress-bar JS could render both -- the giveaway that the
inconsistency was a known papercut.

All four now expose `bytes_done`. JSON API output for `/workers/*`
and `/catalog/downloads` / `/catalog/hashes` / `/workers/backups`
endpoints carry the same field name. Progress-callback type-alias
docstrings in `bty.catalog` + `bty.images` updated to the new
parameter name.

- **Removed**: the JS normalisation shim in
  `_templates/ui/downloads.html` and the parallel pseudo-object
  trick in `_templates/ui/hashing.html`.
- **Documented**: `BackupManager._run_one` now carries an inline
  comment explaining why -- unlike its three siblings -- it does
  NOT poll `state._cancel` in the worker loop (metadata-only
  export finishes in milliseconds; the Protocol shape still
  requires the field).

### Operator impact

- **Breaking** (pre-1.0 OK): if you scrape the bty-web JSON API,
  the `bytes_downloaded` / `bytes_hashed` / `bytes_written` keys
  are gone. Read `bytes_done` instead.

## [0.33.3] - 2026-05-25

Two simplification rounds following v0.33.2's metadata-only backup
shape. Each round was an after-the-fact "this is now overkill"
catch.

### Round 1: drop the tar-stream download wrapper

`iter_bundle_tar` streamed a custom tar archive of the bundle dir
into the HTTP response via a `_ChunkBuf` file-like; the gymnast
made sense when bundles were multi-GiB. v3 bundles are one
`inventory.json`, so `/ui/backups/{id}/download` now serves the
file directly via `FileResponse` as `application/json`.
Content-Disposition is `attachment; filename="<id>.json"` so the
browser saves it with a self-describing name.

- **Removed**: `bty.web._backup.iter_bundle_tar`,
  `_ChunkBuf`, the `tarfile` / `mypy` ignore hack, and the
  two tar-roundtrip tests.
- **Changed**: the operator's download button now reads ".json"
  (was ".tar").

### Round 2: dead-code sweep

- **`_dir_size` -> `_bundle_size`**: was a full `os.walk` of the
  bundle dir; v3 bundles are one file so it's a single stat.
  Existing on-disk v2 bundles now report just their inventory
  size, which is what's actually portable across the upgrade.
- **Dropped v1 fallback in `_read_bundle`**: line previously
  read `inventory.get("exported_by_bty_version") or inventory.
  get("bty_version")` to be charitable to pre-v0.31.0 bundles.
  Pre-1.0 policy refuses v1 on import; the listing-page
  fallback was dead.
- **Kept `BackupState._cancel`**: the field looked dead
  (metadata-only export finishes in milliseconds; no cancel
  check in `_run_one`), but it's required by the
  `_BaseAsyncManager` Protocol that the cancel API operates on.
  Removing it would break the shape contract for no win.

### Operator impact

The backup download button gives you a `.json` you can `jq` (or
diff across appliances) directly. No more "unpack the tar first"
step.

## [0.33.2] - 2026-05-25

**Backups are metadata-only. No image bytes.** v0.31.0 through
v0.33.1 shipped full image_root in every backup bundle, which
produced multi-GiB "backups" dominated by catalog cache files the
appliance can just re-fetch. The v3 bundle format is just
`inventory.json` (renamed from `manifest.json` -- the file is a
machine inventory; "manifest" stays reserved for the catalog
manifest TOML) with the per-machine hardware identity (mac +
`lshw` + `known_disks`). A backup now fits in dozens of KiB and
finishes in milliseconds. The two JSON fields decode to native
objects/arrays (not re-encoded strings), so the file is
`jq`-readable as-is.

The data model the user named: backup = mac + lshw + lsblk. Import
= add the machines with that hardware attached. Image files carry
their `bty_image_ref` prefix in the filename
(`catalog-<ref:12>-<slug>.<ext>`); they associate with catalog
entries automatically when both exist on the same appliance (v0.33.1
fix). No image bytes ever travel in a backup.

### Changed

- **`export_bundle(state_path, dest, *, now)`** -- dropped the
  `image_root` parameter; the bundle no longer copies image
  bytes. Returns `ExportSummary(machines, dest)`; the `files`
  count is gone.
- **`import_bundle(state_path, src, *, now)`** -- same drop;
  returns `ImportSummary(machines)`. Half-import rollback is gone
  because there's nothing to roll back -- the only mutation is
  the `INSERT OR REPLACE` over the `machines` table.
- **`_EXPORT_VERSION = 3`**. v1 (pre-v0.31.0) and v2 (v0.31.0..
  v0.33.1, with image bytes) both refuse on import. Pre-1.0
  policy: regenerate on the source release.
- **`BackupManager.start(state_path, backups_root)`** -- dropped
  the `image_root` parameter; backups no longer touch image_root.
- **`BackupState` / `BackupOnDisk`** -- dropped the `files`
  field. The Backups page shows machine count + bytes-on-disk
  (bundle directory size) only.
- **`bty-web export` / `bty-web import` CLI** -- same arg drop;
  the help text reflects metadata-only semantics.
- **`/ui/backups` intro copy** -- now says "metadata-only ...
  image bytes live in BTY_IMAGE_ROOT and are NOT included".

### Operator impact

- A scheduled or "Back up now" run produces a single-file bundle
  whose `inventory.json` lists the per-machine hardware identity.
  Tens of KiB even with hundreds of machines.
- Existing v2 bundles on disk still list on `/ui/backups` (with
  blank metadata if their manifest is unparseable), but
  `bty-web import` against them returns `BundleVersionMismatch`.
  Regenerate on the source release if you need a v3 bundle.
- After a fresh-appliance reflash + `bty-web import <bundle>`,
  the machines re-appear with `boot_mode=bty-inventory` and just
  their hw_lshw + known_disks. The operator re-binds image +
  policy. If the image-store disk survived the reflash, the
  `catalog-<ref:12>-<slug>.<ext>` files associate with their
  catalog entries automatically.

## [0.33.1] - 2026-05-25

**Fix duplicate /ui/images rows when a catalog entry is cached.**
v0.33.0 (and the v0.31.0+ catalog-prefix rollout before it)
auto-imported every file under `BTY_IMAGE_ROOT` as a synthetic
`catalog_entries` row with `src = file://<name>`. For files in
the `catalog-<ref:12>-<slug>.<ext>` cache form, that minted a
second row whose `bty_image_ref` didn't match the upstream
catalog entry's ref, so `/ui/images` rendered both the upstream
entry ("nosi fedora-sysdev (x86_64, rolling)") and a synthetic
twin showing the raw filename as Name. The synthetic row also
carried a `file:` source pointing at the same on-disk file as
the upstream entry's local source -- a give-away that the merge
was treating one image as two.

### Fixed

- **`bty.images.merge_with_catalog` pass 1** skips
  catalog-prefixed filenames; pass 2 picks them up via the
  cache-hit lookup against the real catalog entry.
- **`_app._auto_import_dir_scan_rows`** skips catalog-prefixed
  filenames; the upstream catalog entry already owns them.

### Added

- **`bty.catalog.is_catalog_cache_filename(name)`** predicate -- the
  one place the dir-scan paths consult to recognise cache files.
- **Two regression tests** (`test_merge_with_catalog_skips_catalog_
  cache_files_in_dir_scan`, `test_auto_import_skips_catalog_cache_
  files`) covering the visible-screenshot shape end-to-end.

### Operator impact

After upgrading + restarting bty-web, `/ui/images` shows one row
per logical image again. Existing duplicate `catalog_entries`
rows from a v0.33.0 DB clear on the next `state.db` rotation
(or via `DELETE FROM catalog_entries WHERE src LIKE 'file://catalog-%'`
if you want to drop them in place).

## [0.33.0] - 2026-05-25

**Schema mismatches now auto-rotate; the recovery wizard is gone.**
The v0.32.x interactive recovery flow (browser checklist, polling
JS, `os._exit(0)` dance, `_recovery.py`, `recovery.html`) was
overengineered for what `state.db` actually is. The DB holds
machine bindings (re-discovered on next PXE contact), an audit log
(cosmetic), a catalog cache index (regenerated), and a handful of
settings -- all of it regenerable. Operator-irreplaceable state
lives in image files under `BTY_IMAGE_ROOT`, which neither code
path ever touches.

The new shape: on `bty_version` mismatch (or a pre-versioning DB
with no marker), `_db.init_db` renames `state.db` to
`state.db.<from>.<UTC-iso>.bak`, unlinks the WAL sidecars, and
creates a fresh DB stamped with the running version. A
`system.schema_reset` event is recorded so the dashboard tripwire
surfaces the rotation; operators acknowledge from `/ui/events`.
No wizard, no polling, no operator confirmation step -- the
appliance just works after `systemctl restart bty-web`.

### Removed

- **`bty.web._recovery` module** (build_recovery_app + all
  POST handlers + the per-action exit scheduler).
- **`_templates/ui/recovery.html`** wizard template.
- **`bty.web._db.VersionMismatchError`** -- no longer raised;
  schema mismatch is non-exceptional.
- **`bty.web._db.check_db` / `DbCheckResult` / `DbState`** --
  the non-mutating probe used by the recovery dispatch.
- **15 recovery-mode integration tests** (`tests/test_web_recovery.py`).
- **Recovery-mode routes documentation** in operations.md +
  reference.md.

### Added

- **`bty.web._db._rotate_to_bak(state_path, from_version)`** --
  renames `state.db` to a timestamped `.bak`, drops sidecars,
  returns the new path. Collision-safe (numeric suffix on
  same-second double-rotation).
- **`system.schema_reset` event kind**, recorded by `init_db`
  when rotation fires. `details = {from_version, to_version,
  archived_at}`; surfaces as an unacknowledged dashboard
  tripwire.
- **8 new `init_db` rotation tests** (`tests/test_web_db.py`):
  pre-versioning rotation, mismatch rotation, event recording,
  idempotent no-op, sidecar cleanup, collision handling,
  forensics-preservation, `.bak`-untouched-on-idempotent.

### Operator impact

- **Upgrade path is no-op-on-the-operator.** Update bty-web,
  systemctl restart, dashboard shows an unacknowledged
  `system.schema_reset` event the operator dismisses. Machine
  bindings rebuild as PXE clients re-check-in.
- **Recovering specific rows from an old DB** is `sqlite3
  /var/lib/bty/state.db.<from>.<ts>.bak "SELECT ..."`. The
  `.bak` is a normal sqlite file; `rm` to discard.
- **Hardware-inventory preservation** still goes through
  `bty-web export` (before upgrade) + `bty-web import` (after),
  same as v0.31.0+ -- the slim v2 bundle format is unchanged.

## [0.32.4] - 2026-05-25

Round 7 polish: machine-delete UX feedback + docs caught up to
v0.32.0's recovery wizard.

### Changed

- **`POST /ui/machines/<mac>/delete` now flashes the outcome.**
  Previously the form silently 303'd to `/ui/machines` whether or
  not the row existed -- a stale tab clicking delete on an
  already-removed MAC got the same redirect as a real delete with
  no signal. v0.32.4 returns ``?deleted=<mac>`` on real removal
  (green success banner) or ``?missing=<mac>`` on no-op (yellow
  info banner: "was not found -- already deleted, or never
  bound"). Banners auto-dismiss after 5s.

### Documentation

- `docs/src/operations.md` -- upgrade section now describes the
  v0.32.0+ recovery wizard checklist alongside the CLI-driven
  alternative for headless / scripted upgrades.
- `docs/src/reference.md` -- new "Recovery-mode routes (v0.32.0+)"
  table documenting `/`, `/ui/recovery`,
  `/ui/recovery/status`, `/ui/recovery/wipe`,
  `/ui/recovery/wipe-and-import`, `/healthz`, the catchall 503,
  and the error-response shape (400 / 404 / 409 / 500 / 507).

## [0.32.3] - 2026-05-25

Round 4 of the post-v0.32.0 improvement grind: security boundary
DRY-up, concurrency hardening, observability fixes.

### Added

- **`bty.web._security.validate_basename`** -- shared rejector for
  path-traversal-y inputs (NUL, `/`, `\\`, `.`, `..`, empty).
  Replaces five duplicated implementations of the same rule
  across `_catalog._reject_traversal_name`,
  `_hash._reject_traversal_name`, `_app.delete_catalog_cache`,
  `_recovery._wipe_and_import`, and the `e45f93b` D5 test path.
  Carries a ``label`` kwarg so 400-message text identifies which
  field was rejected. 21 new tests in
  `tests/test_web_security.py` lock down the accept/reject
  matrix.

### Fixed

- **Concurrent wipe race in the recovery wizard.** v0.32.0's
  `_schedule_exit_after_response` would spawn TWO `os._exit(0)`
  threads if a second `POST /ui/recovery/wipe` arrived during the
  500ms response-flush window. v0.32.3 gates on a module-level
  ``_exit_scheduled`` flag so only one exit thread runs. The
  second request's wipe step is already idempotent (the unlink
  helper soft-skips missing files), so this just closes the race
  on the exit side.
- **sqlite WAL deadlock starves shutdown.** `open_db` connected
  without an explicit timeout. If a future WAL writer wedges
  (out-of-process holder, broken NFS), the lifespan teardown
  would hang forever waiting on `conn.close()`. v0.32.3 sets
  `timeout=5.0` explicitly so lock waits are bounded -- matches
  sqlite's stdlib default but makes the contract auditable.
- **Silent exception swallows on DB-write failures.** Three
  back-fill paths in `_catalog.py` and `_hash.py` caught
  `Exception: pass` to keep the appliance up when state.db is
  briefly unavailable -- correct behaviour, but a corrupt DB
  that rejects every back-fill vanished from the journal too.
  v0.32.3 logs the exception with `log.exception(...)` so a
  repeating failure shows up under
  `journalctl -u bty-web --grep backfill` without changing
  the soft-fail semantics.

## [0.32.2] - 2026-05-25

Round 2 of the improvement grind: a startup perf fix + test
coverage on the recovery-dispatch branch and bundle preflight.

### Fixed

- **`bty-web` startup ran `images.list_images(image_root)` twice.**
  Once inside `_auto_import_dir_scan_rows`, once just below for
  the hash-enqueue loop. On a Pi-class appliance with many image
  files, two full inode walks per startup add up. v0.32.2 hoists
  the scan into the lifespan caller, threads the result through
  both consumers via a new `_auto_import_dir_scan_rows(scanned)`
  parameter. Single-call invariant verified against the existing
  test suite (no behavior change; only the count of `list_images`
  invocations differs).

### Added

- **`test_wipe_and_import_rejects_bundle_with_missing_manifest`**:
  recovery wizard's preflight refuses a bundle dir whose
  `manifest.json` is absent BEFORE wiping state.db. Locks the
  invariant that a bad operator pick can't leave the appliance
  with both wiped state AND no successful import.
- **`test_create_app_dispatches_to_recovery_when_db_is_pre_versioning`**:
  integration test asserting `create_app` returns the recovery
  app (not the full app) when `check_db` reports needs_recovery.
  A regression that moves the recovery dispatch below `init_db()`
  would now fail at test time instead of in production journals.

## [0.32.1] - 2026-05-25

Polish pass on v0.32.0's recovery wizard + the underlying import
path. Fixes the rougher edges a real operator would hit on first
use.

### Fixed

- **Recovery wizard polls forever on a wedged restart.** The JS
  side now caps polling at 60 attempts (1 minute), then renders a
  red error card with a `journalctl -u bty-web` hint instead of
  spinning silently. Previously a bty-web that didn't come back
  (broken state.db path, port conflict, dependency missing) left
  the operator staring at "Waiting for it to come back..." forever.
- **Double-click race on the recovery POST actions.** Page-level
  `inflight` flag now blocks every action button + cancels
  beforeunload navigation while a wipe/import is in progress. A
  hard-refresh during the destructive action no longer slips a
  second POST through before the first finishes.
- **Half-imported state after a copy failure.** `import_bundle`'s
  file-copy loop now tracks every destination it creates; an
  `OSError` mid-loop unlinks the partial copies before re-raising
  so the operator sees image_root in its pre-import state instead
  of a half-loaded mess. Re-raises as an annotated `OSError`
  naming the failing file count + the cleanup count. New regression
  test (`test_import_rolls_back_partial_file_copies_on_oserror`).
- **Wizard rendered the destructive checklist against an OK DB.**
  When the operator's browser polled `/ui/recovery` between the
  wipe response and systemd's restart, the OLD recovery-mode
  process answered with the full action checklist against a now-
  fresh state.db. v0.32.1 detects `not needs_recovery` in the
  render path and shows a "Recovery complete -- redirecting"
  banner instead, with a 1.5s auto-redirect to `/ui/dashboard`.
- **Unreadable bundle file-dirs silently showed "0 files".** The
  picker now flags any bundle whose `files/` subdir raises
  `OSError` on `iterdir` as `unreadable=True` (same UI state as
  a missing manifest), so the operator sees the bundle disabled
  + labeled "unreadable manifest" instead of selecting an
  unloadable one.

### Changed

- **`POST /ui/recovery/wipe-and-import` error responses** now
  categorise: `BundleVersionMismatch` -> 409, missing manifest /
  bundle -> 404, `PermissionError` -> 500 with chmod hint,
  `OSError` (disk full) -> **507 Insufficient Storage** with a
  free-space hint, anything else -> 500 with `TypeName: message`.
  Operator sees a recoverable next step in the wizard error
  panel instead of a bare 500.

## [0.32.0] - 2026-05-25

Recovery-mode UI: when `bty-web` boots against a `state.db` that
doesn't match the running release (v0.31.x's hard-check refusal
class), it now serves an **interactive recovery wizard** on the
same port instead of dying with a journal-only error. The
operator hits the appliance URL in a browser, lands on a styled
checklist, picks a recovery strategy, and bty-web executes it.

### Added

- **`bty.web._db.check_db(path)`** -- non-mutating probe that
  returns a `DbCheckResult` describing the state (`OK`, `FRESH`,
  `PRE_VERSIONING`, `MISMATCH`) without raising. Replaces the
  "always raise, let the caller try / except" shape for the
  recovery flow's needs.
- **`bty.web._recovery.build_recovery_app(...)`** -- minimal
  FastAPI app served when `check_db` reports a mismatch. Routes:
  - `GET /` -> redirect to `/ui/recovery`
  - `GET /ui/recovery` -> the wizard page
  - `GET /ui/recovery/status` -> JSON poll (the wizard's JS
    polls this to detect when bty-web has restarted into normal
    mode and auto-redirects)
  - `POST /ui/recovery/wipe` -> unlink `state.db` (+ sqlite
    sidecars) and schedule `os._exit(0)` so systemd's
    `Restart=on-failure` brings up a fresh process
  - `POST /ui/recovery/wipe-and-import` body=`{"backup_id":
    "..."}` -> wipe + import a v2 bundle from
    `backups_root/<backup_id>`, then exit
  - `GET /healthz` -> 503 with reason (so automated probers see
    the unhealthy state)
  - Everything else -> 503 with a meta-refresh back to
    `/ui/recovery` (so a bookmarked normal-mode URL doesn't
    leave the operator on a JSON error page)

- **Recovery wizard template** (`_templates/ui/recovery.html`):
  ultra-explicit banner ("bty-web needs operator attention"),
  current state summary (stored version vs running version,
  at-risk row counts), three recovery strategy cards
  (wipe-and-fresh / wipe-and-import-from-backup / manual shell
  recipe), and an auto-progressing checklist (steps 3 + 4 tick
  green as the operator's chosen action proceeds + bty-web
  restarts into normal mode).

### Changed

- **`create_app`** dispatches to the recovery app when
  `check_db` reports a needs-recovery state, instead of letting
  `init_db` raise out into the journal. The full app is built
  only when the DB is OK or FRESH; everything else goes through
  the wizard.

### Why this matters

The v0.31.0 hard-check was correct policy (refuse to silently
mix schemas) but the UX was painful: bty-web died in a journal
loop, the operator had to ssh in and read the systemd error to
find the `rm state.db` recovery command. v0.32.0 keeps the
policy (no silent migration) but moves the recovery
conversation into the browser where the operator already is.

## [0.31.1] - 2026-05-25

Critical fix for the v0.31.0 hard `bty_version` DB check, plus a
quality grind through the documentation + tests left stale by the
v0.31.0 cache→images merge.

### Fixed

- **Hard `bty_version` check leaked across `systemd Restart=on-failure`
  retries.** v0.31.0's `_db.init_db` ran `conn.executescript(SCHEMA)`
  BEFORE the refuse condition; `sqlite3.executescript` issues an
  implicit COMMIT, so the new `bty_version` table (empty) got
  committed to disk even when the call then raised
  `VersionMismatchError`. systemd retried 5s later; the second call
  saw the marker table existed, treated the empty row as "fresh DB,
  stamp it", and silently accepted the franken-state (old machine
  inventory + audit log carried into v0.31.0 under a stamped
  `bty_version=0.31.0` row). Surfaced in production: an operator
  with `bty-state-migrate` (state.db on a separate disk) upgraded
  the appliance disk, hit the bug, and saw old `bty-flash-once` +
  events in the v0.31.0 UI.

  Fix: refuse BEFORE `executescript` runs so no mutation slips
  through. Same shape for the version-mismatch path -- the refuse
  branch leaves the DB exactly as it was, so the operator's
  `bty-web export` on the OLD release reads consistent state.
  Regression test
  (`test_init_db_refuses_pre_versioning_db_across_restart_retries`)
  exercises three consecutive `init_db` calls against the same
  pre-versioning DB and asserts no marker table appears after the
  failed first call.

### Changed

- **Documentation sweep for v0.31.0's cache→images merge.**
  `README.md`, `AGENTS.md`, `docs/src/operations.md`,
  `docs/src/walkthrough-image-store.md`,
  `docs/src/walkthrough-catalog.md`, `docs/src/walkthrough-usb.md`,
  `docs/src/reference.md`, `docs/src/dependencies.md`, and
  `docs/src/flows.md` updated to describe the new
  `BTY_IMAGE_ROOT`-only layout, the
  `catalog-<ref:12>-<slug(name)>.<ext>` naming, and the
  hard-version-check upgrade flow. `AGENTS.md`'s `boot_policy`
  references updated to the v0.23.0 `boot_mode` vocabulary with
  the v0.25.0 mode/state split documented. New "Catalog file
  naming" section in `reference.md` explains the URL-keyed name
  scheme.
- **`_ui.py:_row_to_dict` docstring** updated -- no longer cites
  the dropped `_REQUIRED_COLUMNS` / `StaleSchemaError` machinery;
  cites the `bty_version` hard check instead.

### Added

- **`local_filename_for` edge-case tests** in `tests/test_catalog.py`:
  unicode names (slug strips to ASCII), very long names (no
  truncation; ref-prefix still disambiguates), consecutive
  separator collapse, leading-dot format normalisation,
  format=None default, empty/all-non-ASCII name fallback to
  `image`, idempotence on same inputs, distinct URLs producing
  distinct filenames. Pure-function coverage so the on-disk dedup
  contract is locked in.

## [0.31.0] - 2026-05-25

**BREAKING:** state.db wipes on upgrade. bty-web now refuses to start
on an old DB. The cross-release path is `bty-web export` (slim bundle
of images + cached files + hardware inventory) then wipe + import on
the new release.

### Added

- **Hard `bty_version` check at bty-web startup.** state.db carries
  the exact `bty.__version__` that created it in a new `bty_version`
  table; on startup, mismatch (or a pre-versioning DB with data
  tables but no marker row) raises `VersionMismatchError` with an
  operator-actionable recovery message and bty-web refuses to start.
  Pre-1.0 policy is no migration apparatus -- every release wipes
  state (or migrates via export/wipe/import). Replaces the soft per-
  column `StaleSchemaError` machinery which let pre-versioning DBs
  silently survive into incompatible code paths (the v0.30.x footgun
  that motivated this release).

### Changed

- **Cache → image_root merge.** No more separate
  `BTY_CATALOG_CACHE_DIR` / `/var/lib/bty/cache/`. Catalog-fetched
  files now land under the operator's `BTY_IMAGE_ROOT` (i.e.
  `/var/lib/bty/images/`) with a URL-keyed name:
  `catalog-<bty_image_ref[:12]>-<slug(name)>.<ext>`. Operator-typed
  files keep their original filenames. One mental model, one
  directory, one `ls` to inventory everything. The catalog-prefix
  + 12-hex namespace guarantees same-URL idempotency and rules out
  cross-entry collisions at any plausible homelab catalog size.

  Operator impact on upgrade: existing files in `/var/lib/bty/cache/`
  are orphaned (not deleted). Re-fetch via the Downloads UI lands
  them at the new URL-keyed path; the operator can `rm -rf
  /var/lib/bty/cache/` to reclaim disk once they're confident.

- **Slim export/import format (bundle version 2).** `bty-web export`
  now carries:

  | What | Why |
  |---|---|
  | Everything under `BTY_IMAGE_ROOT` (flat `files/` subdir) | Re-downloads are expensive; bytes ARE the value |
  | Per-machine: `mac` + `hw_lshw` + `known_disks` (+timestamps) | Hardware inventory is expensive to re-collect via PXE |

  Drops (operator re-types on the destination): catalog entries,
  machine bindings (`boot_mode` / `bty_image_ref` / `target_disk_serial`
  / `sanboot_drive` / `hostname`), `saw_flasher_boot` state, audit
  log, settings, backups. Version 1 bundles (pre-v0.31.0) are not
  migratable -- regenerate on the source release before upgrading.

- **`fetch_to_cache` no longer requires `entry.sha256`.** Rolling-tag
  ORAS entries (`oras://...:latest`) get a stable URL-keyed local
  filename and benefit from on-disk dedup the same way sha-pinned
  entries do. Verification still fires when a sha is pinned.

### Removed

- `bty.catalog.default_cache_dir()`. Callers use
  `bty.images.default_image_root()` everywhere the cache used to be.
- `BTY_CATALOG_CACHE_DIR` env var and the "Image cache" row on the
  Settings page.
- `BackupState.catalog_entries` and `BackupState.images` (-> single
  `BackupState.files` field); same for `BackupOnDisk`.
- The per-column `_REQUIRED_COLUMNS` / `_ADDITIVE_COLUMNS` /
  `StaleSchemaError` machinery in `bty.web._db` (superseded by the
  version-stamp check).

## [0.30.2] - 2026-05-25

Fixes `bty-flash-once` re-flashing on every PXE boot instead of
terminating after one flash. Real operator-impact bug surfaced by
audit logs showing the same machine flashing twice within three
minutes.

### Fixed

- **`bty-flash-once` is now actually one-shot.** The `/boot/<name>?mac=`
  arm site's WHERE clause was missing `bty-flash-once`, so the
  `saw_flasher_boot` bit never got set for that mode -- which made
  the plan resolver's "bit set -> sanboot the just-flashed disk"
  branch unreachable. Every post-flash PXE contact fell through to
  the flash branch and re-served the flash chain, an infinite reflash
  loop on the mode whose entire point is "flash once". Fix is
  one-line: include `'bty-flash-once'` in the arm WHERE clause
  alongside `bty-flash-always` and `bty-inventory`. Added an e2e
  regression test (`test_e2e_flash_once_terminates_after_first_flash`)
  that asserts the bit stays set across multiple post-flash PXE
  contacts; the existing `test_e2e_boot_artifact_mac_arms_only_alternating
  _policies` test now covers all three bit-consuming policies + both
  non-armed modes (bty-tui, ipxe-exit) so this regression class can't
  slip past CI again.

## [0.30.1] - 2026-05-25

CI gap close: the release pipeline now asserts the bty wizard
actually rendered on tty1 of the freshly-baked USB ISO, not just
that the partition grew. No operator-facing behaviour change.

### Changed

- **`test-usb-grow` + `test-usb-ventoy` assert wizard renders on
  tty1.** Both tasks now grep `/dev/vcs1` (the kernel's text-snapshot
  of tty1's framebuffer) for `Pick an image source` -- a string only
  the rendered wizard produces. The wrapper's pre-Rich
  `bty is starting...` deliberately doesn't match, so a bty that
  prints the banner then crashes fails the assertion. 60s read
  budget for cold-cache import chains. Catches a real-shaped
  regression class (failed Rich init, BtyTui constructor crash,
  wrapper exit before exec) that v0.27..v0.30 would have shipped
  undetected.

## [0.30.0] - 2026-05-24

The "SSE polish" release. Two follow-ups to v0.29.0's bus migration:
push-driven progress counters and a small shared-JS extraction so
future helper additions don't fan out to three places.

### Added

- **Throttled progress events via SSE.** v0.29.0 fired SSE only on
  state transitions (queued -> running -> terminal), so a long-
  running download / hash showed a frozen byte counter until the
  30s safety poll. New `_BaseAsyncManager._fire_progress(key, state)`
  debounces per-key to at most one event per second; the catalog
  download / hash / release-fetch progress callbacks publish
  through it. Progress counters tick at ~1 Hz instead of frozen-
  for-30-seconds, without flooding the bus on a fast NVMe read.
- **`/static/bty-utils.js`** -- shared `window.btyUtils.esc(s)` +
  `window.btyUtils.fmtBytes(n)` helpers. The three pages that
  copy-pasted these (Backups, Downloads, Hashing) now alias them
  locally so a future tweak (rounding precision, byte-unit labels,
  escape semantics) is a one-place change.

### Changed

- **`_layout.html` includes `/static/bty-utils.js`** alongside the
  existing htmx + sse vendored bundles. Every authed page sees
  `window.btyUtils`.

## [0.29.0] - 2026-05-24

The "SSE everywhere" release. Worker pages stop polling every 2s and
listen to push-driven Server-Sent Events instead; refresh latency
drops from "up to 2 seconds" to "tens of milliseconds" and idle
appliance load drops accordingly.

### Added

- **Worker-events SSE stream** at `GET /events/workers`. Same in-
  process pub/sub bus that drives the machines stream, filtered to
  the `worker-state-changed` event name. Payload is a JSON triple:
  `{"kind": "backup|hash|download|release", "key": "<state-key>",
  "status": "<lifecycle>"}`.
- **State listener hook on `_BaseAsyncManager`.** Every observable
  status transition (queued -> running, queued -> cancelled in
  stop/cancel, running -> terminal in `_run_one`) fires
  `_fire_state_change(state)`. Lifespan wires each manager
  (Backup / Hash / Download / Release) to publish to the bus.
- **Shared `EventSource` in `_layout.html`.** One subscription per
  tab; dispatches `bty-worker-state-changed` CustomEvents on
  `document` so each polling page taps in without opening its own
  connection. A `bty-worker-events-connected` event fires on
  successful (re)connect so pages catch up on anything missed
  during the disconnect window.

### Changed

- **Backups / Hashing / Downloads / Netboot / Images pages migrated
  from `setInterval(refresh, 2000)` to SSE + 30-second safety
  poll.** The instant update path is the EventSource; the slow
  poll is the recovery net for a silently-dropped SSE connection
  (EventSource auto-reconnects on errors, so this is belt-and-
  braces). Each page filters by `kind` so it only refreshes when
  relevant.
- **Navbar worker-badge poll cadence relaxed from 5s to 30s.**
  Same SSE-fast-path, slow-poll-safety-net pattern; the navbar
  badges + the live indicator update on every worker transition
  via the shared EventSource.

### Performance

- Idle bty-web on a backupped fleet now generates ~1 HTTP request
  every 30 seconds per tab instead of ~3 per second across the
  worker pages -- a ~90x drop in steady-state load.

## [0.28.0] - 2026-05-24

The "auto-refresh + Backup card reshape" release. Three places where
the operator used to have to manually reload the page now refresh on
their own.

### Added

- **Backups page auto-reloads on completion.** When the polling loop
  observes a backup transitioning from active to terminal (the
  active-job count drops to 0), the page reloads so the on-disk
  listing + Recent activity cards reflect the new bundle + the new
  event without the operator pressing F5. Same trick as the
  Downloads page's `seenCompletedKeys`, but tracked as a closure-
  level `lastActiveCount`.
- **Hashing page auto-reloads on completion.** Mirrors the Backups
  pattern: when a hash job completes, the page reloads so the cached
  sha badge on /ui/images + the Recent activity card pick up the
  new state.
- **Netboot page auto-reloads when a release fetch completes.** The
  artifact present/missing badges + the Recent events card were
  server-rendered and went stale until the operator hit refresh.
  New lightweight poller checks `/boot/releases` every 2s; reloads
  when an active fetch hits a terminal state.

### Changed

- **Backup schedule card reorganised.** Retention + Destination +
  Last scheduled run move to the top of the form, above an `<hr>`;
  the optional Enable + Cadence knobs live below. Retention applies
  to every successful backup (manual or scheduled) so it's an
  always-relevant knob, not a property of the schedule.
- **/ui/images per-row Actions standardised to `btn-group btn-
  group-sm`.** Hash / Fetch / Cache-delete / Catalog-delete now sit
  flush in a Bootstrap button group, same idiom as the Backups page's
  Download + Delete column. The `ms-1` margin spacing pattern is
  gone.

## [0.27.0] - 2026-05-24

The "Backups page complete" + pre-1.0 strict-validation release.

### Added

- **On-disk backup listing on `/ui/backups`.** A new "Backups on
  disk" card lists every bundle under `$BTY_BACKUP_DIR` -- newest
  first -- with machine / catalog / image counts pulled from each
  bundle's `manifest.json`, total bytes-on-disk, and the bty
  version that produced it. Empty-state copy points the operator
  at the trigger / Settings cadence. A bundle with a missing or
  malformed manifest still lists with blank metadata so orphans
  are visible to the operator instead of silently hidden.
- **Per-bundle tar download.** Each on-disk row carries a `.tar`
  button pointing at `GET /ui/backups/{backup_id}/download`,
  which streams an uncompressed tar via `tarfile.open(mode="w|")`.
  Members are rooted at the backup_id directory so
  `tar -xf 2026-05-23T10-00-00Z.tar` produces a same-named
  folder ready for `bty-web import`. Multi-GiB bundles don't
  materialise in memory -- the per-chunk buffer holds at most
  one tar member at a time.
- **Per-bundle delete.** Each on-disk row carries a trash button
  hitting `DELETE /ui/backups/{backup_id}` (with a JS confirm
  prompt). `rmtree` + a new `backup.deleted` audit event with
  the snapshotted counts. The operator now has full CRUD over
  backups from the UI: trigger, download, delete; the scheduler
  + retention still own automatic creation + pruning.
- **Retention number visible.** The schedule summary on
  `/ui/backups` now shows `Retention: keep last N (prunes oldest
  on every successful backup)`. Retention already applied to
  every successful backup regardless of trigger; only the
  operator-facing surface was missing.

### Changed

- **`resolve_backup_*` resolvers are strict.** A non-canonical
  value in state.db now raises `SettingValueError` instead of
  silently coercing to a default. The Settings form already
  writes canonical values ("1" / "0" / known cadence / positive
  int), so live deployments are unaffected; only a hand-edit of
  state.db can trip the new path. The form handler returns 422
  (not a silent 303 with the value coerced) on unknown cadence
  or non-numeric / sub-1 retention.
- **`?saved=upstream` replaces `?saved=1`.** The upstream-settings
  form's success redirect uses the canonical key. Unknown
  `?saved=X` values now skip the banner instead of falling back
  to a generic "Saved." -- a hand-crafted URL can't echo
  arbitrary strings into the UI. Pre-1.0 policy: dropped the
  legacy `"1"` flash key with no back-compat shim.
- **`BTY_WEB_PORT` validation is strict.** A typo'd or out-of-
  range value hard-exits with status 2 + a clear error, instead
  of silently warning + falling back to 8080. Operators who set
  the env var meant it; silent fallback masks the bug until they
  notice the port didn't take effect.

### Fixed

- **`/ui/backups` no longer hides empty state.** The page previously
  required at least one in-flight or recent backup to look useful;
  fresh appliances showed an empty table with no hint of what to
  do next. The new on-disk card carries an explicit "click Back
  up now, or enable a schedule" pointer.

### Removed

- **Historical "what used to be here (v0.14 - v0.17.1)" memorial
  block in `_sysconfig.py`.** Pre-1.0 doesn't owe operators a
  tour of removed code; git history serves that purpose.
- **Defensive pre-Phase-E JS branch in `downloads.html`.** The
  fallback for `ReleaseFetchState` rows without an `artifacts`
  array served no live code path -- the per-artifact split has
  been authoritative since landing.

### Audit log

- New event kind: `backup.deleted` (operator-initiated removal
  of an on-disk bundle).

## [0.26.0] - 2026-05-24

The "workers reshape + scheduled backups + Ventoy CI" release.

### Added

- **Scheduled backups (in-UI).** `/ui/backups` carries a "Back up
  now" trigger + a schedule summary; `/ui/settings#backup-schedule`
  exposes enable/cadence/retention. The scheduler polls every 60s
  so a Settings change takes effect without restart. Backups land
  under `$BTY_BACKUP_DIR` (default `$BTY_STATE_DIR/backups/`) as
  the same bundle shape `bty-web export` produces. Two new env
  vars: `BTY_BACKUP_DIR`, `BTY_BACKUP_MAX_PARALLEL`.
- **Three operator-add triggers consolidated on `/ui/downloads`**:
  Fetch artifacts (netboot trio + sha256 manifest), Add image from
  URL (http(s):// / oras://), Upload image (local file via XHR PUT).
- **Per-file netboot artifact downloads.** A "Fetch artifacts" click
  enqueues four files; each shows as its own row in the Downloads
  list and the navbar Downloads counter ticks down per file.
- **Catalog-row state-aware buttons** on `/ui/images`. Per-row Fetch
  / Hash flips to "Downloading" / "Hashing" while a job is in
  flight; the catalog row auto-reloads when the job terminates so
  the cached badge + Action column update without manual refresh.
- **Ventoy boot test in CI** (`test-usb-ventoy`). Installs Ventoy on
  a 4 GiB loop-attached disk, drops the bty .iso + a sentinel image
  + a catalog.toml, boots via QEMU, asserts the live env's
  `bty-images-discover.service` finds the operator drop via the
  Ventoy dm-mapper passthrough.
- **New audit event kinds**: `backup.created` / `backup.failed` /
  `backup.pruned` / `settings.backup.updated`. `KNOWN_SUBJECT_KINDS`
  gains `"backup"`.
- **Audit log subject filter** on /ui/events: filterable by
  `subject_kind=backup` for backup history.

### Changed

- **`/ui/workers` (merged status page) was split into three pages**:
  `/ui/downloads` (active downloads + the three triggers + recent
  download events), `/ui/hashing` (active SHA-256 jobs + recent
  hash events), `/ui/backups` (active backups + Back-up-now +
  recent backup events). Each lights only its own navbar indicator.
- **`/ui/images` is now catalog-listing-only.** The Add-image card
  + the per-page job tables moved to `/ui/downloads` /
  `/ui/hashing` respectively. The catalog list's per-row Fetch /
  Hash buttons stay.
- **`/ui/netboot` is now inventory-only.** Drops the Fetch-artifacts
  trigger; adds a "Fetch on the Downloads page" link.
- **`bty-images-discover.service` finds Ventoy data partitions.**
  Enumerates `/dev/dm-*` devices via `blkid` (in addition to lsblk)
  so the Ventoy linear passthrough is mountable even though the
  underlying `/dev/sda1` is held by Ventoy's own dm-linear over the
  chained .iso.
- **`ReleaseFetchState` exposes per-artifact state** (`artifacts:
  dict[str, ReleaseArtifactState]`) so each file in the trio +
  sha256 manifest can be tracked + cancelled independently.
- **DownloadManager fallback to DB catalog entries.** Operator-added
  rows (via the Add-image form) live in the `catalog_entries` table
  but NOT in the parsed `catalog.toml`; the manager now looks at
  both. Without this, the per-row Fetch button on `/ui/images`
  returned a silent 404 for any URL/oras entry added through the
  form.
- **Downloads progress bar reads `bytes_downloaded` for catalog
  rows** (in addition to `bytes_done` for release artifacts).
  Pre-fix catalog downloads showed 0% in the UI regardless of
  actual progress because the JS only knew the release-artifact
  field name.

### Removed

- Legacy `/ui/workers`, `/ui/fetches`, `/ui/hashes`, `/ui/downloads`
  (merged page + sub-pages) -- replaced by the three split pages
  above. Pre-1.0 so no redirects.
- Dead `_parse_boot_order` helper from `src/bty/flash.py` + its 2
  unused tests.

### Fixed

- **Ventoy boot under QEMU**: `VTOY_SECONDARY_TIMEOUT=1` auto-
  confirms Ventoy's "Boot in normal mode" secondary menu;
  previously the test sat at the menu forever waiting for keyboard
  input the test harness can't supply.
- **`/dev/dm-1` mount inside the live env**: `bty-images-discover`
  was trying to mount `/dev/sda1` directly (held open by Ventoy's
  dm-mapper); now also probes the dm passthrough.
- **`bty-usb-grow.service` race under Ventoy**: `systemctl is-active
  --wait` is the wrong primitive (returns immediately for
  inactive-with-queued-job); switched to `is-system-running --wait`
  in the test sync barrier.
- **PXE chain test SSH diagnostics** on `/healthz` timeout: tries
  multiple credentials (`odus.321` + `odus` + cfg override) before
  declaring auth failure, so partial cloud-init / stale
  cloudinit-userdata.user runs still surface the bty-web journal.
- **BackupManager `status=completed` race**: was flipped before
  `_prune_old_backups` ran (outside the lock for non-blocking),
  letting external observers exit poll loops mid-housekeeping.
  Status now flips AFTER the prune so "completed" means truly done.
- **Docker build context loader**: `cijoe/_build/` + `cijoe-output/`
  + `cijoe-archive/` (root-owned from sudo'd live-build steps) now
  in `.dockerignore` + swept by `make docker-clean`. Pre-fix
  `make docker-build` failed with "error from sender: open
  cijoe/_build/.../credstore: permission denied".

### Documentation

- `docs/src/flows.md`: events table now covers all `KNOWN_EVENT_KINDS`
  (0/0 undocumented). Subject-kind list corrected (`boot` →
  `netboot` + added `backup`).
- `docs/src/operations.md`: new "Scheduled backups (UI-driven)"
  subsection with the on-disk layout + env vars + audit-log mapping.
- `docs/src/reference.md`: /ui/workers route block replaced with
  per-page descriptions for /ui/downloads / /ui/hashing /
  /ui/backups.
- `docs/src/walkthrough-catalog.md`: Action column description
  covers the new "Downloading" / "Hashing" busy state; page names
  match the actual URLs.

[0.26.0]: https://github.com/safl/bty/releases/tag/v0.26.0
