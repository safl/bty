# Components

bty is one Python package - the `bty` module, distributed on PyPI as
[`bty-lab`](https://pypi.org/project/bty-lab/) - with three console-script
entry points, plus a sibling appliance-image builder (`bty-media/`).

## How the pieces connect

Three delivery shapes, the same `bty` library at the centre:

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
 | | bty        | |      | | bty        | |    HTTP    | | bty-web     | |
 | | (local     | |      | | --catalog  +-+----------->+ | iPXE/TFTP/  | |
 | |  catalog)  | |      | |   SOURCE)  | | (catalog)  | | dnsmasq     | |
 | +-----+------+ |      | +-----+------+ |            | +------+------+ |
 +-------+--------+      +-------+--------+            +--------+--------+
         |                       |                              |
         |  dd to disk           | dd to disk                   | iPXE
         | (BTY_IMAGES)          | (image fetched               | chain ->
         |                       |   from catalog               | live env ->
         v                       |   server)                    | bty in
   +-----------+                 v                              | flash mode
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

The `bty` package implements the flashing logic (`bty.flash`,
`bty.images`, `bty.disks`, `bty.catalog`, `bty.oras`) consumed by both
shipping flows. ``bty`` (the operator wizard) and ``bty-web`` (the HTTP
server) are the two UI shells; in the netboot live env, ``bty`` is launched
on tty1 by `bty-on-tty1.service` and dispatches via the bty-web plan
endpoint - no separate auto-flash service. Same operations, different
delivery vehicles. The middle shape (`--catalog SOURCE`, typically pointed
at a bty-web instance's `/catalog.toml`) is where the Docker container
fits: a single command on a workstation gives a small team a shared image
catalog without the appliance.

## `bty` (wizard + library)

The operator-facing tool: a Rich-based wizard that picks an image + a
target disk and flashes; the same code also runs in scripted /
server-driven mode via the bty-web plan endpoint. Single source of truth
for image inspection, target-disk discovery, flashing, and remote-catalog
ingestion. Library modules (`bty.flash`, `bty.images`, `bty.catalog`,
`bty.disks`, `bty.oras`) are stable Python API for in-process scripting.

Three invocation shapes:

- `bty` -- interactive wizard, local image-root only.
- `bty --catalog URL` -- interactive wizard with the catalog
  pre-loaded.
- `bty --server X --mac Y` -- server-driven via
  `<X>/pxe/<Y>/plan`.

Requires the `tui` extra (Rich):

```bash
pipx install "bty-lab[tui]"
```

## `bty-web` (HTTP server + browser UI)

HTTP server with a browser UI, intended to run on the bty server
appliance. Hosts:

- MAC-address-keyed assignment of image to machine.
- Per-MAC iPXE configuration rendering.
- Bootstrap requests issued by the bty live environment during a network
  flash.
- An audit log of operator + machine activity (see "Audit log" below).

Requires the `web` extra:

```bash
pipx install "bty-lab[web]"
```

`bty-web` is a flasher only: it writes bytes, records what was flashed
when, and never opens an SSH session to a flashed target. First-boot
bring-up belongs in the image builder (cloud-init / NoCloud user-data baked
at image-build time); bty-web holds zero credentials against the targets it
flashes.

State (machine records, MAC <-> image assignments, image catalog metadata,
server settings, sessions, audit-log events) is persisted in a single
SQLite database under the configured `BTY_STATE_DIR`. Backup or migrate by
copying the file.

The runtime is sized for modest x86 hardware: lightweight Python web
framework, no heavy front-end build pipeline, no JVM dependencies.

## `bty-media/` (appliance-image builder)

Sibling directory at the repo root. Not a Python package. Builds four
appliance variants from a shared rootfs overlay:

**USB live image (`usb-x86`).** Bootable USB stick carrying the `bty`
runtime and an exFAT `BTY_IMAGES` partition for pre-built images. Operator
plugs it in, boots a target; ``bty`` auto-launches on tty1 and walks
through pick + flash. Self-contained and offline. Direct-flash delivery
vehicle.

**Server image, x86_64 (`server-x86`).** Installable disk image that, once
written to a host's disk and booted, runs the bty provisioning server
(`bty-web`, the iPXE/TFTP/HTTP services PXE clients chain through, the
network-flash live environment those clients boot into, and a storage
layout for the image library). One artifact, ready to serve a fleet.
Headless: a plain-ASCII `/etc/issue` + `/etc/motd` identify the appliance
on the serial console and over SSH; no graphical boot splash and no
ASCII-art banner (a server is most often watched via the serial console,
where backslash-laden art confuses agetty's escape parser and emits VT100
control bytes onto ttyS0).

**Server image, Raspberry Pi 4 / 5 (`server-rpi`).** Same appliance role,
delivered as an SD-card image for arm64. Built by mounting the upstream
Raspberry Pi OS Lite image and customising it in a `qemu-aarch64-static`
chroot. Operator `dd`'s the resulting `.img.gz` (after `gunzip`) to an SD
card and boots a Pi 4 or Pi 5; first-boot ends at the same
`$BTY_ADMIN_PASSWORD`-gated operator UI as the x86 server image.

**Network-flash live env (`netboot-x86`).** Kernel + initrd + squashfs trio
that PXE clients chain into. Built via Debian's `live-build`. The chroot
ships `bty-on-tty1.service` (unconditional; runs on every boot), which
exec's `bty --server X --mac Y` (values from `/proc/cmdline`); ``bty`` GETs
`<server>/pxe/<mac>/plan` and dispatches (auto-flash without prompts,
interactive wizard, or no-op-and-exit).

## `ghcr.io/safl/bty-web` (Docker container)

A multi-arch container (`linux/amd64` + `linux/arm64`) built from the same
`bty-lab[web]` wheel and published to
[GitHub Container Registry](https://github.com/safl/bty/pkgs/container/bty-web)
on every tagged release. Hosts `bty-web` only - **HTTP-only by design**: no
TFTP daemon, no DHCP role. The container is the **HTTP-Boot / `boots-from`
deployment lane** for fleets where either:

- Target firmware supports **UEFI HTTP Boot**: the operator's router serves
  DHCP option 67 = ``http://bty-web/ipxe.efi`` (bty-web serves the iPXE
  binaries from ``/boot/`` over HTTP); no TFTP end-to-end.
- Targets boot from a [`boots-from`](https://github.com/safl/boots-from)
  USB stick whose embedded iPXE script chains to bty-web's
  ``/pxe-bootstrap.ipxe``; the stick replaces the
  PXE-firmware-fetches-bootfile step entirely, so neither DHCP-PXE options
  nor a TFTP daemon are needed on the LAN.

For mixed-firmware fleets that include **legacy BIOS** or older UEFI
implementations that only support TFTP option 67, use the `bty-server`
appliance instead - it bundles ``dnsmasq`` configured for TFTP serving
alongside bty-web.

Use cases:

- Trial / kicking-the-tires deploys: `docker run -p 8080:8080
  ghcr.io/safl/bty-web:latest` and the browser UI is up in seconds.
- Network-shared image catalog: a fleet of operators with bty USB
  sticks all point `bty --catalog SOURCE` at the same container.
- Local development backend for `bty --catalog` work.
- Production PXE-flash for **UEFI-HTTP-Boot-capable** or
  **`boots-from`**-driven fleets where TFTP is not in the path.

See [`walkthrough-server-docker.md`](walkthrough-server-docker.md) for the
full operator guide.

The intended operator experience is appliance-grade:

1. `dd` (or boot the bty USB live, pick the bty-server entry from
   the wizard's starter catalog, flash) the image onto the server
   host's disk (or SD card, for the Pi variant).
2. Boot. Network comes up via DHCP; the appliance auto-starts
   `bty-web` on `:8080`, the operator UI gated by `$BTY_ADMIN_PASSWORD`
   (unset = open, with a startup warning), and an `odus` admin user with
   passwordless sudo.
3. Set `$BTY_ADMIN_PASSWORD` (and restart bty-web) to gate the UI, then
   open `/ui/login` in a browser.
4. The Netboot page shows how to point your LAN DHCP server (option
   60/66/67) at the appliance (bty serves TFTP, not DHCP); everything
   else (machine assignments, image catalog, boot artifacts) is
   browser-driven from that point on.

*Hardware targets.* `server-x86` runs on any amd64 box that boots a Debian
cloud image: older Intel NUCs, discarded 1U servers, recent GMKtec
mini-PCs. The same artifact also boots as a VM disk. `server-rpi` targets
the 64-bit Raspberry Pis (4 and 5); both boot the SD-card image natively.

## No post-flash provisioning

bty has no online provisioning surface. The bty-web server is a flasher: it
writes bytes, records when bytes were written, and never opens an SSH
session to a flashed target. "The flasher holds root creds on every machine
it ever provisioned" is a bad security shape, so the surface intentionally
does not exist.

First-boot bring-up belongs in the image builder: cloud-init / NoCloud
user-data baked into the image at build time. Post-boot config management
is anything you run from the target itself (cijoe over SSH, ansible, etc.),
not from bty-web.

## Audit log

bty-web records "who did what when" rows to a slim `events` table in
`state.db` and surfaces them at `/ui/events` (HTML table) and `GET /events`
(JSON API). Every operator action, PXE-client check-in, and async-manager
terminal status lands a row.

**Schema:** `kind` (dotted namespace e.g. `machine.discovered`,
`image.hashed`), `subject_kind` + `subject_id` (the entity the event is
about), `actor` (`operator` / `system` / `pxe-client`), `source_ip`
(request client host or target IP, v4-mapped-v6 normalised to bare v4),
`summary` (operator-readable string), `details` (JSON blob with extras).
Append-only; no auto-trimming - the table is a few KB per event so years of
homelab activity fit. Operators with strict retention needs run `DELETE
FROM events WHERE ts < ?` themselves.

**Behind a reverse proxy.** When bty-web sits behind nginx / caddy /
Traefik, set `BTY_TRUSTED_PROXY=1` so audit rows record the real client IP
from `X-Forwarded-For` rather than the proxy's loopback. Off by default
because the header is client-spoofable - only enable it when the proxy
strips inbound `X-Forwarded-For` from external requests.

**Failure symmetry.** Every async-manager + operator-driven action that can
fail emits a paired `<kind>_failed` event (`auth.login.failed`,
`image.upload_failed`, `image.hash_failed`,
`netboot.artifacts.fetch_failed`, `netboot.tftp.control_failed`,
`catalog.entry.add_failed`). Failed kinds render with a danger-coloured
badge so they pop in a long log.

**Filtering.** The `/ui/events` filter form and the JSON API both accept
`kind`, `subject_kind`, `subject_id`, `actor`, `source_ip`. Click-pivot
links on each cell jump to the timeline filtered by that value, so an
operator can ask "everything from 192.168.1.5" or "everything that touched
image X" with one click.

**Recent-activity cards** on `/ui/dashboard`, `/ui/machines/{mac}`,
`/ui/images`, and `/ui/netboot` (list + fetch sections) all embed the same
`_events_card.html` partial filtered to the relevant subject, so each page
has a short timeline of context-relevant rows. ``/ui/settings`` is now a
thin operator-account page and omits the card; the global timeline lives
under ``/ui/events``.
