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
| `images/` | The local image cache (`BTY_IMAGE_ROOT`). | Optional -- re-fetchable from the catalog. |
| `boot/` | The netboot artifacts (`BTY_BOOT_DIR`: kernel / initrd / squashfs). | Optional -- re-fetchable via "Fetch netboot artifacts". |
| `catalog.toml` | The active catalog manifest. | Optional -- re-fetchable from the upstream. |

A minimal backup is just `state.db`; a full backup is the whole
`/var/lib/bty` tree.

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

A bundle is a plain directory (`manifest.json` + an `images/` folder),
so `tar` it for archival.

## Upgrade

bty pre-1.0 has **no database migration framework**. Two cases, and the
common one is painless:

- **Additive schema changes (the usual case).** New defaulted / nullable
  columns are added automatically with `ALTER TABLE ... ADD COLUMN` on the
  next start. Your records survive untouched. Most releases are this.
- **Breaking schema changes (rare).** If a release changes a *required*
  column, bty-web refuses to start against an old `state.db` rather than
  silently corrupting it; the fix is to **delete `state.db`** (rebuilt
  empty on next start). Such releases call this out in their notes. **Back
  up first** (above) so you can re-enter records, or let auto-discovery +
  the operator re-assign.

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
