# Reference

Reference material for bty's surfaces. Filled in as features land.

## Pre-built release artifacts

Each tagged release publishes a fixed set of assets to GitHub. The
`releases/latest/download/<filename>` URLs always 302 to the newest
tag's copy of that file; substitute `latest` for a specific tag (e.g.
`v0.8.3`) to pin.

| Asset | What it is | URL (latest) |
|---|---|---|
| `bty-usb-x86_64.iso.gz` (+ `.sha256`) | Bootable USB live ISO with built-in writable `BTY_IMAGES` exFAT partition for the operator's image catalog. Open in Balena Etcher / Raspberry Pi Imager / Rufus DD-mode (decompresses `.gz` natively). CLI: `gunzip -d --stdout bty-usb-x86_64.iso.gz \| sudo dd of=/dev/sdX bs=4M`. | <https://github.com/safl/bty/releases/latest/download/bty-usb-x86_64.iso.gz> |
| `bty-server-x86_64.img.gz` (+ `.sha256`) | Server appliance image, x86_64 (browser UI + iPXE + dnsmasq). Boot in QEMU or `dd` to a disk. | <https://github.com/safl/bty/releases/latest/download/bty-server-x86_64.img.gz> |
| `bty-server-rpi-arm64.img.gz` (+ `.sha256`) | Server appliance image for Raspberry Pi 4 / 5 (arm64). Write with `dd` to an SD card. | <https://github.com/safl/bty/releases/latest/download/bty-server-rpi-arm64.img.gz> |
| `bty-netboot-x86_64.{vmlinuz,initrd,squashfs}` (+ `bty-netboot-x86_64.sha256`) | Netboot trio for PXE-flash clients. Drop into the server's `BTY_BOOT_DIR` (or click "fetch latest release" on `/ui/boot`). | <https://github.com/safl/bty/releases/latest/download/bty-netboot-x86_64.vmlinuz> |
| `bty.pdf` | Offline copy of the docs (this site, rendered by Sphinx + LaTeX). | <https://github.com/safl/bty/releases/latest/download/bty.pdf> |
| `bty_lab-X.Y.Z-py3-none-any.whl` / `.tar.gz` | Python wheel + sdist. Mirrored on PyPI as [`bty-lab`](https://pypi.org/project/bty-lab/) - prefer `pipx install bty-lab` over downloading by hand. | <https://github.com/safl/bty/releases> |

The browser path is <https://github.com/safl/bty/releases>; the JSON
API for build automation is `GET /repos/safl/bty/releases/latest`.

## CLI

The `bty` command groups operations as subcommands. Each leaf command
accepts `--json` to emit machine-readable output instead of the default
human-readable table.

`bty --version` prints the installed version (sourced from package
metadata) and exits.

### JSON output envelope

Every `--json` output is wrapped:

```json
{
  "schema_version": "1",
  "command": "<subcommand-name>",
  ...command-specific fields...
}
```

Agents key off `schema_version`; incompatible structural changes bump
the version. See [`AGENTS.md`](https://github.com/safl/bty/blob/main/AGENTS.md)
for the full per-command schema reference and the exit-code table.

### Exit codes

| Code | Meaning |
|------|--------------------------------------------------------------------|
| 0 | Success. |
| 1 | Operation failed (validation rejected the plan; write subprocess returned non-zero). |
| 2 | Misuse - argparse error, missing required flag, missing input file. |
| 3 | Privilege required - operation needs root, rerun via `sudo`. |
| 4 | Required external tool is not installed (e.g. `qemu-img` for `.qcow2`). |
| 5 | Target raced - block device became mounted or otherwise unsuitable between validation and write. |

### Block-device discovery: use `lsblk`

bty doesn't ship its own block-device-listing command (a `bty list
disks` wrapper existed pre-v0.8.4 but added little over what
`lsblk` already does). Use `lsblk -d -e7` to see the candidate
disks at a glance:

```text
$ lsblk -d -e7
NAME    MAJ:MIN  RM   SIZE  RO  TYPE  MOUNTPOINTS
nvme0n1  259:0    0    1T   0   disk
sda        8:0    0  500G   0   disk
```

`-d` strips partitions, `-e7` excludes loop devices.

### `bty images [--image-root PATH | --catalog SOURCE]`

List supported images directly under the image root (non-recursive).
Recognised formats: `.qcow2`, `.img`, `.img.zst`, `.img.xz`,
`.img.gz`, `.img.bz2`.

bty itself ships all of its dd-able images
(`bty-server-x86_64.img.gz`, `bty-server-rpi-arm64.img.gz`,
`bty-usb-x86_64.iso.gz`) as gzip for universal flasher support
- Etcher / Rufus / Imager / dd / Windows / macOS all decompress
gzip natively without the version-cliff or implementation-bug
issues that bit us with xz (Etcher's bundled xz handler) and
zstd (older Etcher pre-1.18). Stick prep / appliance setup is
a one-shot host operation, not a hot-path concern; gzip's
universal compatibility wins over the marginal speed advantage
of zstd on a one-shot decompress.

The flash code still accepts `.img.zst` / `.img.xz` / `.img.gz`
/ `.img.bz2` for operator-supplied target images, so an operator
running per-job CI reflash on a fast disk can pick `.img.zst`
for the speed advantage without bty-shipped artifacts forcing
that choice. Decompression speed ranking (rough): zstd > gzip >
xz > bzip2.

**Tarballs are NOT supported.** `.tar.gz` / `.tar.xz` / `.tgz` /
`.tar.bz2` etc. wrap one or more files in TAR headers; running
the gzip/xz/bzip2 layer on them yields a TAR stream, not an
image. dd'ing that into a target disk would write tar headers
into the MBR. Extract first (`tar -xzf foo.tar.gz`) and drop the
resulting `.img` onto BTY_IMAGES.

The image root is resolved in this order:

1. The `--image-root` argument, if given.
2. The `BTY_IMAGE_ROOT` environment variable.
3. `/var/lib/bty/images` (the path the bty USB live appliance auto-mounts
 the `BTY_IMAGES` partition at).

The listing also includes any `.bri` (bty Remote Image) descriptors
present in the image root. Each remote row carries a
`source = "remote"` field plus `url` (the upstream location -- an
HTTP/HTTPS URL or an `oras://` OCI registry reference; see the
schemes block below); local rows carry `source = "local"` and `path`.

A `.bri` is a tiny TOML file:

```toml
url = "https://github.com/safl/bty/releases/latest/download/bty-server-x86_64.img.gz"
# Optional: name, format, size_bytes, sha256, description
```

Only `url` is required; everything else is inferred from the URL
or left null. Operators can drop `.bri` files alongside `.img.gz`
/ `.qcow2` files in BTY_IMAGES (or, on Ventoy / IP-KVM
deliveries, at the surrounding stick's partition root or in a
`bty-images/` subfolder there).

#### `url` field: schemes

The `url` field accepts three schemes:

- `https://` (or `http://`) — plain HTTP fetch. Format is inferred
  from the URL's filename extension.
- `oras://<host>/<owner>/<repo>:<tag>` — an OCI artefact published
  via [ORAS](https://oras.land/) (OCI Registry As Storage -- the
  spec for **non-container** artefacts in a container registry).
  bty resolves the tag to a content-addressed layer digest at
  flash time (through the registry's anonymous-pull flow; no
  credentials required for public packages) and verifies the
  downloaded bytes against that digest. Use a rolling tag
  (`:latest`) to follow upstream re-publishes; pin a specific
  build by replacing the tag with `@sha256:<hex>`.

  Distinct from a `docker pull ghcr.io/...` reference: nosi-style
  disk images are stored as OCI blobs but are **not** runnable
  container images. The `oras://` spelling makes that explicit so
  an operator reading a .bri file doesn't reach for `docker`
  / `podman` by mistake.

  Any OCI v2 registry that follows the GHCR anonymous-pull
  convention works; the URL's host (`ghcr.io`, `quay.io`,
  `registry.example.com:5000`, etc.) drives the per-host token
  endpoint. ``bty-web``'s "Add image by URL" form / `POST
  /catalog/entries` endpoint accepts the same `oras://` shape;
  the server resolves the manifest at add time and stores the
  layer digest as the entry's sha256 (no separate `sha_url`
  needed for oras refs).

```toml
# rolling tag, bty pins to current digest at flash time
url = "oras://ghcr.io/safl/nosi/debian-sysdev:latest"

# digest-pinned, skips the manifest fetch
url = "oras://ghcr.io/safl/nosi/debian-sysdev@sha256:94e6..."
```

Fresh USB sticks ship with four starter .bri files pre-staged
on the BTY_IMAGES partition: three nosi sysdev images
(`debian-sysdev`, `ubuntu-sysdev`, `fedora-sysdev`, each via
`oras://ghcr.io/safl/nosi/<v>:latest`) plus the latest bty-server
appliance from the GitHub release URL. Operators see all four
in the TUI catalog without setting up any infrastructure, and
edit / delete / replace them freely from a host OS since the
files are plain TOML on an exFAT partition.

To install the bty-server appliance specifically, no `.bri`
shipping is needed: `bty tui` has an `i` keybinding that flashes
the latest `bty-server-x86_64.img.gz` from
`https://github.com/safl/bty/releases/latest/...` directly. The
`.bri` mechanism is for operator-supplied URL pointers (private
mirrors, custom-built images, etc.), not for the bty-server
bootstrap.

### `bty inspect PATH`

Print detailed metadata for a single image file or `.bri` descriptor.
Always reports `path`, `format`, and `size_bytes`. Adds a format-
specific `detail` block when the relevant tool succeeds:

- `.qcow2` -> `qemu-img info --output=json`
- `.img.zst` -> `zstd -l`
- `.img.xz` -> `xz -l`
- `.img.gz` -> `gzip -l`
- `.img.bz2` -> (no listing tool; detail omitted)
- `.img` -> nothing extra (raw images have no header to query)
- `.bri` -> parsed descriptor contents (`url`, `name`, `format`, etc.)

Exit codes:

- `0` -> success
- `2` -> the path does not exist (or argparse rejected the invocation)

### `bty flash IMAGE TARGET [--progress {text,ndjson,none}] [--dry-run] [--yes]`

Flash an image onto a target block device. ``bty flash`` is a
flasher only -- first-boot bring-up belongs in the image builder
upstream (cloud-init / NoCloud user-data baked at image-build
time). There are no provisioning flags here.

`IMAGE` (positional) accepts four forms:

- A local file path (`/path/to/foo.img.gz`).
- An HTTP/HTTPS URL (`https://server/foo.img.gz`); raw `.img` and
  compressed `.img.*` URLs stream straight to disk.
- An `oras://` reference to an OCI artefact (`oras://ghcr.io/owner/repo:tag`
  or `...@sha256:<hex>`); bty resolves the manifest, picks the
  disk-image layer, and streams the blob through the same pipeline
  as a plain HTTPS fetch (see the `.bri` schemes block above).
- A `.bri` descriptor path; bty resolves the descriptor's `url`
  field (any of the above) and falls into the URL flash path
  automatically.

Either `--dry-run` or `--yes` is required:

| Flags | Behaviour |
|---|---|
| `--dry-run` | Validate the plan; no writes. Exit `0` if valid, `1` if not. |
| `--yes` | Validate, then write. Requires root. |
| (neither) | Refuse with exit `2` and a hint pointing at both flags. |
| `--dry-run --yes` | `--dry-run` wins. |

#### Validation

Both modes start by validating the plan:

- Image exists and is a recognised format (`.qcow2` / `.img` / `.img.zst` / `.img.xz`).
- Image virtual size (decompressed / qcow2-virtual size, not on-disk
 size) fits the target. Skipped with a note if the virtual size
 cannot be determined (e.g. `qemu-img info` failure).
- Target exists and is a block device.
- Target has no mounted partitions (refuses to overwrite live storage).

#### Write (`--yes` only)

If validation passes and `bty` is running as root, the write proceeds
in a format-specific way:

- `.img` -> `dd if=IMG of=TARGET bs=4M conv=fsync status=progress`
- `.img.zst` -> `zstd -d --stdout IMG | dd of=TARGET bs=4M conv=fsync status=progress`
- `.qcow2` -> `qemu-img convert -p -O raw IMG TARGET`

Immediately before the write, the target is re-probed and re-validated
to catch races (e.g. the target getting mounted between dry-run and
flash). On success, `bty` runs `sync` and `partprobe TARGET` so the
kernel re-reads the new partition table.

#### Progress

`--progress {text,ndjson,none}` controls lifecycle reporting (default
`text`).

Lifecycle events: `started`, `writing`, `synced`, `partprobed`,
`done`, `failed`.

- `text` (default) - one line per event on stderr (`[event] note`).
- `ndjson` - one JSON object per line on stdout
 (`{"event":"started","total_bytes":12345}` etc.). Use this from
 agents and CI scripts.
- `none` - no lifecycle output. Subprocess noise (`dd status=progress`)
 still goes to stderr in all modes; redirect if you want a clean
 channel.

The same callback shape (`bty.flash.ProgressCallback` /
`bty.flash.FlashProgress`) is used by `bty-tui`'s flash modal - UI
updates and CLI output share the same event stream.

#### Exit codes (specific to `bty flash`)

- `0` -> success (validation passed for `--dry-run`; write completed for `--yes`).
- `1` -> validation failed, or the write subprocess returned non-zero.
- `2` -> argparse error, missing image, neither `--dry-run` nor `--yes` given.
- `3` -> `--yes` was passed without root.
- `4` -> required external tool missing (e.g. `qemu-img` for `.qcow2`).
- `5` -> target raced (became mounted or stopped being a block device between validation and write).

The general exit-code table at the top of this section applies to all
subcommands.

### `bty tui [--catalog SOURCE] [--image-root PATH] [--mac MAC]`

Terminal UI for picking an image + a target disk and flashing. Same
flash machinery as the CLI; the TUI is a thin wrapper around
`bty.flash.execute_plan`.

Catalog sources (combine freely):

- **Local image-root** (always scanned). Files + `.bri` descriptors
  under the configured root (USB live env's `BTY_IMAGES` partition,
  `BTY_IMAGE_ROOT` env, or `--image-root /path`).
- **Catalog overlay** (`--catalog SOURCE`). One additional source:
  a local TOML file (`/path/to/catalog.toml`), an HTTP URL
  (`https://example.com/catalog.toml`), an `oras://` reference
  (`oras://ghcr.io/owner/bty-catalog:latest`), or a `bty-web`
  instance's TOML endpoint (`http://server:8080/catalog.toml`).
  Fetched once at startup and held in memory; pressing `r` re-scans
  only the local image-root, not the remote catalog. The TUI's pane
  title shows the merged source label.

`--mac MAC` is the self-MAC. Combined with an http(s) `--catalog`,
the TUI auto-derives the URL's `scheme://host` as the pxe-done base
and `POST`s `<base>/pxe/<mac>/done` after a successful flash so a
bty-web's `last_flashed_at` updates. Best-effort: a non-bty-web
catalog source (static file, `oras://`) skips the POST.

The TUI-on-PXE flow uses both flags: the live env reads `bty.server`
and `bty.mac` from `/proc/cmdline` and the
`/usr/local/sbin/bty-tui-on-tty1` wrapper rewrites the bty.server
base URL into `--catalog <base>/catalog.toml`.

## Configuration

bty resolves a small set of paths and runtime knobs from the
environment and sensible defaults.

### Environment variables

| Variable | Purpose | Default |
|-------------------|----------------------------------------------------------------|---------------------|
| `BTY_IMAGE_ROOT` | Image root for `bty images` and `bty inspect`. | `/var/lib/bty/images` |

The `bty --image-root` flag (when given) takes precedence over
`BTY_IMAGE_ROOT`.

### Default paths

- `/var/lib/bty/images` - image root. The USB live appliance
 auto-mounts the `BTY_IMAGES` partition here.

## Python API

bty's modules are usable as a library. Stable entry points:

| Module | Purpose |
|------------------|-----------------------------------------------------------|
| `bty.disks` | `list_disks() -> list[dict]` - block-device discovery. |
| `bty.images` | `list_images(root)`, `inspect_image(path)`, `Image` dataclass, `detect_format(path)`, `default_image_root()`, `read_bri(path)`, `list_remote_images(root)`, `RemoteImage` / `BriError`. |
| `bty.oras` | `parse_ref(ref) -> OrasRef`, `resolve_ref(ref) -> ResolvedBlob`, `is_oras_url(url) -> bool`, `OrasError`. ORAS / OCI registry adapter for `oras://` URLs. |
| `bty.formatting` | `print_table(rows, columns)`, `print_inspect(info)`. |

A full sphinx-autodoc surface is on the roadmap. Until then treat
any module not listed above as internal.

## HTTP API

`bty-web` exposes a FastAPI server. Backed by a single SQLite file at
`$BTY_STATE_DIR/state.db` (default `/var/lib/bty/state.db`).

### Auth

Single-tenant PAM authentication. bty-web runs as a Linux service
user (typically ``bty``); the only credential is **that user's OS
password**. ``passwd bty`` rotates it. ``POST /ui/login`` (form-
encoded ``password=...``) PAM-checks the password and flips
``request.session["bty_authed"] = True``; the session is a server-
signed cookie managed by Starlette's
:class:`SessionMiddleware` (cookie name ``bty-token``, sliding 7-day
TTL). No DB-backed session table - the cookie value is the session,
signed against the per-appliance key at
``/var/lib/bty/session-secret`` (generated by ``bty-web-init`` on
first boot). ``POST /ui/logout`` clears the session.

Open routes - these are reachable by PXE clients and other live-env
tooling which can't carry a session cookie:

- `GET /healthz` - `{"status": "ok"}`
- `GET /version` - `{"version": "..."}`
- `GET /pxe/{mac}` - per-MAC iPXE script (`text/plain`). The
 response depends on the machine's `boot_policy`:
 - `local` (default) or no image assigned: sanboot fallback ("boot
 from local disk"). Auto-discovery still applies to unknown MACs.
 - `flash` + image assigned: chain into the live env over HTTP
 with kernel cmdline params `bty.server`, `bty.mac`, and
 `bty.image_url` so the live env can flash the assigned image.

 Auto-discovery: the first contact for an unknown MAC inserts a
 placeholder row (image=null, boot_policy=local) so the operator
 sees it in `GET /machines` and can claim it with `PUT
 /machines/{mac}`. Repeat contacts update `last_seen_at` /
 `last_seen_ip`. Trust model: bty-web is meant for a homelab /
 CI network, not the open internet - anyone reachable can write
 discovery rows.
- `POST /pxe/{mac}/done` - completion signal from the live env
 after a successful flash. Updates `last_flashed_at`. **Does not
 modify `boot_policy`** - flipping a machine back to `local` is an
 explicit operator action so the per-job CI cadence (constant
 reflashing) survives across boots. bty-web does *not* run any
 post-flash provisioning -- the target reboots into whatever the
 pre-built image brings up via cloud-init.
- `GET /pxe-bootstrap.ipxe` - static iPXE script that dnsmasq points
 iPXE clients at on their second-stage DHCP. Returns
 `chain http://<host>/pxe/${net0/mac:hexhyp}` where `<host>` is the
 request's `Host` header, so the client always loops back to
 whichever IP / hostname / .local name it used to reach the server.
- `GET /boot/{name}` - serve a live-env artifact from `BTY_BOOT_DIR`
 (default `/var/lib/bty/boot/`). Same trust model as `/pxe/*`.
 Operators populate the dir via the browser UI's "fetch latest
 release" button on the Boot page, or with the auth-gated
 `PUT /boot/{name}` upload route.
- `GET /images/{name}` - serve image bytes from `BTY_IMAGE_ROOT`.
 Used by the live env to download the assigned image; reachable
 by anyone on the network. Companion auth-gated upload route at
 `PUT /images/{name}` for operators / scripts.
- `GET /images` - list the catalog (array of `ImageEntry`). Open for
 the same reason as `GET /images/{name}`: the bty-tui-on-PXE flow
 needs to enumerate from inside the live env without first
 bootstrapping a session, and discovery adds no capability beyond
 what the already-open byte-serving route provides.
- `GET /catalog.toml` - same row set as `GET /images`, serialised as
 a `bty.catalog.Catalog` TOML manifest (``version = 1``, ``[[images]]``
 tables). Open for the same reason as `GET /images`; consumed by
 `bty tui --catalog` and `bty images --catalog` so the same client
 code path that handles static files (e.g. published on GitHub
 releases) works against a live bty-web. Entries without a sha256
 are skipped.

Protected routes (session cookie required):

| Method | Path | Body | Returns |
|---|---|---|---|
| GET | `/machines` | - | array of `Machine` |
| GET | `/machines/{mac}` | - | `Machine` (404 if missing) |
| PUT | `/machines/{mac}` | `MachineUpsert` | `Machine` (the new state) |
| DELETE | `/machines/{mac}` | - | 204 (404 if missing) |

MAC addresses are accepted in any case + `:`-or-`-` separated, and
normalised to lower-case `aa:bb:cc:dd:ee:ff`.

### Wire types

```
Machine = {
  "mac": "aa:bb:cc:dd:ee:ff",
  "image_sha256": "<64-hex>" | null,         # null = discovered but unassigned
  "hostname": "..." | null,
  "discovered_at": "<ISO 8601>" | null,      # first /pxe contact; null if PUT-only
  "last_seen_at":  "<ISO 8601>" | null,      # most recent /pxe contact
  "last_seen_ip":  "203.0.113.42" | null,
  "boot_policy":   "local" | "flash" | "tui", # what /pxe/{mac} returns
  "last_flashed_at": "<ISO 8601>" | null,    # set by POST /pxe/{mac}/done
  "created_at":    "<ISO 8601>",
  "updated_at":    "<ISO 8601>"
}

MachineUpsert = {
  "image_sha256": "<64-hex>" | null,
  "hostname": str | null,
  "boot_policy": "local" | "flash" | "tui"  # default "local" on PUT;
                                            # auto-discovery sets "tui"
}

ImageEntry = {
  "name": "debian.qcow2",
  "path": "/var/lib/bty/images/debian.qcow2",
  "format": "qcow2",
  "size_bytes": 268435456
}
```

### Configuration

| Variable | Purpose | Default |
|---|---|---|
| `BTY_STATE_DIR` | Where `state.db` lives | `/var/lib/bty` |
| `BTY_IMAGE_ROOT` | Image catalog directory | `/var/lib/bty/images` |
| `BTY_BOOT_DIR` | Live-env artifacts (`/boot/{name}` source) | `${BTY_STATE_DIR}/boot` |
| `BTY_BOOT_RELEASE_REPO` | GitHub repo (`<owner>/<name>`) the "fetch latest release" UI pulls live-env artifacts from | `safl/bty` |
| `BTY_WEB_HOST` | uvicorn bind address | `0.0.0.0` |
| `BTY_WEB_PORT` | uvicorn port | `8080` |

### Browser UI (`/ui`)

`bty-web` ships a server-rendered browser UI under `/ui` (Jinja
templates, Bootstrap CSS, HTMX form posts).

- `GET /ui` -> 303 redirect to `/ui/dashboard`
- `GET /ui/login` -> login form
- `POST /ui/login` -> validates the password against PAM and flips
 ``request.session["bty_authed"] = True``; SessionMiddleware emits
 the signed `bty-token` cookie on the redirect response
 (``SameSite=Strict``).
- `POST /ui/logout` -> ``request.session.clear()``; SessionMiddleware
 emits a deletion cookie.
- `GET /ui/dashboard` -> overview (machine count, discovered count,
 image count)
- `GET /ui/machines` -> table of all machines with a "discovered"
 badge for unassigned rows; auto-refreshes via SSE
- `GET /ui/machines/{mac}` -> detail + edit form
- `POST /ui/machines/{mac}` -> upsert from a form submit
- `POST /ui/machines/{mac}/delete` -> delete record
- `GET /ui/images` -> image catalog page (dir-scan listing +
 "Add image by URL" form + upload widget)
- `POST /ui/catalog/entries` (form) and
 `POST /catalog/entries` (JSON) -> add an operator-curated
 catalog entry. ``image_url`` accepts http(s):// URLs and
 ``oras://`` references; for ``oras://`` the server resolves the
 OCI manifest at add time, uses the layer's content-addressed
 digest as the entry's sha256 (= machine-bindable), and skips
 the optional sha_url branch (manifest is authoritative).
- `GET /ui/boot` -> live-env boot artifacts: present/missing per
 artifact, sizes, last-fetched timestamps, "fetch latest release"
 form
- `POST /ui/boot/fetch-release` -> downloads
 `vmlinuz`/`initrd`/`squashfs`/`sha256` from
 `https://github.com/<BTY_BOOT_RELEASE_REPO>/releases/<tag>/download/`
 (default `safl/bty`, default tag `latest`); verifies the manifest
 and atomically installs into `BTY_BOOT_DIR`
- `GET /ui/settings` -> two-card page:
 - **Authentication** - explanatory text only. The credential is
 the OS password of the bty service user; the operator rotates
 it with `sudo passwd bty`. To force every session to invalidate
 in one shot, rotate the cookie-signing secret with `rm
 /var/lib/bty/session-secret && systemctl restart bty-web`.
 - **PXE proxy-DHCP** - interface dropdown (read from
 `/sys/class/net/`) + subnet input (`192.168.1.0` or
 `192.168.1.0/24`). Activate calls `bty-web-activate-pxe`
 which writes `/etc/dnsmasq.d/bty-pxe-active.conf` and
 restarts dnsmasq. A separate Deactivate button calls
 `bty-web-deactivate-pxe` to remove that file and restart
 dnsmasq back to TFTP-only. When the configured interface
 is no longer present (NIC renamed across reboot, USB
 ethernet unplugged), the panel flags the broken binding so
 the operator can deactivate and re-bind.
- `POST /ui/settings/pxe-activate` -> drives `bty-web-activate-pxe`,
 a sudoers-permitted helper in `/usr/local/sbin/` that writes
 `/etc/dnsmasq.d/bty-pxe-active.conf` and restarts dnsmasq.
- `POST /ui/settings/pxe-deactivate` -> drives `bty-web-deactivate-pxe`,
 the sibling helper that removes the active config and restarts
 dnsmasq. Idempotent: a missing active config is reported as
 already-deactivated. The two NOPASSWD entries in
 `/etc/sudoers.d/bty-web` are the only sudo grants the appliance
 gives bty-web.

The auth dependency checks ``request.session.get("bty_authed")``;
the session is a Starlette ``SessionMiddleware``-signed payload
carried in the ``bty-token`` cookie, so no per-request DB hop is
needed. Logging out clears the session dict; ``SessionMiddleware``
emits a deletion cookie on the response.

#### Static assets (offline-friendly)

Bootstrap CSS, HTMX, and the HTMX SSE extension are **vendored** into
the wheel under `bty.web._static/` and served at `/static/`. The bty
appliance does not contact any CDN at runtime - all browser code is
served from the same origin. See `src/bty/web/_static/README.md` in
the source tree for asset versions and the refresh procedure.

#### Live updates (`GET /events/machines`)

The machines table subscribes to a Server-Sent Events stream so the
operator does not have to refresh after PXE auto-discovery or another
admin's edit. The endpoint:

- Authenticates with the same session-cookie dep as the rest of the
 API. Browsers carry the cookie automatically; the SSE `EventSource`
 API does not let you set custom headers.
- Sends `Content-Type: text/event-stream` and an initial
 `machines-update` event containing the current `<tbody>` snapshot
 on connect.
- Emits a fresh `machines-update` event after every mutation
 (`PUT /machines/{mac}`, `DELETE /machines/{mac}`, the corresponding
 `/ui` form posts, and PXE auto-discovery on `/pxe/{mac}`).

The fan-out bus is in-process - slow consumers are silently dropped
(every event carries the full snapshot, so they catch up on the next
mutation). **Single uvicorn worker** is required: a multi-worker
deployment would need a real broker (Redis pub/sub, NATS, ...), which
is overkill for an appliance serving a homelab fleet.

## Configuration schemas

Schemas for the on-disk configuration files used by `bty` and
`bty-web`. Populated alongside the relevant features.

## State export / import format

Format of the archive produced by `bty-web`'s state export, and
expected by import. Populated alongside the export/import feature.
