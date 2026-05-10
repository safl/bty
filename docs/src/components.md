# Components

bty is one Python package - the `bty` module, distributed on PyPI as
[`bty-lab`](https://pypi.org/project/bty-lab/) - with three console-script
entry points, plus a sibling appliance-image builder (`bty-media/`).

## How the pieces connect

Three delivery shapes, the same `bty` library at the centre of all
three:

```
   Self-contained USB        USB + network catalog        PXE-driven (no operator)
   (no infra)                (light infra)                (full appliance)

   operator's box            operator's box               operator's workstation
   |                         |                            | browser
   v                         v                            v
 +----------------+      +----------------+            +-----------------+
 |  bty-usb       |      |  bty-usb       |            |  bty-server     |
 |  live env      |      |  live env      |            |  appliance      |
 |                |      |                |            |                 |
 | +------------+ |      | +------------+ |            | +-------------+ |
 | | bty-tui    | |      | | bty-tui    | |    HTTP    | | bty-web     | |
 | | (local     | |      | | --server   +-+----------->+ | iPXE/TFTP/  | |
 | |  catalog)  | |      | |   URL)     | | (catalog)  | | dnsmasq     | |
 | +-----+------+ |      | +-----+------+ |            | +------+------+ |
 +-------+--------+      +-------+--------+            +--------+--------+
         |                       |                              |
         | bty flash             | bty flash                    | iPXE
         | (BTY_IMAGES)          | (image fetched               | chain to
         |                       |   from catalog               | target +
         v                       |   server)                    | flash-on-
   +-----------+                 v                              | boot
   | target    |           +-----------+                        v
   | machine   |           | target    |                  +---------------+
   | local     |           | machine   |                  | target machine|
   | disk      |           | local     |                  | netboot env   |
   +-----------+           | disk      |                  | -> local disk |
                           +-----------+                  +---------------+

                              ^                                  ^
                              | network catalog                  | full PXE
                              | source (one of):                 | server
                              |                                  |
                              | * ghcr.io/safl/bty-web           | bty-server-x86
                              |   (Docker; trial / small team)   | bty-server-rpi
                              | * bty-server appliance           |
                              |   (also serves catalog over HTTP)|
```

The `bty` library implements the flashing logic (`bty.flash`,
`bty.images`, `bty.disks`) consumed by all three flows. `bty-tui` and
`bty-web` are UI shells; `bty-flash-on-boot` is the systemd service
that runs the flash unattended after a PXE boot. Same operations,
different delivery vehicles. The middle shape (`--server URL`) is
where the Docker container fits naturally - a single command on a
workstation gives a small team a shared image catalog without
standing up the appliance.

## `bty` (CLI)

Main command-line interface. The single source of truth for image
inspection, target-disk discovery, flashing, and provisioning. Every
other component is a UI or delivery vehicle for what `bty` does.

Installable on any Linux environment with a sufficient Python runtime:

```bash
pipx install bty-lab
```

## `bty-tui` (Terminal UI)

Terminal UI on top of the same library. Targeted at interactive use from
a live environment where a graphical browser is not appropriate - a
serial console, an SSH session, or a minimal recovery image. Exposes the
same operations as the CLI in a navigable form.

Requires the `tui` extra:

```bash
pipx install "bty-lab[tui]"
```

## `bty-web` (HTTP server + browser UI)

HTTP server with a browser UI, intended to run on the bty server
appliance. Hosts:

- MAC-address-keyed assignment of image and provisioning mode.
- Per-MAC iPXE configuration rendering.
- Bootstrap requests issued by the bty live environment during a
  network flash.
- Online CIJOE orchestration after a target first-boots, with
  per-machine known-good state tracking.

Requires the `web` extra:

```bash
pipx install "bty-lab[web]"
```

State (machine records, MAC <-> image/provisioning assignments, CIJOE
task references and run reports, known-good baselines, image
catalog metadata, server settings, sessions) is persisted in a
single SQLite database under the configured `BTY_STATE_DIR`. Backup
or migrate by copying the file.

CIJOE produces a structured report on every task run. `bty-web`
captures these reports - both for offline runs (sent back from the live
environment) and online runs (executed by the server itself) - and
exposes them in the UI per machine and per run. Reports are downloadable
in full, so an operator chasing a flaky reflash can inspect the
complete log without leaving the browser.

The runtime is sized for modest x86 hardware: lightweight Python web
framework, no heavy front-end build pipeline, no JVM dependencies.

## `bty-media/` (appliance-image builder)

Sibling directory at the repo root. Not a Python package. Builds four
appliance variants from a shared rootfs overlay:

**USB live image (`usb-x86`).** Bootable USB stick carrying the `bty`
runtime and an exFAT `BTY_IMAGES` partition for cooked images. Operator
plugs it in, boots a target, runs `bty flash` against the local disk.
Self-contained and offline. Direct-flash delivery vehicle.

**Server image, x86_64 (`server-x86`).** Installable disk image that,
when written to a host's disk and booted, runs the bty provisioning
server (`bty-web`, the iPXE/TFTP/HTTP services that PXE clients chain
through, the network-flash live environment those clients boot into,
and a storage layout for the image library). One artifact, ready to
serve a fleet. Headless: a plain-ASCII `/etc/issue` + `/etc/motd`
identify the appliance on the serial console and over SSH; no
graphical boot splash and no ASCII-art banner (a server is most often
watched via the serial console, where backslash-laden art confuses
agetty's escape parser and emits VT100 control bytes onto ttyS0).

**Server image, Raspberry Pi 4 / 5 (`server-rpi`).** Same appliance
role, delivered as an SD-card image for arm64. Built by mounting the
upstream Raspberry Pi OS Lite image and customising it in a
`qemu-aarch64-static` chroot. Operator `dd`'s the resulting `.img.zst`
to an SD card and boots a Pi 4 or Pi 5; first-boot ends at the same
`bty / bty` credential as the x86 server image.

**Network-flash live env (`netboot-x86`).** Kernel + initrd + squashfs
trio that PXE clients chain into. Built via Debian's `live-build`. The
chroot ships a `bty-flash-on-boot.service` oneshot that reads its
assignment from `/proc/cmdline`, downloads the assigned image, runs
`bty flash`, signals completion, and reboots.

## `ghcr.io/safl/bty-web` (Docker container)

A multi-arch container (`linux/amd64` + `linux/arm64`) built from
the same `bty-lab[web]` wheel and published to
[GitHub Container Registry](https://github.com/safl/bty/pkgs/container/bty-web)
on every tagged release. Hosts `bty-web` only - **no** dnsmasq,
TFTP, or iPXE proxy-DHCP, because Docker bridge networking cannot
relay L2 broadcasts.

Use cases:

- Trial / kicking-the-tires deploys: `docker run -p 8080:8080
  ghcr.io/safl/bty-web:latest` and the browser UI is up in seconds.
- Network-shared image catalog: a fleet of operators with bty USB
  sticks all point `bty-tui --server URL` at the same container.
- Local development backend for `bty-tui --server` work.

For PXE-boot provisioning, deploy the bare-metal `bty-server`
appliance instead. See
[`walkthrough-server-docker.md`](walkthrough-server-docker.md)
for the full operator guide.

The intended operator experience is appliance-grade:

1. `dd` (or `bty flash`) the image onto the server host's disk (or SD
   card, for the Pi variant).
2. Boot. Network comes up via DHCP; the appliance auto-starts
   `bty-web` on `:8080` with a default `bty / bty` credential and an
   `odus` admin user with passwordless sudo.
3. SSH in once to rotate the password (`sudo passwd bty`) and then
   open `/ui/login` in a browser.
4. The Settings page activates the dnsmasq proxy-DHCP block when
   you're ready to start serving PXE; everything else (machine
   assignments, image catalog, boot artifacts) is browser-driven from
   that point on.

*Hardware targets.* `server-x86` runs on any amd64 box that boots a
Debian cloud image: older Intel NUCs, discarded 1U servers, recent
GMKtec mini-PCs. The same artifact also boots as a VM disk.
`server-rpi` targets the 64-bit Raspberry Pis (4 and 5); both boot the
SD-card image natively.

## CIJOE provisioning (post-boot only)

After the target first-boots into the freshly-flashed OS,
`bty-web` SSHes in and runs a CIJOE task against the running
machine. v0.7.39 narrowed bty's CIJOE surface to this
server-driven mode only -- offline image-mounting is the image
cooker's job.

**Transport config layering.** bty-web always synthesises a
`transport.toml` with `[cijoe.transport.ssh]` pinned at the
target's `last_seen_ip` + the operator-supplied SSH key at
`/var/lib/bty/keys/id_ed25519`. The operator can drop a second
config file at `/var/lib/bty/cijoe-user-config.toml`
(overridable via `BTY_CIJOE_USER_CONFIG`) with task-specific
settings, additional named transports, environment variables,
etc. cijoe is invoked as:

```
cijoe <task.yaml> --config <user-config> --config <transport.toml> --monitor
```

The user config loads first; bty-web's transport TOML loads
LAST and wins on conflicts -- so the SSH transport that targets
the right machine is always under bty-web's control. The
operator can layer extra named transports / settings on top
without redirecting commands to the wrong host.

**Non-default SSH port.** Targets that listen on something
other than 22 (a separate IPMI host, an SSH jumpbox, a homelab
NAT-mapped port) configure via `BTY_CIJOE_SSH_PORT` at the
bty-web service level. Out-of-range or non-numeric values fall
back to 22 silently so a typo doesn't break every task run; set
it once in `/etc/default/bty-web` (or the systemd unit's
`Environment=`) for the whole appliance.

**Cancelable.** The operator can DELETE `/tasks/{mac}` from the
UI's "Cancel running task" button; bty-web flips a
`threading.Event` and `Popen.terminate()`s the cijoe subprocess.
Status surfaces via `last_task_status` and the SSE
machines-update channel; lifecycle events land in the audit log
(`/ui/events`).

## Audit log

bty-web records "who did what when" rows to a slim `events`
table in `state.db` and surfaces them at `/ui/events` (HTML
table) and `GET /events` (JSON API). Every operator action,
PXE-client check-in, and async-manager terminal status lands
a row.

**Schema:** `kind` (dotted namespace e.g. `machine.discovered`,
`image.hashed`), `subject_kind` + `subject_id` (the entity
the event is about), `actor` (`operator` / `system` /
`pxe-client`), `source_ip` (request client host or target IP,
v4-mapped-v6 normalised to bare v4), `summary` (operator-
readable string), `details` (JSON blob with extras). Append-
only; no auto-trimming -- the table is a few KB per event so
years of homelab activity fit. Operators with strict retention
needs run `DELETE FROM events WHERE ts < ?` themselves.

**Failure symmetry.** Every async-manager + operator-driven
action that can fail emits a paired `<kind>_failed` event
(`image.upload_failed`, `image.hash_failed`,
`boot.release.fetch_failed`, `settings.pxe.activate_failed`,
`catalog.entry.add_failed`). Failed kinds render in the
events table with a danger-coloured badge so they pop in a
long log.

**Filtering.** The `/ui/events` filter form and the JSON API
both accept `kind`, `subject_kind`, `subject_id`, `actor`,
`source_ip`. Click-pivot links on each cell of the events
table jump to the timeline filtered by that value, so an
operator can ask "everything from 192.168.1.5" or "everything
that touched image X" with one click.

**Recent-activity cards** on `/ui/dashboard`, `/ui/machines/
{mac}`, `/ui/images`, `/ui/boot`, and `/ui/settings` all
embed the same `_events_card.html` partial filtered to the
relevant subject so each page has a short timeline of
context-relevant rows.
