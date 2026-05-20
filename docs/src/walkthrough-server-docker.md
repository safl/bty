# Walkthrough: bty-web in Docker

A pre-built bty-web container is published to
[`ghcr.io/safl/bty-web`](https://github.com/safl/bty/pkgs/container/bty-web)
on every tagged release. It hosts the **image catalog**, **machine
registry**, and **browser UI** of bty-web. `bty --catalog SOURCE`
clients (running from the USB live env or a workstation) connect
to it and pick images for flashing.

This is not the bty-server appliance. The appliance bundles
`dnsmasq` for TFTP serving on top of bty-web; the container is
**HTTP-only by design**. This makes the container the
**HTTP-Boot / `boots-from` deployment lane** -- production-fit
for either of:

- **UEFI HTTP Boot targets**: the operator's LAN DHCP server is
  configured to serve DHCP option 67 = `http://<bty-web>/ipxe.efi`
  (bty-web serves the iPXE binaries from `/boot/` over HTTP).
  Modern UEFI firmware fetches the binary natively -- no TFTP in
  the path at all.
- **`boots-from` USB sticks**: the operator boots a target from a
  [`boots-from`](https://github.com/safl/boots-from) USB whose
  embedded iPXE script chains to bty-web's `/pxe-bootstrap.ipxe`.
  The stick replaces the firmware-driven PXE step entirely, so
  neither DHCP-PXE options nor a TFTP daemon are needed on the
  LAN. Works on legacy BIOS too.

For mixed-firmware fleets that include legacy BIOS or older UEFI
implementations that only support TFTP option 67, deploy the
bty-server appliance instead (see
[walkthrough-server.md](walkthrough-server.md)) -- it bundles
the TFTP daemon.

## When to use this

- **Trial / kicking the tires**: you want to poke at the bty-web UI
  without flashing an SD card or burning a NUC.
- **Image library**: you have a fleet of operators who all carry
  bty USB sticks and want a network-shared catalog of pre-built
  images instead of copying files to every stick.
- **Local-dev backend** for `bty --catalog` work.
- **Production HTTP-Boot or `boots-from` deployment** for fleets
  that don't need a TFTP daemon (UEFI-HTTP-Boot-capable firmware
  or USB-driven targets).

For mixed-firmware fleets that include TFTP-only PXE clients,
deploy the bty-server appliance instead.

## Quick start

The container runs `bty-web` as the unprivileged `bty` user (uid
999), matching the bare-metal appliance. Pre-create the host data
dir with that ownership before starting (one-time, host-side):

```bash
mkdir -p ./bty-data
sudo chown -R 999:999 ./bty-data    # match the in-container bty user
docker run -d --name bty-web \
  -p 8080:8080 \
  -v "$PWD/bty-data":/var/lib/bty \
  ghcr.io/safl/bty-web:latest
```

Or skip the chown by using a docker-managed volume (inherits the
image's ownership automatically):

```bash
docker run -d --name bty-web \
  -p 8080:8080 \
  -v bty-data:/var/lib/bty \
  ghcr.io/safl/bty-web:latest
```

Open <http://localhost:8080/ui> and log in with `bty / bty`. Drop
`.img.zst` / `.qcow2` / `.img.gz` files into the data directory's
`images/` subfolder and they appear in the catalog after a refresh.

If the bind-mount permission isn't right, the entrypoint exits
with a one-line fix command instead of letting bty-web crash deep
in a Python traceback.

## Compose

The repo ships a sample under
[`docker/docker-compose.yml`](https://github.com/safl/bty/blob/main/docker/docker-compose.yml):

```bash
curl -fLO https://raw.githubusercontent.com/safl/bty/main/docker/docker-compose.yml
docker compose up -d
```

Same defaults: `:8080` published, `./bty-data/` bind-mounted as the
volume, `restart: unless-stopped`.

## Connecting `bty`

From a workstation or the USB live env:

```bash
bty --catalog http://<host>:8080/catalog.toml
```

The catalog pane fills with whatever the server has under
`/var/lib/bty/images`. Pick an image (Enter), pick a target disk,
confirm the flash plan. The server is the catalog source; the
actual write happens on the local machine running `bty`.

## Scripted flash via the plan endpoint (no wizard)

For batch / CI workflows: bind the machine on the appliance with
`boot_policy=bty-flash-always` + a `bty_image_ref` + a `target_disk_serial`,
then on the target run `bty --server <host> --mac <self-mac>`. The
wizard skips and the flash runs scripted (`mode=auto` from
`GET <host>/pxe/<mac>/plan`). The image streams directly from the
container's catalog through the live env to the target's disk; no
operator copy step.

For one-shot ad-hoc flashes (no MAC binding), the wizard's URL
accept covers HTTP and `oras://` sources via `bty --catalog ...`.

## Rotating the default credentials

The pre-built image ships with `bty / bty` so the operator can start
poking at the UI immediately. **Rotate before exposing past a
trusted LAN.**

```bash
docker exec -it -u root bty-web passwd bty
```

The `-u root` runs `passwd` as root inside the container so it
prompts only for the new password (skipping "current password"
the way `passwd bty` would when invoked as the bty user). The new
hash lands in `/etc/shadow` inside the container; restart-resilient,
since pamela reads `/etc/shadow` directly on every auth call.

> If you rebuild or pull a fresh image, the password resets to
> `bty / bty` because the new container's `/etc/shadow` comes
> from the image. To survive image upgrades, bind-mount
> `/etc/shadow` from the host or set up an external auth proxy
> in front of bty-web.

## Volume layout

The container expects a single volume at `/var/lib/bty`:

```
/var/lib/bty/
  state.db           SQLite: machines, MAC -> image bindings, sessions
  session-secret     bty-web cookie key (generated on first start)
  images/            pre-built image catalog (bind-mountable from host)
  boot/              kernel / initrd / squashfs (only used by PXE flow)
```

`state.db` is plain SQLite; back it up by stopping the container
and copying the file. Migrations run automatically on every start.

## Environment variables

| Var | Default | Purpose |
|---|---|---|
| `BTY_WEB_HOST` | `0.0.0.0` | Listen address |
| `BTY_WEB_PORT` | `8080` | Listen port |
| `BTY_STATE_DIR` | `/var/lib/bty` | Where `state.db` and `session-secret` live |
| `BTY_IMAGE_ROOT` | `/var/lib/bty/images` | Image catalog directory |
| `BTY_BOOT_DIR` | `${BTY_STATE_DIR}/boot` | Kernel/initrd/squashfs (PXE boot artifacts) |
| `BTY_SESSION_SECRET` | (generated) | Cookie key override; useful for multi-instance |
| `BTY_QUIET` | unset | Suppress the start-up banner with default credentials |

## Building locally

The Dockerfile expects a wheel under `dist/`. Build the wheel
first, then the image:

```bash
uv build
docker build -f docker/Dockerfile -t bty-web:dev .
docker run --rm -p 8080:8080 -v "$PWD/bty-data":/var/lib/bty bty-web:dev
```

## Multi-arch

The published image is `linux/amd64` + `linux/arm64`. Pull from a
Pi 4 or Pi 5 just as easily as from an x86 host. Pure-Python wheel,
so the only per-arch differences are the apt-installed system deps.

## What is missing vs the appliance

This container deliberately does not run:

- `dnsmasq` for TFTP. The container is HTTP-only; UEFI HTTP-Boot
  targets work directly against `/boot/ipxe.efi`. TFTP-only PXE
  clients would need a separate TFTP server (or use the appliance
  image instead, which bundles dnsmasq).
- Cloud-init / first-boot rootfs grow.

bty no longer runs any DHCP role in either deployment shape -- the
operator's LAN DHCP server is configured to point PXE clients at
bty (via option 60/66/67 tagging) regardless of whether bty runs
as the appliance or as this container. For HTTP-Boot-capable
firmware, that means this container is a complete deployment.
