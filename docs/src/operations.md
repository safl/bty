# Operations: backup, upgrade, migrate

Looking after a running bty-server appliance: back its state up, upgrade
the software, and move it to new hardware (or onto a dedicated disk that
survives a reflash).

## What counts as state

A bty-server keeps everything in one directory, `BTY_STATE_DIR` (default
`/var/lib/bty`):

| Path | What | Backup? |
|---|---|---|
| `state.db` | The SQLite database: machine records, MAC->image assignments, catalog metadata, server settings, sessions, and the audit log. | **Yes** -- this is the irreplaceable bit. |
| `images/` | All image files (`BTY_IMAGE_ROOT`): operator-typed images + catalog-fetched files (named `catalog-<ref:12>-<slug>.<ext>`, v0.31.0+). | Optional -- catalog files re-fetchable from upstream; operator-typed files are irreplaceable. |
| `boot/` | The netboot artifacts (`BTY_BOOT_DIR`: kernel / initrd / squashfs). | Optional -- re-fetchable via "Fetch netboot artifacts". |
| `catalog.toml` | The active catalog manifest. | Optional -- re-fetchable from the upstream. |

A minimal backup is just `state.db`; a full backup is the whole
`/var/lib/bty` tree.

## Data separation and read-only-OS readiness

bty-server is built so that **all mutable runtime state lives under
`BTY_STATE_DIR` (`/var/lib/bty`)** -- the writable volume. The direction
is that the OS rootfs can eventually be mounted **read-only**, so a power
cut can't corrupt it and recovery is "reflash the immutable OS, re-adopt
the writable volume." That flip isn't done yet; this section is the
readiness checklist.

bty-web's runtime writes already all land under `/var/lib/bty`, split
into two classes:

| Path | Class | Notes |
|---|---|---|
| `state.db` | precious | records: machines, catalog, settings, audit log |
| `images/` | precious | all image bytes (operator-typed + catalog-fetched, expensive to refetch); v0.31.0+ merged the old `cache/` subdir in here under `catalog-<ref:12>-<slug>.<ext>` names |
| `boot/` | ephemeral | netboot artifacts -- **version-coupled**; refetch on a bty version bump |
| `session-secret` | regenerable | cookie key |

Precious = carry across a migration / back up (the `bty-web export`
bundle covers exactly these). Ephemeral = safe to lose, re-created on
demand. `boot/` is the subtle one: it lives on the writable volume (so a
read-only OS is possible) but is re-fetched when it no longer matches the
running bty-web version, rather than preserved as precious.

**OS-level holdouts** -- what still writes outside `/var/lib/bty` and so
must be handled before the rootfs can go `ro`:

| Path | Why it's written | Mitigation at flip time |
|---|---|---|
| `/etc/shadow` | `passwd` rotates the UI / SSH credential | move the credential under `/var/lib/bty`, or an `/etc` overlay |
| `/etc/issue` | boot banner writes the appliance IP | tmpfs `/etc/issue`, or render to a writable path |
| `/etc/fstab` | `bty-state-migrate` adds the data-disk mount | migrate before sealing, or fstab on an overlay |
| `/var/log` | journald | volatile journald / tmpfs `/var/log` |
| `/var/lib/cloud`, `/etc/ssh` | first boot (cloud-init, host keys) | run first boot before sealing, or persist to the volume |

The plan: keep `/var/lib/bty` the single writable volume, retire these
holdouts one at a time, then flip the rootfs to `ro` with a small
overlay / tmpfs for the unavoidable spots.

## Backup

`state.db` is a single SQLite file. The safe way to copy a live database is
SQLite's online backup (consistent even while bty-web is running):

```bash
sqlite3 /var/lib/bty/state.db ".backup '/tmp/bty-state-$(date +%F).db'"
```

A plain `cp` also works if bty-web is stopped first:

```bash
sudo systemctl stop bty-web
cp -a /var/lib/bty/state.db ~/bty-state-backup.db
sudo systemctl start bty-web
```

For a full backup (records + cached images + netboot artifacts), copy
the whole directory while bty-web is stopped:

```bash
sudo systemctl stop bty-web
sudo tar -C /var/lib -czf ~/bty-state-$(date +%F).tar.gz bty
sudo systemctl start bty-web
```

Restore by putting the file(s) back under `/var/lib/bty` (bty-web
stopped) and starting the service.

### Scheduled backups (UI-driven, since v0.25.7)

The `/ui/backups` page carries a **Back up now** trigger plus a
**Schedule** card on `/ui/settings#backup-schedule` for cadence
(`daily` / `weekly` / `manual`) + retention (keep N most recent).
The scheduler ticks every 60s; a change in Settings takes effect
on the next tick without restarting bty-web.

Each backup is a directory written under `$BTY_BACKUP_DIR`
(default `$BTY_STATE_DIR/backups`) named after the ISO-8601
timestamp, e.g. `2026-05-24T08-00-00Z/`. The bundle layout is
identical to what `bty-web export` produces (`manifest.json` +
`images/`), so a scheduled backup is interchangeable with a
manual one. Retention prunes the oldest siblings after every
successful run.

Two env vars tune the feature when the in-UI knobs aren't enough:

| Variable                     | Default                       | Meaning                                                              |
|------------------------------|-------------------------------|----------------------------------------------------------------------|
| `BTY_BACKUP_DIR`             | `$BTY_STATE_DIR/backups`      | Where backup directories land. Move off the OS disk if you want them to survive an OS reflash. |
| `BTY_BACKUP_MAX_PARALLEL`    | `1`                           | Max concurrent backup jobs. Concurrent exports race on dest dirs; leave at 1 unless you have a reason. |

History lands in the audit log under `subject_kind=backup` (kinds
`backup.created` / `backup.failed` / `backup.pruned`); the
`/ui/backups` page also surfaces the recent rows in a card at
the bottom.

## Portable export / import (operator data only)

`tar`-copying the whole tree (above) is the verbatim option. The
`bty-web export` / `import` subcommands are the **selective** one: they
move only the operator-owned half of the state -- the machine hardware
identities + image bindings, the catalog, and the local image files --
and nothing bty manages itself. Reach for them to migrate to a new
server (possibly a newer version) without dragging stale bty internals
along, or to back up just the parts you typed in.

```bash
# On the old server (reads BTY_STATE_DIR + BTY_IMAGE_ROOT):
bty-web export /tmp/bty-bundle

# Copy /tmp/bty-bundle to the new server, then:
bty-web import /tmp/bty-bundle
```

The in-UI **Back up now** trigger on `/ui/backups` produces the
same bundle shape; reach for the CLI when scripting (cron / ssh
into the appliance / packaging into an archive pipeline) and the
UI when you want an ad-hoc snapshot without leaving the browser.

What a bundle carries, and what it deliberately leaves behind:

| Travels | Stays behind (fresh on the destination) |
|---|---|
| Machine `mac` + `lshw` + disk inventory | The **boot mode** (every machine imports as `bty-inventory`) |
| Image binding + `target_disk_serial` + `hostname` | The `saw_flasher_boot` state bit + `last_flashed_at` |
| The image catalog (`catalog_entries`) | The netboot artifacts (re-fetch to match the new version) |
| The local image files (`BTY_IMAGE_ROOT`) | Server settings + the audit log |

Resetting the boot mode is the point: a freshly-migrated machine
shouldn't auto-flash against netboot artifacts you haven't refreshed
yet. Each box arrives as a re-discovered `bty-inventory` box with its
hardware + binding pre-filled; you re-enable a flash mode once the new
server is verified and its netboot artifacts re-fetched.

A bundle is a plain directory (`manifest.json` + a flat `files/`
subdir), so `tar` it for archival.

## Upgrade

bty pre-1.0 has **no database migration framework**. v0.31.0+ enforces
this strictly: bty-web carries the exact `bty.__version__` that created
`state.db` in a `bty_version` table, and refuses to start when the
running code's version doesn't match. Every release is therefore
breaking for state, by design. The fix is one of:

- **Wipe-and-restart.** Delete `state.db` (it's recreated empty on next
  start). Loses machine records, image bindings, settings, audit log;
  the image files in `BTY_IMAGE_ROOT` and netboot artifacts in
  `BTY_BOOT_DIR` survive. Operator re-binds via the UI.
- **Export-wipe-import.** Run `bty-web export <bundle-dir>` on the OLD
  release BEFORE upgrading. Upgrade. Wipe `state.db`. Run `bty-web
  import <bundle-dir>` on the new release. The slim bundle carries
  every file under `BTY_IMAGE_ROOT` plus a minimal per-machine record
  (`mac` + `hw_lshw` + `known_disks`) so hardware identity survives;
  bindings (`boot_mode`, `bty_image_ref`, `target_disk_serial`) reset
  to defaults and the operator re-binds. See "Backup".

If bty-web refuses to start with a `VersionMismatchError`, the
journal message names both the stored and the running version plus
the exact `rm` command to run. Take a backup (`bty-web export`)
before wiping.

### Upgrade in place (pip / pipx install)

If you installed `bty-lab` directly:

```bash
pipx upgrade bty-lab            # or: pip install -U bty-lab
sudo systemctl restart bty-web
```

**Re-fetch the netboot artifacts after upgrading.** The live-env
artifacts in `BTY_BOOT_DIR` (kernel / initrd / squashfs) are versioned
and fetched separately from bty-web -- the package upgrade does NOT
touch them. So a freshly-upgraded server keeps serving the *previous*
live env until you refresh it: open `/ui/netboot` and click **Fetch
latest artifacts** (or pin a tag under Settings -> Upstream sources
first). Skip this and PXE clients boot the old live env against the new
server -- a confusing version split.

### Upgrade the appliance image

The server appliance is a disk image. To move to a newer build, write the
new image (see [Set up a bty server appliance](walkthrough-server.md)) and
restore your state. A **dedicated state disk** pays off here: if
`/var/lib/bty` lives on its own disk (next section), reflashing the OS disk
leaves your records, images, and netboot artifacts intact. Without one,
restore your `state.db` backup after the reflash.

## Migrate (new hardware, or a dedicated disk)

### To new hardware

Stop bty-web on the old box, copy `/var/lib/bty` to the new one (same
path), start bty-web there. The MAC->image assignments and audit log come
with it; only the appliance's own IP changes.

### Onto a dedicated disk (survives an OS reflash)

The server image ships `bty-state-migrate`, which moves the whole state
directory onto a second disk so it persists across OS reflashes (the
CI-driven "reflash the appliance per job" workflow). It formats the target
disk ext4 with the label `BTY_IMAGE_STORE`, copies the current
`/var/lib/bty` onto it, and adds the matching `fstab` line so
`/var/lib/bty` mounts from that disk on every boot:

```text
LABEL=BTY_IMAGE_STORE /var/lib/bty ext4 nofail,x-systemd.device-timeout=10s 0 2
```

Run it on the appliance with the second disk attached (it prompts before
formatting unless you pass `--yes`; it refuses to format the rootfs disk).
After that, reflashing the OS disk and rebooting brings the same state back
automatically: every appliance image bakes that `fstab` line in, with
`nofail` so a diskless appliance still boots off the rootfs `/var/lib/bty`.
When a `BTY_IMAGE_STORE` disk is present it mounts at `/var/lib/bty`, so a
freshly reflashed appliance re-adopts the existing state disk with no
manual step.
