# Reference

Reference material for bty's surfaces. Filled in as features land.

## Pre-built release artifacts

Each tagged release publishes a fixed set of assets to GitHub. The
`releases/latest/download/<filename>` URLs always 302 to the newest tag's
copy; substitute `latest` for a specific tag (e.g. `v0.11.1`) to pin.

| Asset | What it is | URL (latest) |
|---|---|---|
| `bty-usb-x86_64.iso` (+ `.sha256`) | Bootable USB live ISO with a built-in writable `BTY_IMAGES` exFAT partition (32 MiB at bake; auto-grows to fill the stick on first boot via `bty-usb-grow.service`). Uncompressed: open in Etcher / RPi Imager / Rufus / dd directly. CLI: `dd if=bty-usb-x86_64.iso of=/dev/sdX bs=4M`. | <https://github.com/safl/bty/releases/latest/download/bty-usb-x86_64.iso> |
| `bty-server-x86_64.img.gz` (+ `.sha256`) | Server appliance image, x86_64 (browser UI + iPXE + dnsmasq). Boot in QEMU or `dd` to a disk. | <https://github.com/safl/bty/releases/latest/download/bty-server-x86_64.img.gz> |
| `bty-server-rpi-arm64.img.gz` (+ `.sha256`) | Server appliance image for Raspberry Pi 4 / 5 (arm64). Write with `dd` to an SD card. | <https://github.com/safl/bty/releases/latest/download/bty-server-rpi-arm64.img.gz> |
| `bty-netboot-x86_64.{vmlinuz,initrd,squashfs}` (+ `bty-netboot-x86_64.sha256`) | Netboot trio for PXE-flash clients. Drop into the server's `BTY_BOOT_DIR` (or click "Fetch netboot artifacts" on `/ui/netboot`). | <https://github.com/safl/bty/releases/latest/download/bty-netboot-x86_64.vmlinuz> |
| `bty.pdf` | Offline copy of the docs (this site, rendered by Sphinx + LaTeX). | <https://github.com/safl/bty/releases/latest/download/bty.pdf> |
| `bty_lab-X.Y.Z-py3-none-any.whl` / `.tar.gz` | Python wheel + sdist. Mirrored on PyPI as [`bty-lab`](https://pypi.org/project/bty-lab/) - prefer `pipx install bty-lab` over downloading by hand. | <https://github.com/safl/bty/releases> |

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
line) pointing at the appliance and `bty --mac X` just works. The
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

bty ships its appliance images (`bty-server-x86_64.img.gz`,
`bty-server-rpi-arm64.img.gz`) as gzip for
universal flasher support: Etcher / Rufus / Imager / dd all decompress gzip
natively, without the version-cliff issues that bit us with xz (Etcher's
bundled xz handler) and zstd (older Etcher pre-1.18). The flash path inside
the wizard accepts every format above for operator-supplied target images.

### Image root

The local image-root is resolved in this order:

1. `BTY_IMAGE_ROOT` environment variable.
2. `/var/lib/bty/images` (the USB live env auto-mounts the
   `BTY_IMAGES` exFAT partition here; the server appliance
   auto-mounts a `LABEL=BTY_IMAGE_STORE` second disk here so the
   cache survives reflashes).

## Configuration

bty resolves a small set of paths and runtime knobs from the environment
and sensible defaults.

### Environment variables

| Variable | Purpose | Default |
|-------------------|----------------------------------------------------------------|---------------------|
| `BTY_IMAGE_ROOT` | Image root the `bty` wizard scans. | `/var/lib/bty/images` |
| `BTY_REGISTER_UEFI_BOOT` | Opt in (`1`/`true`/`yes`/`on`) to register a UEFI NVRAM boot entry (one-shot `BootNext`) for the disk after a flash. Off by default: most firmware boots the flashed disk on its own, and touching NVRAM is risky on some server boards. | (unset = off) |

### Default paths

- `/var/lib/bty/images` - image root. The USB live appliance
 auto-mounts the `BTY_IMAGES` partition here. The server appliance
 auto-mounts a `LABEL=BTY_IMAGE_STORE` disk here (if one is present)
 so the image cache survives reflashes; see
 [walkthrough-image-store](walkthrough-image-store.md).

### Appliance helpers

These ship with the bty-server appliance only (not the bty-lab
Python package).

| Helper | Purpose |
|---|---|
| `bty-state-migrate [--yes] DEVICE` | Move the whole bty state dir `/var/lib/bty` (images, netboot artifacts, content cache, `state.db`) onto a 2nd disk (ext4, label `BTY_IMAGE_STORE`, mounted at `/var/lib/bty`) so it survives an OS reflash. Stops bty-web, copies + verifies before removing the rootfs copy, updates `/etc/fstab` for auto-mount. Run once; the labelled disk auto-mounts after reflashes (the venv stays on the rootfs and upgrades with the reflash). |
| `bty-web-tftp <start\|stop\|restart>` | Control the local `dnsmasq.service` (which owns the TFTP root). Driven by the browser UI's TFTP daemon Start/Stop/Restart buttons on the Netboot page (`/ui/netboot`). |

## Python API

bty's modules are usable as a library. Stable entry points:

| Module | Purpose |
|------------------|-----------------------------------------------------------|
| `bty.disks` | `list_disks() -> list[dict]` - block-device discovery. |
| `bty.images` | `list_images(root)`, `inspect_image(path)`, `Image` dataclass, `detect_format(path)`, `default_image_root()`. |
| `bty.oras` | `parse_ref(ref) -> OrasRef`, `resolve_ref(ref) -> ResolvedBlob`, `is_oras_url(url) -> bool`, `OrasError`. ORAS / OCI registry adapter for `oras://` URLs. |
| `bty.catalog` | `Catalog`, `load_source(src)`, `load_bytes(...)`, `fetch_bytes(...)`. Portable catalog TOML loader. |
| `bty.flash` | `execute_plan(plan, progress=, cancel=)`, `FlashPlan`, `FlashProgress`, `FlashError`. The flash machinery the wizard sits on top of. |

A full sphinx-autodoc surface is on the roadmap. Until then treat any module
not listed above as internal.

## HTTP API

`bty-web` exposes a FastAPI server, backed by a single SQLite file at
`$BTY_STATE_DIR/state.db` (default `/var/lib/bty/state.db`).

### Auth

Single-tenant PAM authentication. bty-web runs as a Linux service user
(typically ``bty``); the only credential is **that user's OS password**.
``passwd bty`` rotates it. ``POST /ui/login`` (form-encoded
``password=...``) PAM-checks the password and flips
``request.session["bty_authed"] = True``; the session is a server-signed
cookie managed by Starlette's :class:`SessionMiddleware` (cookie name
``bty-token``, sliding 7-day TTL). No DB-backed session table: the cookie
value is the session, signed against the per-appliance key at
``/var/lib/bty/session-secret`` (generated by ``bty-web-init`` on first
boot). ``POST /ui/logout`` clears the session.

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
- `GET /boot/{name}` - serve a live-env artifact from `BTY_BOOT_DIR`
 (default `/var/lib/bty/boot/`). Same trust model as `/pxe/*`. Operators
 populate the dir via the browser UI's "Fetch netboot artifacts" button on
 the Netboot page, or with the auth-gated `PUT /boot/{name}` upload route.
- `GET /images/{key}` and `GET /images/{key}/{name}` - serve image bytes
 from `BTY_IMAGE_ROOT` (or the catalog cache). ``key`` may be a filename,
 a ``bty_image_ref``, or a ``disk_image_sha``; the trailing ``{name}``
 form is decorative (preserves format-by-extension client-side). Used by
 the live env to download the assigned image; reachable by anyone on the
 network. Companion auth-gated upload route at `PUT /images/{name}`.
- `GET /images` - list the catalog (array of `ImageEntry`). Open for the
 same reason as `GET /images/{key}`: the PXE-booted ``bty`` flow needs to
 enumerate from inside the live env without bootstrapping a session, and
 discovery adds no capability beyond the already-open byte-serving route.
- `GET /catalog.toml` - same row set as `GET /images`, serialised as a
 `bty.catalog.Catalog` TOML manifest (``version = 1``, ``[[images]]``
 tables). Open for the same reason; consumed by `bty --catalog` so the
 same client code path that handles static files (e.g. on GitHub releases)
 works against a live bty-web. Entries without a sha256 are skipped.

Protected routes (session cookie required):

| Method | Path | Body | Returns |
|---|---|---|---|
| GET | `/machines` | - | array of `Machine` |
| GET | `/machines/{mac}` | - | `Machine` (404 if missing) |
| GET | `/machines/{mac}/lshw.json` | - | raw `lshw -json` blob (404 if none posted) |
| GET | `/machines/{mac}/disks.json` | - | lsblk-derived disk inventory JSON (404 if none posted) |
| PUT | `/machines/{mac}` | `MachineUpsert` | `Machine` (the new state) |
| DELETE | `/machines/{mac}` | - | 204 (404 if missing) |
| POST | `/catalog/entries` | `CatalogEntryAdd` | new entry (201) |
| GET | `/catalog/entries` | - | array of catalog rows |
| DELETE | `/catalog/entries?src=URL` | - | 204 (404 if missing) |
| POST | `/catalog/import?source=...` | - | `{imported, skipped, errors}` |
| POST | `/catalog/downloads` | `CatalogEnqueueRequest` | download state (202) |
| GET | `/catalog/downloads` | - | list of download states |
| DELETE | `/catalog/downloads/{name}` | - | 200 with state; 404 if not active |
| DELETE | `/catalog/cache/{name}` | - | `{deleted, sha256?}`; idempotent |
| POST | `/catalog/hashes` | `CatalogEnqueueRequest` | hash state (202) |
| GET | `/catalog/hashes` | - | list of hash states |
| DELETE | `/catalog/hashes/{name}` | - | 200 with state; 404 if not active |

``POST /catalog/import`` parses the TOML at ``source`` (path,
``http(s)://``, or ``oras://``) via ``bty.catalog.load_source`` and adds
each entry to the catalog as metadata. **No bytes are fetched at import
time**; each row surfaces in ``/images`` as ``cached: false`` until the
operator triggers a fetch (the ``/ui/images`` "Fetch" button or ``POST
/catalog/downloads``). Idempotent: re-importing the same source skips
duplicates by ``src``.

``DELETE /catalog/cache/{name}`` unlinks ``$cache_dir/<sha256>`` for the
named entry; the catalog row is preserved. The next ``GET /images`` listing
shows the row as ``cached: false`` so the operator can re-enqueue a fetch.
Idempotent: missing cache file or unknown name both return 200 with
``deleted: false`` and a ``reason`` string.

MAC addresses are accepted in any case + `:`-or-`-` separated, and
normalised to lower-case `aa:bb:cc:dd:ee:ff`.

### Wire types

```
Machine = {
  "mac": "aa:bb:cc:dd:ee:ff",
  "bty_image_ref": "<64-hex>" | null,        # null = discovered but unassigned
                                             # references catalog_entries.bty_image_ref
                                             # (sha256 of canonicalised src URL)
  "hostname": "..." | null,
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
  "hostname": str | null,
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

CatalogEntry (as returned by `GET /catalog/entries`) = {
  "bty_image_ref":  "<64-hex>",                # PK; sha256(canonicalise_src(src))
  "src":            "file://..." | "https://..." | "oras://...",
  "disk_image_sha": "<64-hex>" | null,         # observed content sha;
                                               # populated by HashManager (file://)
                                               # or fetch-to-cache (remote)
  "name":           "<filename>",
  "format":         "img.gz" | "img.zst" | ...,
  "size_bytes":     int | null,
  "sha_url":        "https://.../<name>.sha256" | null,
  "description":    str | null,
  "added_at":       "<ISO 8601>"
}

ImageEntry = {
  "name":       "debian.qcow2",
  "format":     "qcow2",
  "size_bytes": 268435456,
  "url":        "http://server:8080/images/<disk_image_sha>/<name>"
                                              | "https://..." | "oras://...",
  "ref":        "<64-hex>",                    # bty_image_ref (=
                                              # sha256(canonicalise_src(src)));
                                              # the value to PUT as
                                              # MachineUpsert.bty_image_ref
                                              # without recomputing the
                                              # canonicalisation client-
                                              # side
  "sha_short":  "<12-hex>" | null,             # display-only prefix
                                              # of disk_image_sha
  "cached":     true | false                   # true iff bty-web has
                                              # the bytes on disk
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

| Variable | Purpose | Default |
|---|---|---|
| `BTY_STATE_DIR` | Where `state.db` lives | `/var/lib/bty` |
| `BTY_IMAGE_ROOT` | Image catalog directory | `/var/lib/bty/images` |
| `BTY_BOOT_DIR` | Live-env artifacts (`/boot/{name}` source) | `${BTY_STATE_DIR}/boot` |
| `BTY_BOOT_RELEASE_REPO` | GitHub repo (`<owner>/<name>`) the "Fetch netboot artifacts" UI pulls live-env artifacts from | `safl/bty` |
| `BTY_WEB_HOST` | uvicorn bind address | `0.0.0.0` |
| `BTY_WEB_PORT` | uvicorn port | `8080` |

### Browser UI (`/ui`)

`bty-web` ships a server-rendered browser UI under `/ui` (Jinja templates,
Bootstrap CSS, HTMX form posts).

- `GET /ui` -> 303 redirect to `/ui/dashboard`
- `GET /ui/login` -> login form
- `POST /ui/login` -> validates the password against PAM and flips
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
- `GET /ui/images` -> image catalog page (the unified dir-scan +
 catalog-entry listing, with Fetch-latest-catalog / Upload-catalog
 controls in its header). The "Add image" card below the list
 carries the per-image "Add by URL" + local-upload widgets.
- `POST /ui/catalog/entries` (form) and
 `POST /catalog/entries` (JSON) -> add an operator-curated
 catalog entry. ``image_url`` accepts http(s):// URLs and
 ``oras://`` references; for ``oras://`` the server resolves the
 OCI manifest at add time, uses the layer's content-addressed
 digest as the entry's sha256 (= machine-bindable), and skips
 the optional sha_url branch (manifest is authoritative).
- `GET /ui/netboot` (Netboot) -> the netboot artifacts inventory
 (present/missing per artifact, sizes, last-fetched timestamps, with a
 Fetch button that hands off to the Workers page) plus the
 **TFTP daemon** panel: live `systemctl is-active dnsmasq.service`
 badge + Start / Stop / Restart buttons driven by the
 sudoers-permitted `bty-web-tftp` helper. An in-page sub-nav jumps
 between List / TFTP Daemon / Activity.
- `GET /ui/workers` (Workers) -> the merged background-workers page:
 Downloads (catalog fetches + per-file release artifacts), Hashing,
 Backup. Active jobs only -- terminal rows evict on completion; the
 events log carries the history. The Backup card has a Back-up-now
 trigger; the other triggers stay on their home pages
 (`/ui/images` for catalog fetches, `/ui/netboot` for release
 artifacts). Reached from the three navbar worker icons; all three
 point at this merged page with anchor sections.
- The router-config **DHCP / Network boot** cheatsheet (appliance-IP /
 interfaces table + option 60 / 66 / 67 values to paste into the LAN's
 DHCP server, for both PXE-via-TFTP and UEFI HTTP Boot) lives on the
 Settings page (`/ui/settings#dhcp-pxe`). bty does NOT run any DHCP
 role; the operator's existing DHCP server points clients at this
 appliance for TFTP + HTTP-Boot fetches.
- `POST /ui/netboot/fetch-release` -> downloads
 `vmlinuz`/`initrd`/`squashfs`/`sha256` from
 `https://github.com/<BTY_BOOT_RELEASE_REPO>/releases/<tag>/download/`
 (default `safl/bty`, default tag `latest`); verifies the manifest
 and atomically installs into `BTY_BOOT_DIR`.
- `GET /ui/settings` -> the config map: read-only groups for every bty
 magic value (where each comes from: env var / derived path / default),
 the editable **Upstream sources** (release repo / catalog URL / release
 tag) card, and the **DHCP / Network boot** router cheatsheet. Operator
 authentication is on the separate Account page (`/ui/account`, reached
 via the user pill): the credential is the OS password of the bty service
 user, rotated with `sudo passwd bty`; to invalidate every session at
 once, rotate the cookie-signing secret with `rm
 /var/lib/bty/session-secret && systemctl restart bty-web`.
- `POST /ui/settings/upstream` / `POST /ui/settings/flash` -> persist
 the editable Upstream-sources / settle-policy overrides.
- `POST /ui/settings/tftp-control` -> drives `bty-web-tftp <action>`
 (allowlist `start` / `stop` / `restart`), the sole sudoers
 grant in `/etc/sudoers.d/bty-web`. URL is unchanged for
 backwards compat though the panel lives on /ui/netboot now.

The auth dependency checks ``request.session.get("bty_authed")``; the
session is a Starlette ``SessionMiddleware``-signed payload carried in the
``bty-token`` cookie, so no per-request DB hop is needed. Logging out clears
the session dict; ``SessionMiddleware`` emits a deletion cookie.

#### Static assets (offline-friendly)

Bootstrap CSS, HTMX, and the HTMX SSE extension are **vendored** into the
wheel under `bty.web._static/` and served at `/static/`. The appliance
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
a real broker (Redis pub/sub, NATS, ...), overkill for an appliance serving
a homelab fleet.

## Configuration schemas

Schemas for the on-disk configuration files used by `bty` and
`bty-web`. Populated alongside the relevant features.

## State export / import format

Format of the archive produced by `bty-web`'s state export, and
expected by import. Populated alongside the export/import feature.
