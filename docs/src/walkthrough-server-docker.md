# Walkthrough: bty-web in Docker

A pre-built bty-web container is published to
[`ghcr.io/safl/bty-web`](https://github.com/safl/bty/pkgs/container/bty-web)
on every tagged release. It hosts bty-web's **image catalog**, **machine
registry**, and **browser UI**. `bty --catalog SOURCE` clients (from the
USB live env or a workstation) connect to it and pick images for flashing.

This is not the bty-server appliance, but it bundles the same PXE stack:
bty-web (HTTP + browser UI) plus `dnsmasq` serving the iPXE bootfiles over
TFTP. Both boot lanes work:

- **UEFI HTTP Boot targets**: DHCP option 67 = `http://<bty-web>:8080/boot/ipxe.efi`;
  modern UEFI firmware fetches the binary over HTTP, no TFTP in the path.
- **TFTP PXE targets** (legacy BIOS, older UEFI): DHCP option 66 = the
  container host's IP, option 67 = `ipxe.efi`; the container's dnsmasq
  serves it over TFTP. Publish `69/udp` for this (see Quick start).
- **`boots-from` USB sticks**: boot a target from a
  [`boots-from`](https://github.com/safl/boots-from) USB whose embedded
  iPXE script chains to bty-web's `/pxe-bootstrap.ipxe`, replacing the
  firmware PXE step entirely - no DHCP-PXE options or TFTP needed at all.

The differences from the appliance are operational, not functional: the
container's dnsmasq is launched by the entrypoint (not systemd), so the
Netboot page shows its status but can't Start/Stop/Restart it, and there's
no cloud-init / rootfs-grow (it's a container, not a disk image).

## When to use this

- **Trial / kicking the tires**: poke at the bty-web UI without flashing an
  SD card or burning a NUC.
- **Image library**: a fleet of operators carry bty USB sticks and want a
  network-shared catalog of pre-built images instead of copying files to
  every stick.
- **Local-dev backend** for `bty --catalog` work.
- **Production PXE / HTTP-Boot deployment**: serves TFTP PXE, UEFI HTTP
  Boot, and `boots-from` sticks. Reach for the bty-server appliance
  instead when you want a turnkey disk image with systemd-managed services
  rather than a container to run.

## Quick start

The container runs `bty-web` as the unprivileged `bty` user (uid 999),
matching the bare-metal appliance. Pre-create the host data dir with that
ownership before starting (one-time, host-side):

```bash
mkdir -p ./bty-data
sudo chown -R 999:999 ./bty-data    # match the in-container bty user
docker run -d --name bty-web \
  -p 8080:8080 \
  -p 69:69/udp \
  -v "$PWD/bty-data":/var/lib/bty \
  ghcr.io/safl/bty-web:latest
```

Or skip the chown by using a docker-managed volume (inherits the
image's ownership automatically):

```bash
docker run -d --name bty-web \
  -p 8080:8080 \
  -p 69:69/udp \
  -v bty-data:/var/lib/bty \
  ghcr.io/safl/bty-web:latest
```

`69/udp` publishes TFTP for PXE clients (drop it if you only use HTTP
Boot or `boots-from`). If TFTP transfers stall behind Docker's NAT, run
with `--network host` instead of `-p`.

Open <http://localhost:8080/ui> and log in with `bty / bty`. Drop
`.img.zst` / `.qcow2` / `.img.gz` files into the data directory's `images/`
subfolder and they appear in the catalog after a refresh.

If the bind-mount permission isn't right, the entrypoint exits with a
one-line fix command instead of crashing deep in a Python traceback.

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
`/var/lib/bty/images`. Pick an image (Enter), pick a target disk, confirm
the flash plan. The server is the catalog source; the write happens on the
local machine running `bty`.

## Scripted flash via the plan endpoint (no wizard)

For batch / CI workflows: bind the machine on the appliance with
`boot_mode=bty-flash-always` + a `bty_image_ref` + a `target_disk_serial`,
then on the target run `bty --server <host> --mac <self-mac>`. The wizard
skips and the flash runs scripted (`mode=flash` from `GET
<host>/pxe/<mac>/plan`). The image streams directly from the container's
catalog through the live env to the target's disk; no operator copy step.

For one-shot ad-hoc flashes (no MAC binding), the wizard's URL accept
covers HTTP and `oras://` sources via `bty --catalog ...`.

## Rotating the default credentials

The pre-built image ships with `bty / bty` so the operator can start
poking immediately. **Rotate before exposing past a trusted LAN.**

```bash
docker exec -it -u root bty-web passwd bty
```

The `-u root` runs `passwd` as root inside the container so it prompts only
for the new password (skipping "current password" the way `passwd bty`
would when invoked as the bty user). The new hash lands in `/etc/shadow`
inside the container; restart-resilient, since pamela reads `/etc/shadow`
directly on every auth call.

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

The published image is `linux/amd64` + `linux/arm64`. Pull from a Pi 4 or
Pi 5 as easily as from an x86 host. Pure-Python wheel, so the only per-arch
differences are the apt-installed system deps.

## Differences vs the appliance

The container runs the same bty-web + dnsmasq (TFTP) stack as the
appliance. The differences are operational:

- **TFTP daemon control**: the container's dnsmasq is launched by the
  entrypoint, not systemd, so the Netboot page shows its status but can't
  Start/Stop/Restart it (manage the daemon with `docker` instead).
- **No cloud-init / first-boot rootfs grow**: it's a container image, not
  a disk image.

bty runs no DHCP role in either shape: the operator's LAN DHCP server
points PXE clients at bty (via option 60/66/67 tagging) whether bty runs
as the appliance or this container.
