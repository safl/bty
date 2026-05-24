# Changelog

This file follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
The format reflects what actually matters to an operator running bty
(the `bty-lab` PyPI package + `bty-web` container) -- behaviour the
operator perceives, defaults that survived a `pip install -U`, and
gates that landed in CI.

Per-release commit history lives in `git log`; this file captures the
operator-facing summary.

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
