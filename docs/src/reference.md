# Reference

Reference material for bty's surfaces. Filled in as features land.

## Pre-built release artifacts

Each tagged release publishes a fixed set of assets to GitHub. The
`releases/latest/download/<filename>` URLs always 302 to the newest
tag's copy of that file; substitute `latest` for a specific tag (e.g.
`v0.2.7`) to pin.

| Asset | What it is | URL (latest) |
|---|---|---|
| `bty-usb-x86_64.iso.gz` (+ `.sha256`) | Bootable USB live ISO with built-in writable `BTY_IMAGES` exFAT partition for the operator's image catalog. Open in Balena Etcher / Raspberry Pi Imager / Rufus DD-mode (decompresses `.gz` natively). CLI: `gunzip -d --stdout bty-usb-x86_64.iso.gz \| sudo dd of=/dev/sdX bs=4M`. | <https://github.com/safl/bty/releases/latest/download/bty-usb-x86_64.iso.gz> |
| `bty-server-x86_64.img.zst` (+ `.sha256`) | Server appliance image, x86_64 (browser UI + iPXE + dnsmasq). Boot in QEMU or `dd` to a disk. | <https://github.com/safl/bty/releases/latest/download/bty-server-x86_64.img.zst> |
| `bty-server-rpi-arm64.img.zst` (+ `.sha256`) | Server appliance image for Raspberry Pi 4 / 5 (arm64). Write with `dd` to an SD card. | <https://github.com/safl/bty/releases/latest/download/bty-server-rpi-arm64.img.zst> |
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
| 1 | Operation failed (validation rejected the plan; write subprocess returned non-zero; cloud-init / cijoe step failed). |
| 2 | Misuse - argparse error, missing required flag, missing input file. |
| 3 | Privilege required - operation needs root, rerun via `sudo`. |
| 4 | Required external tool is not installed (e.g. `cijoe`). |
| 5 | Target raced - block device became mounted or otherwise unsuitable between validation and write. |

### `bty list disks`

List interesting block devices on the local system. Shells out to
`lsblk -J` and projects useful columns: `path`, `size`, `tran` (bus
transport), `vendor`, `model`, `serial`, `removable`.

```text
PATH          SIZE  TRAN  VENDOR  MODEL              SERIAL          REMOVABLE
------------  ----  ----  ------  -----------------  --------------  ---------
/dev/nvme0n1  1T    nvme          Samsung 980 PRO    NVME0X000001    False
/dev/sda      500G  sata  ATA     Samsung SSD 870    S5SUNG0123456   False
```

### `bty list images [--image-root PATH]`

List supported images directly under the image root (non-recursive).
Recognised formats: `.qcow2`, `.img`, `.img.zst`, `.img.xz`,
`.img.gz`, `.img.bz2`.

bty itself ships its target images (`bty-server-x86_64.img.zst`,
`bty-server-rpi-arm64.img.zst`) as `.img.zst` because flash-time
decompression is on the hot path of the per-job CI reflash use
case (zstd decompresses at ~800-1500 MB/s and saturates the
target disk; xz at ~50-100 MB/s would bottleneck flash by ~7x in
absolute terms, ~80s extra per CI job). The bty USB stick image
(`bty-usb-x86_64.iso.gz`) ships as gzip because Etcher's bundled
xz decompressor fails on our output regardless of how it's
shaped, while every flasher we tested handles gzip natively for
host-side stick-prep — that's a one-off host operation, not a
hot-path concern. The flash code accepts all
of `.img.zst` / `.img.xz` / `.img.gz` / `.img.bz2` for
operator-supplied images so neither format choice is forced on
you. Decompression speed ranking (rough): zstd > gzip > xz > bzip2.

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

### `bty inspect image PATH`

Print detailed metadata for a single image file. Always reports
`path`, `format`, and `size_bytes`. Adds a format-specific `detail`
block when the relevant tool succeeds:

- `.qcow2` -> `qemu-img info --output=json`
- `.img.zst` -> `zstd -l`
- `.img` -> nothing extra (raw images have no header to query)

Exit codes:

- `0` -> success
- `2` -> the path does not exist (or argparse rejected the invocation)

### `bty flash --image PATH --target PATH [--provision MODE] [--user-data PATH] [--meta-data PATH] [--cijoe-workflow PATH] [--cijoe-config PATH] [--progress {text,ndjson,none}] [--dry-run] [--yes]`

Flash an image onto a target block device.

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
- Provisioning mode is one of `none`, `cloud-init`, `cijoe`.

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

#### Provisioning

After the flash, `bty` runs the configured post-flash step:

- **`none`** - no post-flash work; the cooked image is the result.
- **`cloud-init`** - mounts the partition on the target whose rootfs
 carries `/etc/cloud/` (the unambiguous "cloud-init lives here"
 marker), writes operator-supplied `user-data` (and either supplied
 or auto-synthesised `meta-data`) under
 `/var/lib/cloud/seed/nocloud-net/` so cloud-init's NoCloud
 datasource picks them up on first boot. **Requires `--user-data
 PATH`**; rejects with exit `2` if the flag is missing. Errors
 loudly if no partition on the target appears to have cloud-init
 installed, rather than silently writing a seed nothing will read.
- **`cijoe`** - mounts the largest partition on the target (heuristic
 for the rootfs), exports `BTY_ROOTFS` pointing at the mount, then
 invokes `cijoe <workflow> --monitor [-c <config>]`. The workflow's
 tasks read or mutate the rootfs through `$BTY_ROOTFS`; bty itself
 does not interpret what they do. **Requires `--cijoe-workflow PATH`**;
 rejects with exit `2` if missing. **Requires `cijoe` on `PATH`**
 (`pipx install cijoe`); errors clearly if absent. Workflow exit
 non-zero is propagated as a flash failure.

#### Progress

`--progress {text,ndjson,none}` controls lifecycle reporting (default
`text`).

Lifecycle events: `started`, `writing`, `synced`, `partprobed`,
`provisioning` (cloud-init / cijoe steps only), `done`, `failed`.

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
- `1` -> validation failed, or a write / provisioning subprocess returned non-zero.
- `2` -> argparse error, missing image, missing `--user-data` / `--cijoe-workflow`, neither `--dry-run` nor `--yes` given.
- `3` -> `--yes` was passed without root.
- `4` -> required external tool missing (e.g. `cijoe` for `--provision cijoe`).
- `5` -> target raced (became mounted or stopped being a block device between validation and write).

The general exit-code table at the top of this section applies to all
subcommands.

### `bty-tui [--server URL] [--mac MAC]`

Two-pane terminal UI for picking an image + a target disk and
flashing. Same flash machinery as the CLI; the TUI is a thin wrapper
around `bty.flash.execute_plan`.

Two image-source modes:

- **Local** (default). Scans an image-root directory (USB live env's
  `BTY_IMAGES` partition, or whatever path
  [`BTY_IMAGE_ROOT`](#environment-variables) points at).
- **Remote** (`--server URL`). Fetches the catalog from a running
  `bty-web` via `GET /images`. Selecting an image streams it from
  the server's `GET /images/{name}` straight to the target disk -
  no local download. The TUI's pane title shows the server URL so
  the operator can see at a glance where the catalog comes from.

`--mac MAC` is used together with `--server`: after a successful
flash the TUI `POST`s `<server>/pxe/<mac>/done` so the server's
`last_flashed_at` updates. Best-effort - a failed signal surfaces
in the status bar but doesn't undo the flash.

The TUI-on-PXE flow uses both flags: the live env reads `bty.server`
and `bty.mac` from `/proc/cmdline` and assembles the matching CLI
invocation in `/usr/local/sbin/bty-tui-on-tty1`.

## Configuration

bty resolves a small set of paths and runtime knobs from the
environment and sensible defaults.

### Environment variables

| Variable | Purpose | Default |
|-------------------|----------------------------------------------------------------|---------------------|
| `BTY_IMAGE_ROOT` | Image root for `bty list images` and `bty inspect image`. | `/var/lib/bty/images` |

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
| `bty.images` | `list_images(root)`, `inspect_image(path)`, `Image` dataclass, `detect_format(path)`, `default_image_root()`. |
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
 with kernel cmdline params `bty.server`, `bty.mac`,
 `bty.image_url`, `bty.provisioning` so the live env can flash
 the assigned image.

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
 reflashing) survives across boots. If the machine has
 `provisioning_mode='cijoe-online'` and a `cijoe_workflow_ref`,
 this also kicks off a background workflow run from bty-web
 against the freshly-booted target (milestone 15). Status surfaces
 via `last_workflow_status` and the SSE machines-update channel.
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
  "image": "debian.qcow2" | null,           # null = discovered but unassigned
  "provisioning_mode": "none" | "cloud-init" | "cijoe" | "cijoe-online",
  "hostname": "..." | null,
  "cijoe_workflow_ref": "..." | null,
  "last_known_good": object | null,
  "discovered_at": "<ISO 8601>" | null,     # first /pxe contact; null if PUT-only
  "last_seen_at":  "<ISO 8601>" | null,     # most recent /pxe contact
  "last_seen_ip":  "203.0.113.42" | null,
  "boot_policy":   "local" | "flash" | "tui",  # what /pxe/{mac} returns
  "last_flashed_at": "<ISO 8601>" | null,   # set by POST /pxe/{mac}/done
  "last_workflow_run_at":    "<ISO 8601>" | null,
  "last_workflow_status":    "running" | "success" | "failed" | null,
  "last_workflow_output_path": str | null,  # /var/lib/bty/workflows/<mac>/<run-id>
  "created_at":    "<ISO 8601>",
  "updated_at":    "<ISO 8601>"
}

MachineUpsert = {
  "image": str | null,
  "provisioning_mode": "none" | "cloud-init" | "cijoe" | "cijoe-online",
  "hostname": str | null,
  "cijoe_workflow_ref": str | null,
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
- `GET /ui/images` -> read-only image catalog
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
 restarts dnsmasq.
- `POST /ui/settings/pxe-activate` -> drives `bty-web-activate-pxe`,
 a sudoers-permitted helper in `/usr/local/sbin/` that writes
 `/etc/dnsmasq.d/bty-pxe-active.conf` and restarts dnsmasq. The
 NOPASSWD entry in `/etc/sudoers.d/bty-web` is the only sudo
 grant the appliance gives bty-web.

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
