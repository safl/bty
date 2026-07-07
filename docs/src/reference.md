# Reference

Reference material for bty's surfaces. Filled in as features land.

## Pre-built release artifacts

Each tagged release publishes a fixed set of assets to GitHub. The
`releases/latest/download/<filename>` URLs always 302 to the newest tag's
copy; substitute `latest` for a specific tag (e.g. `v0.11.1`) to pin.

| Asset | What it is | URL (latest) |
|---|---|---|
| `bty-usbboot-pc-x86_64-v*.iso` (+ `.sha256`) | Bootable USB live ISO with a built-in writable `BTY_IMAGES` exFAT partition (32 MiB at bake; auto-grows to fill the stick on first boot via `bty-usb-grow.service`). Uncompressed: open in Etcher / RPi Imager / Rufus / dd directly. CLI: `dd if=bty-usbboot-pc-x86_64-v*.iso of=/dev/sdX bs=4M`. | <https://github.com/safl/bty/releases/latest/download/bty-usbboot-pc-x86_64.iso> |
| `bty-usbboot-rpi-arm64-v*.img.gz` (+ `.sha256`) | arm64 Raspberry-Pi flasher: a Pi-bootable raw disk image (FAT32 firmware + ext4 live squashfs + auto-growing exFAT `BTY_IMAGES`). Boots on CM5 / Pi5 / Pi4 from USB and runs the same bty TUI as `usbboot-pc`; targets local eMMC / NVMe / SD. CLI: `gunzip -c bty-usbboot-rpi-arm64-v*.img.gz \| sudo dd of=/dev/sdX bs=4M conv=fsync`. | <https://github.com/safl/bty/releases/latest/download/bty-usbboot-rpi-arm64.img.gz> |
| `bty-ipxe-x86_64-v*.efi` | bty's custom iPXE UEFI binary with the embedded chain to `/pxe-bootstrap.ipxe`. Served by bty-web over HTTP for UEFI HTTP Boot and baked into the `bty-tftp` sidecar image. | <https://github.com/safl/bty/releases> |
| `bty-netboot-pc-x86_64-v*.{vmlinuz,initrd,squashfs}` (+ `.sha256`) | Netboot trio for PXE-flash clients. Drop into the server's `BTY_PATHS_BOOT_DIR` (or click "Fetch netboot artifacts" on `/ui/netboot`). | <https://github.com/safl/bty/releases/latest/download/bty-netboot-pc-x86_64.vmlinuz> |
| `catalog.toml` | The default image catalog (`oras://ghcr.io/safl/nosi/...` entries) the `bty` wizard offers as `[d] default`. Published by the upstream image-builder, not by bty itself. | <https://github.com/safl/nosi/releases/latest/download/catalog.toml> |
| `release.toml` | Release manifest: the version plus the asset filenames for the tag. Stable URL for "what's the latest". | <https://github.com/safl/bty/releases/latest/download/release.toml> |
| `bty.pdf` | Offline copy of the docs (this site, rendered by Sphinx + LaTeX). | <https://github.com/safl/bty/releases/latest/download/bty.pdf> |
| `bty_lab-X.Y.Z.tar.gz` | Source release (sdist). Archival; install via `pipx install bty-lab` from [PyPI](https://pypi.org/project/bty-lab/) instead. The PyPI wheel is the canonical install path; no wheel is attached to the GitHub release. | <https://github.com/safl/bty/releases> |

The browser path is <https://github.com/safl/bty/releases>; the JSON
API for build automation is `GET /repos/safl/bty/releases/latest`.

## CLI

`bty` is a Rich-based wizard that picks an image + a target disk and
flashes. Three invocation shapes:

```text
bty                              # interactive wizard, local image-root only
bty --catalog <URL>              # interactive wizard, catalog pre-loaded
bty --server <X> --mac <Y>       # server-driven mode (flash / interactive
                                 # / inventory / exit) chosen by GET <X>/pxe/<Y>/plan
```

`bty --version` prints the installed version (sourced from package
metadata) and exits. `bty --help` documents every flag inline.

### `--server URL` (default `bty-server`)

bty-server base URL or hostname. Bare hostnames are accepted; missing
scheme defaults to `http://`. Pair with a LAN DNS entry (or `/etc/hosts`
line) pointing at the bty-web host and `bty --mac X` just works. The
PXE-booted live env sets this from the kernel cmdline (`bty.server=...`).

### `--mac MAC`

Self-MAC of this client (e.g. `aa:bb:cc:dd:ee:ff`). When supplied, `bty`
switches to **server-driven mode**: it POSTs the local disk inventory to
`<server>/pxe/<mac>/inventory`, then GETs `<server>/pxe/<mac>/plan` and
dispatches on the JSON response:

| `plan.mode` | What happens |
|---|---|
| `flash` | Flash without prompts (the plan carries the image URL + target serial picked on the server side), then POST `/pxe/<mac>/done` and reboot. |
| `interactive` | Drop into the wizard with the plan's catalog pre-loaded. Operator picks image + disk. |
| `inventory` | Post the disk inventory, then reboot (no flash, no wizard). The next PXE contact boots the disk. Used by `boot_mode=bty-inventory`. |
| `exit` | Print a notice and exit. Firmware / local-disk boot handles it. |

Network / parse failures fall through to `interactive` with the server's
`/catalog.toml` as the catalog source, so the operator still has something
to act on.

### `--catalog URL`

Catalog URL or path to pre-load (http(s):// for HTTP, oras:// for OCI, or a
local file path). When given, the SELECT_CATALOG screen is skipped and the
wizard jumps straight to SELECT_IMAGE with the catalog overlaying the local
image-root. Equivalent to picking `[c] custom` on the source screen and
typing the URL.

Ignored in server-driven mode (`--mac` set): the server supplies the
catalog as part of `/pxe/<mac>/plan`.

### Catalog sources

`--catalog` accepts the same shapes the wizard's `[c] custom` prompt does:

- **Local TOML file** (`/path/to/catalog.toml`).
- **HTTP URL** (`https://example.com/catalog.toml`).
- **`oras://` reference** (`oras://ghcr.io/owner/bty-catalog:latest`).
- **bty-web instance** (`http://server:8080/catalog.toml`).

The catalog TOML schema is `bty.catalog.Catalog` (version 1):

```toml
version = 1

[[images]]
name = "demo.qcow2"
src = "https://example.com/images/demo.qcow2"
sha256 = "abc123..."  # optional; required for sha-pinned bty-web entries
format = "qcow2"
size_bytes = 1024
```

`src` accepts `http(s)://`, `oras://`, or `file://`. `sha256` is
optional in the schema; rolling tags (`oras://...:latest`) leave
it null because the digest is resolved at flash time.


### Recognised image formats

- `.qcow2` -- decompressed via `qemu-img convert`.
- `.img` -- raw image; `dd` directly.
- `.img.zst` -- `zstd -d --stdout | dd`.
- `.img.xz` -- `xz -d --stdout | dd`.
- `.img.gz` -- `gzip -d --stdout | dd`.
- `.img.bz2` -- `bzip2 -d --stdout | dd`.

Tarballs (`.tar.gz`, `.tgz`, etc.) are **not** supported: the gzip/xz/bzip2
layer applied to a tarball yields a TAR stream, not an image, and writing
TAR headers into the MBR is a wrong-answer. Extract first.

gzip is the safe default for distributed images: Etcher / Rufus / Imager /
dd all decompress it natively, without the version-cliff issues that bit us
with xz (Etcher's bundled xz handler) and zstd (older Etcher pre-1.18). The
flash path inside the wizard accepts every format above for
operator-supplied target images.

### Image root (bty CLI only)

The `bty` wizard scans a local directory for flashable image files
on the host it runs on -- typically the USB live env's `BTY_IMAGES`
exFAT partition. Resolved in this order:

1. `BTY_IMAGE_ROOT` environment variable.
2. `/var/lib/bty/images` (the USB live env auto-mounts the
   `BTY_IMAGES` partition here).

bty-web (v0.40+) does NOT use this directory; it has no image-store.
See [walkthrough-image-store](walkthrough-image-store.md) for the
server-side bytes model (withcache + URL-only catalog entries).

## Configuration

bty resolves a small set of paths and runtime knobs from the environment
and sensible defaults.

### Environment variables

| Variable | Purpose | Default |
|-------------------|----------------------------------------------------------------|---------------------|
| `BTY_IMAGE_ROOT` | Image root the `bty` wizard scans (CLI only; bty-web ignores it). | `/var/lib/bty/images` |
| `BTY_REGISTER_UEFI_BOOT` | Opt in (`1`/`true`/`yes`/`on`) to register a UEFI NVRAM boot entry (one-shot `BootNext`) for the disk after a flash. Off by default: most firmware boots the flashed disk on its own, and touching NVRAM is risky on some server boards. | (unset = off) |

### Default paths

- `/var/lib/bty/` -- bty-web state directory. Holds `state.db` +
  `boot/` (netboot artifacts) + `catalog.toml` (the active
  manifest) + `session-secret`. v0.40+: no image-store subdirectory.
- `/var/lib/bty/images` -- USB live env's auto-mount point for the
  `BTY_IMAGES` partition. Used only by the `bty` CLI, not bty-web.
  See [walkthrough-image-store](walkthrough-image-store.md) for the
  bty-web server-side model (withcache + URL-only catalog entries).

## Python API

bty's modules are usable as a library. Stable entry points:

| Module | Purpose |
|------------------|-----------------------------------------------------------|
| `bty.disks` | `list_disks() -> list[dict]` - block-device discovery. |
| `bty.images` | `list_images(root)`, `inspect_image(path)`, `Image` dataclass, `detect_format(path)`, `default_image_root()`. |
| `withcache.oras` | `parse_ref(ref) -> OrasRef`, `resolve_ref(ref) -> ResolvedBlob`, `is_oras_url(url) -> bool`, `OrasError`. ORAS / OCI registry adapter for `oras://` URLs. Moved out of `bty.oras` upstream to withcache in v0.59.0 so the cache-host and library callers share one implementation. |
| `bty.catalog` | `Catalog`, `load_source(src)`, `load_bytes(...)`, `fetch_bytes(...)`. Portable catalog TOML loader. |
| `bty.flash` | `execute_plan(plan, progress=, cancel=)`, `FlashPlan`, `FlashProgress`, `FlashError`. The flash machinery the wizard sits on top of. |

A full sphinx-autodoc surface is on the roadmap. Until then treat any module
not listed above as internal.

## HTTP API

`bty-web` exposes a FastAPI server, backed by a single SQLite file at
`$BTY_PATHS_STATE_DIR/state.db` (default `/var/lib/bty/state.db`).

### Auth

Single-admin-password authentication. The operator UI is gated by
``$BTY_ADMIN_PASSWORD``; when it is unset the UI is open (bty-web logs a
startup warning). Rotate by changing the env var and restarting bty-web.
``POST /ui/login`` (form-encoded ``password=...``) constant-time-compares
the password against ``$BTY_ADMIN_PASSWORD`` and flips
``request.session["bty_authed"] = True``; the session is a server-signed
cookie managed by Starlette's :class:`SessionMiddleware` (cookie name
``bty-token``, sliding 7-day TTL). No DB-backed session table: the cookie
value is the session, signed against the per-instance key at
``/var/lib/bty/session-secret`` (generated by ``bty-web-init`` on first
start). ``POST /ui/logout`` clears the session.

Open routes, reachable by PXE clients and other live-env tooling that can't
carry a session cookie:

- `GET /healthz` - `{"status": "ok"}`
- `GET /version` - `{"version": "..."}`
- `GET /pxe/{mac}` - per-MAC iPXE script (`text/plain`). The
 response depends on the machine's `boot_mode`:
 - `ipxe-exit` (default): boot the local disk, firmware-aware via iPXE's
 `${platform}`. On UEFI the script is `iseq ${platform} efi && exit` -
 hand back to the firmware boot order, which boots the disk's EFI
 loader (UEFI has no BIOS INT13 drive map, so `sanboot --drive` can't
 work there). On legacy BIOS it's `sanboot --no-describe --drive
 <sanboot_drive>` (default `0x80`) with `|| exit` falling back to the
 firmware order. A machine with no usable assignment (or a stale
 policy) falls through to the same. Auto-discovery still applies to
 unknown MACs.
 - `bty-flash-always` / `bty-flash-once` + image assigned + target
 serial picked: chain into the live env over HTTP with kernel cmdline
 `bty.server=` + `bty.mac=`. The live env's ``bty`` then GETs
 `/pxe/<mac>/plan` to retrieve the image URL + target_disk_serial and
 runs the flash.

 Auto-discovery: the first contact for an unknown MAC inserts a
 placeholder row (image=null, boot_mode=bty-inventory) so the box
 self-reports its disks and just boots; the operator sees it in
 `GET /machines` with a populated disk dropdown and can claim it with
 `PUT /machines/{mac}`. Repeat contacts update `last_seen_at` /
 `last_seen_ip`. Trust model: bty-web is for a homelab / CI network, not
 the open internet - anyone reachable can write discovery rows.
- `POST /pxe/{mac}/done` - completion signal from the live env after a
 successful flash. Updates `last_flashed_at` and **never** mutates
 `boot_mode`. The post-flash "boot the disk" behaviour comes from the
 `saw_flasher_boot` bit, not a mode rewrite: `bty-flash-once` keeps the
 bit set (boots the disk thereafter, still reading `bty-flash-once`),
 `bty-flash-always` clears it (re-arms the flash chain - the per-job CI
 cadence). bty-web runs no post-flash provisioning; the target reboots
 into whatever the pre-built image brings up via cloud-init.
- `GET /pxe-bootstrap.ipxe` - static iPXE script that dnsmasq points iPXE
 clients at on their second-stage DHCP. Returns
 `chain http://<host>/pxe/${net0/mac:hexhyp}` where `<host>` is the
 request's `Host` header, so the client always loops back to whichever IP
 / hostname / .local name it used to reach the server.
- `GET /boot/{name}` - serve a live-env artifact from `BTY_PATHS_BOOT_DIR`
 (default `/var/lib/bty/boot/`). Same trust model as `/pxe/*`. Operators
 populate the dir via the browser UI's "Fetch netboot artifacts" button on
 the Netboot page, or with the auth-gated `PUT /boot/{name}` upload route.
- `GET /images` - list the catalog (array of `ImageEntry`). Open so the
 PXE-booted ``bty`` flow can enumerate from inside the live env without
 bootstrapping a session. The companion `GET /images/{key}[/{name}]`
 stream-proxy was removed in v0.60.0: oras catalog entries now reach
 the live env either through withcache (when configured) or as the raw
 `oras://` URL the live env's bty TUI handles itself via
 `withcache.oras` (resolve + bearer + curl).
- `GET /catalog.toml` - same row set as `GET /images`, serialised as a
 `bty.catalog.Catalog` TOML manifest (``version = 1``, ``[[images]]``
 tables). Open for the same reason; consumed by `bty --catalog` so the
 same client code path that handles static files (e.g. on GitHub releases)
 works against a live bty-web.

Protected routes (session cookie required):

| Method | Path | Body | Returns |
|---|---|---|---|
| GET | `/machines` | - | array of `Machine` |
| GET | `/machines/{mac}` | - | `Machine` (404 if missing) |
| GET | `/machines/{mac}/lshw.json` | - | raw `lshw -json` blob (404 if none posted) |
| GET | `/machines/{mac}/disks.json` | - | lsblk-derived disk inventory JSON (404 if none posted) |
| PUT | `/machines/{mac}` | `MachineUpsert` | `Machine` (the new state) |
| DELETE | `/machines/{mac}` | - | 204 (404 if missing) |
| POST | `/admin/withcache/refresh` | - | 303 -> `/ui/machines` (forces `WithcacheCatalog.refresh()`) |
| GET | `/images` | - | array of bindable images (backed by `WithcacheCatalog`, filtered to downloaded rows) |
| GET / POST / DELETE | `/workers/backups` | (BackupManager) | trigger / list / cancel backups |
| GET / POST / DELETE | `/boot/releases` | (ReleaseFetchManager) | trigger / list / cancel netboot-artifact pulls |

Since v0.66.0 all catalog mutation lives on withcache. Bty-web
reads via `WithcacheCatalog` (an in-process snapshot backed by
`GET <BTY_WITHCACHE_URL>/catalog`). Add / Download / Delete on
the withcache UI; bty picks up the new state on the next
`WithcacheCatalog.refresh()` (auto on start + on-demand via
`/admin/withcache/refresh`).

### Schema mismatch on upgrade (v0.33.0+)

When bty-web starts and finds a `state.db` whose `bty_version`
disagrees with the running release (or no marker at all -- a
pre-versioning DB), `bty.web._db.init_db` rotates the old DB to
`state.db.<from>.<UTC-iso>.bak` and creates a fresh one. A
`system.schema.reset` event with `details = {from_version,
to_version, archived_at}` is recorded in the fresh DB.

The rotation surfaces as an unacknowledged event on the dashboard
tripwire; acknowledge from `/ui/events`. The `.bak` file is a
normal sqlite DB an operator can open with `sqlite3` to recover
specific rows. See operations.md for the full upgrade flow.

MAC addresses are accepted in any case + `:`-or-`-` separated, and
normalised to lower-case `aa:bb:cc:dd:ee:ff`.

### Wire types

```
Machine = {
  "mac": "aa:bb:cc:dd:ee:ff",
  "bty_image_ref": "<64-hex>" | null,        # null = discovered but unassigned
                                             # matches a withcache catalog entry's
                                             # ref (sha256 of canonicalised src URL)
  "labels": ["rack-3", "noisy", ...],        # free-form display tags; each
                                             # alnum-leading + alnum/space/-/_/.,
                                             # max 64 chars per tag, 16 per machine,
                                             # alphabetical on read
  "discovered_at": "<ISO 8601>" | null,      # first /pxe contact; null if PUT-only
  "last_seen_at":  "<ISO 8601>" | null,      # most recent /pxe contact
  "last_seen_ip":  "203.0.113.42" | null,
  "boot_mode":   "ipxe-exit"               # one of ipxe-exit /
                 | "bty-flash-always"        # bty-flash-always /
                 | "bty-flash-once"          # bty-flash-once /
                 | "bty-tui"                 # bty-tui / bty-inventory;
                 | "bty-inventory",          # what /pxe/{mac} returns
  "sanboot_drive": "0x80" | null,            # iPXE BIOS drive for sanboot
                                             # (null = default 0x80)
  "last_flashed_at": "<ISO 8601>" | null,    # set by POST /pxe/{mac}/done
  "known_disks":   [{ ... InventoryDisk ... }] | null,
                                             # most recent POST /pxe/{mac}/inventory;
                                             # populates the /ui/machines/{mac}
                                             # target-disk dropdown
  "known_disks_at": "<ISO 8601>" | null,     # when the inventory above was posted
  "target_disk_serial": "<vendor serial>" | null,
                                             # operator pick from known_disks;
                                             # required for plan.mode=flash
  "created_at":    "<ISO 8601>",
  "updated_at":    "<ISO 8601>"
}

MachineUpsert = {
  "bty_image_ref": "<64-hex>" | null,
  "labels": [str, ...],                        # free-form display tags;
                                               # set-semantic (the list replaces
                                               # all prior labels for this MAC).
                                               # Default [] when omitted.
  "boot_mode": "ipxe-exit"                 # default "ipxe-exit" on PUT;
              | "bty-flash-always"           # auto-discovery sets
              | "bty-flash-once"             # "bty-inventory"; the
              | "bty-tui"                    # flash policies require a
              | "bty-inventory",             # target_disk_serial
  "sanboot_drive": str | null,               # iPXE BIOS drive for sanboot
                                             # (e.g. "0x80"; null = default)
  "target_disk_serial": str | null           # required when boot_mode is
                                             # bty-flash-always / -once --
                                             # /ui/machines/{mac} POST
                                             # refuses without it
}

ImageEntry (as returned by `GET /images`) = {
  "name":       "debian.qcow2",
  "format":     "qcow2",
  "size_bytes": 268435456,
  "url":        "https://..." | "oras://...",  # the src URL withcache
                                              # holds bytes for
  "ref":        "<64-hex>",                    # sha256(canonicalise_src(src));
                                              # the value to PUT as
                                              # MachineUpsert.bty_image_ref
  "sha_short":  "<12-hex>" | null              # display-only prefix
                                              # of the disk-image sha
}

InventoryDisk = {
  "path":      "/dev/sda",                    # /dev path at inventory time
                                              # (not the durable id)
  "size":      "500G" | null,                 # lsblk human-readable string
  "vendor":    "ATA" | null,
  "model":     "Samsung 980" | null,
  "serial":    "<vendor serial>" | null,      # the durable id; used at
                                              # flash time
  "tran":      "sata" | "nvme" | "usb" | null,
  "removable": false,
  "readonly":  false
}
```

The `POST /pxe/{mac}/inventory` body is `{"disks": [InventoryDisk, ...]}`
plus an optional `"lshw"` field carrying the full `lshw -json` hardware
tree (CPU / RAM / NICs + MACs / peripherals / firmware). `bty` collects
it on every live-env boot. It is **supplementary**: the flasher only
consumes `disks` (from lsblk); `lshw` is stored as a blob, surfaced on
the Machine view, and downloadable raw at
`GET /machines/{mac}/lshw.json` (size-capped server-side; an oversize
or absent blob leaves any prior one intact).

### Configuration

The canonical operator config is a ``bty.toml`` file (located via
``BTY_CONFIG_FILE`` / ``BTY_CONFIG_DIR``, or the default search list
``/etc/bty/conf.d/`` -> ``/etc/bty/bty.toml`` ->
``<state_dir>/bty.toml``), with per-key env overrides following the
``BTY_<SECTION>_<KEY>`` convention.

| Variable | Purpose | Default |
|---|---|---|
| `BTY_PATHS_STATE_DIR` | Where `state.db` lives | `/var/lib/bty` |
| `BTY_PATHS_BOOT_DIR` | Live-env artifacts (`/boot/{name}` source) | `${BTY_PATHS_STATE_DIR}/boot` |
| `BTY_BOOT_RELEASE_REPO` | GitHub repo (`<owner>/<name>`) the "Fetch netboot artifacts" UI pulls live-env artifacts from | `safl/bty` |
| `BTY_SERVER_HOST` | uvicorn bind address | `0.0.0.0` |
| `BTY_SERVER_PORT` | uvicorn port | `8080` |

### Browser UI (`/ui`)

`bty-web` ships a server-rendered browser UI under `/ui` (Jinja templates,
Bootstrap CSS, HTMX form posts).

- `GET /ui` -> 303 redirect to `/ui/dashboard`
- `GET /ui/login` -> login form
- `POST /ui/login` -> constant-time-compares the password against ``[admin] password`` from ``bty.toml`` (env override `BTY_ADMIN_PASSWORD`; default `bty-lab`) and flips
 ``request.session["bty_authed"] = True``; SessionMiddleware emits
 the signed `bty-token` cookie on the redirect response
 (``SameSite=Strict``).
- `POST /ui/logout` -> ``request.session.clear()``; SessionMiddleware
 emits a deletion cookie.
- `GET /ui/dashboard` -> overview (machine count, discovered count,
 image count) + sanity-checklist card (one row per readiness
 condition: netboot artifacts present / catalog non-empty / TFTP
 daemon running, with deep-links into the relevant page when a
 condition fails) + recent-activity slice
- `GET /ui/machines` -> table of all machines with a "discovered"
 badge for unassigned rows; auto-refreshes via SSE
- `GET /ui/machines/{mac}` -> detail + edit form
- `POST /ui/machines/{mac}` -> upsert from a form submit
- `POST /ui/machines/{mac}/delete` -> delete record
- (bty-web /ui/images retired in v0.66.0 -- catalog now lives on
 withcache. Bindable entries surface on `/ui/machines/{mac}`'s
 image picker; add + Download entries on the withcache UI at
 `<BTY_WITHCACHE_URL>/ui/catalog`.)
- `GET /ui/netboot` (Netboot) -> the netboot artifacts inventory
 (present/missing per artifact, sizes, last-fetched timestamps) +
 the Fetch artifacts trigger and active-fetch table (release trio +
 sha256 manifest) + an observation-only **TFTP daemon** panel: the
 live `systemctl is-active dnsmasq.service` state badge plus a
 short triage hint. Lifecycle (start/stop/restart) is left to
 systemd / Podman; the UI no longer drives it. An in-page sub-nav
 jumps between Artifacts / TFTP Daemon / Activity.
- `GET /ui/backups` (Backups) -> Back-up-now trigger + active
 backups list + schedule summary (links to the Settings backup-
 schedule card) + recent ``backup.created`` / ``backup.failed`` /
 ``backup.pruned`` events. Each worker page lights only its own
 navbar indicator.
- The router-config **DHCP / Network boot** cheatsheet (host-IP /
 interfaces table + option 60 / 66 / 67 values to paste into the LAN's
 DHCP server, for both PXE-via-TFTP and UEFI HTTP Boot) lives on the
 Settings page (`/ui/settings#dhcp-pxe`). bty does NOT run any DHCP
 role; the operator's existing DHCP server points clients at this
 host for TFTP + HTTP-Boot fetches.
- `POST /ui/netboot/fetch-release` -> downloads
 `vmlinuz`/`initrd`/`squashfs`/`sha256` from
 `https://github.com/<BTY_BOOT_RELEASE_REPO>/releases/<tag>/download/`
 (default `safl/bty`, default tag `latest`); verifies the manifest
 and atomically installs into `BTY_PATHS_BOOT_DIR`.
- `GET /ui/settings` -> the config page: the editable **Withcache
 upstream** card (URL bty-web reads the catalog from) full-width on
 top, **Netboot release** (release repo + tag) and **Backup schedule**
 (enabled / cadence / retention) cards side-by-side, then read-only
 **Identity / Storage / Network** config groups (each row's source:
 env var / TOML / default, with an inline edit form when sourced from
 TOML), plus the **DHCP / Network boot** router cheatsheet. Operator authentication
 is on the separate Account page (`/ui/account`, reached via the user
 pill): the credential is ``[admin] password`` in ``bty.toml`` (env
 override `BTY_ADMIN_PASSWORD`), rotated by changing the value and
 restarting bty-web; to invalidate every session at once, rotate the
 cookie-signing secret with `rm /var/lib/bty/session-secret &&
 systemctl restart bty-web`.
- `POST /ui/settings/upstream` -> persists the netboot repo / tag and
 the withcache URL into the `settings` table; fetch routes resolve
 from this at request time so the changes take effect without a
 restart.
- `POST /ui/settings/backup` -> persists the scheduled-backup knobs
 (enabled / cadence / retention); the scheduler picks them up on the
 next 60s tick.
- `POST /ui/settings/config/edit` -> per-row inline edit form for the
 read-only config groups (rows whose source is `toml` carry an Edit
 affordance); the handler validates the field, round-trips the value
 through tomlkit to preserve operator formatting, and reloads the
 active config inline so the next render reflects the change.

The auth dependency checks ``request.session.get("bty_authed")``; the
session is a Starlette ``SessionMiddleware``-signed payload carried in the
``bty-token`` cookie, so no per-request DB hop is needed. Logging out clears
the session dict; ``SessionMiddleware`` emits a deletion cookie.

#### Static assets (offline-friendly)

Bootstrap CSS, HTMX, and the HTMX SSE extension are **vendored** into the
wheel under `bty.web._static/` and served at `/static/`. bty-web
contacts no CDN at runtime; all browser code is served from the same
origin. See `src/bty/web/_static/README.md` for asset versions and the
refresh procedure.

#### Live updates (`GET /events/machines`)

The machines table subscribes to a Server-Sent Events stream so the
operator need not refresh after PXE auto-discovery or another admin's edit.
The endpoint:

- Authenticates with the same session-cookie dep as the rest of the API.
 Browsers carry the cookie automatically; the SSE `EventSource` API does
 not let you set custom headers.
- Sends `Content-Type: text/event-stream` and an initial `machines-update`
 event containing the current `<tbody>` snapshot on connect.
- Emits a fresh `machines-update` event after every mutation (`PUT
 /machines/{mac}`, `DELETE /machines/{mac}`, the corresponding `/ui` form
 posts, and PXE auto-discovery on `/pxe/{mac}`).

The fan-out bus is in-process; slow consumers are silently dropped (every
event carries the full snapshot, so they catch up on the next mutation).
**Single uvicorn worker** is required: a multi-worker deployment would need
a real broker (Redis pub/sub, NATS, ...), overkill for a single bty-web
serving a homelab fleet.

## Configuration schemas

Schemas for the on-disk configuration files used by `bty` and
`bty-web`. Populated alongside the relevant features.

## State export / import format

v0.33.2+ (`bty_export_version = 3`): a directory containing a single
`inventory.json`. No image bytes; v1 (pre-v0.31.0) and v2
(v0.31.0..v0.33.1, with image bytes) bundles are refused on import.

`inventory.json` shape (the `...` placeholders below stand in for
elided keys / nested children; the live file is strict JSON):

```text
{
  "bty_export_version": 3,
  "exported_at": "2026-05-25T14:30:00+00:00",
  "exported_by_bty_version": "0.33.2",
  "machines": [
    {
      "mac": "aa:bb:cc:dd:ee:ff",
      "known_disks": [{"path": "/dev/sda", "serial": "..."}],
      "known_disks_at": "2026-05-25T10:00:00+00:00",
      "hw_lshw": {"id": "system", "product": "...", "children": [...]},
      "hw_lshw_at": "2026-05-25T10:00:00+00:00"
    }
  ]
}
```

`known_disks` and `hw_lshw` are native objects/arrays (not
re-encoded JSON strings), so `jq '.machines[].hw_lshw.product'`
works directly. Import inserts each machine as
`boot_mode=bty-inventory` with bindings cleared; the operator
re-binds image + boot mode after `bty-web import`.
