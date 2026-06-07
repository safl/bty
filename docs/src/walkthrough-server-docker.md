# Walkthrough: bty-web in Docker

A pre-built bty-web container is published to
[`ghcr.io/safl/bty-web`](https://github.com/safl/bty/pkgs/container/bty-web)
on every tagged release. It hosts bty-web's **image catalog**, **machine
registry**, and **browser UI**. `bty --catalog SOURCE` clients (from the
USB live env or a workstation) connect to it and pick images for flashing.

This container is HTTP-only: bty-web serves the UI, the per-MAC PXE plans,
the boot artifacts, and the images over HTTP on `:8080`. Targets boot one
of three ways:

- **UEFI HTTP Boot targets**: DHCP option 67 = `http://<bty-web>:8080/boot/ipxe.efi`;
  modern UEFI firmware fetches the binary over HTTP, no TFTP in the path.
- **TFTP PXE targets** (legacy BIOS, older UEFI) that can only bootstrap
  over TFTP: add the `bty-tftp` sidecar, which serves the ~1 MB iPXE
  bootfile over udp/69 and is part of the compose / Quadlet deploy under
  [`deploy/`](https://github.com/safl/bty/tree/main/deploy). This container
  serves no TFTP itself.
- **`boots-from` USB sticks**: boot a target from a
  [`boots-from`](https://github.com/safl/boots-from) USB whose embedded
  iPXE script chains to bty-web's `/pxe-bootstrap.ipxe`, replacing the
  firmware PXE step entirely -- no DHCP-PXE options or TFTP needed at all.

## Quick start -- the canonical container deploy

`uvx bty-lab deploy` writes the compose stack (bty-web + withcache, plus
an optional TFTP sidecar), auto-fills envvars, and brings it up in one
shot. No clone needed; `uv` (or `pipx`) on the host is enough:

```bash
sudo mkdir -p /opt/bty && sudo chown "$USER:$USER" /opt/bty
uvx bty-lab deploy /opt/bty
#   bty: :8080/ui  withcache: :3000/
```

`deploy` detects `HOST_ADDR` from the host's outbound-route IP and
generates random passwords (printed in the final summary, also written to
`/opt/bty/envvars`). Pass `--host-addr 192.0.2.10` to override the
detection, or `--force` to overwrite an existing `envvars`.

For systemd-managed auto-start on boot, add `--systemd` (installs Podman
Quadlet units to `/etc/containers/systemd/` and starts them; requires
root):

```bash
sudo uvx bty-lab deploy /opt/bty --systemd
```

Upgrade against a newer bty release in one shot:

```bash
uvx bty-lab upgrade /opt/bty     # auto-detects compose- vs Quadlet-managed
```

`upgrade` preserves `envvars` + `data/`, regenerates compose against the
CLI's bty version, `podman compose pull`s, then restarts (or `systemctl
restart`s for Quadlet-managed stacks).

`bty-web` reads `$BTY_WITHCACHE_URL` (set by the compose) at boot and
auto-wires withcache as its image source -- no UI configuration step.
For inspect-then-apply control, use `bty-lab init` instead; it emits the
same files without side effects. Full details:
[`deploy/README.md`](https://github.com/safl/bty/blob/main/deploy/README.md).

## Bare `docker run` (dev / single-container)

For contributor work on the bty-web UI alone -- no withcache, no PXE -- a
bare `docker run` works too. The container runs `bty-web` as uid 1000;
either pre-chown a bind-mount or use a docker-managed volume so first-boot
can write `state.db`:

```bash
docker run -d --name bty-web \
  -p 8080:8080 \
  -v bty-data:/var/lib/bty \
  ghcr.io/safl/bty-web:latest
# -> http://localhost:8080/ui   (UI open; set BTY_ADMIN_PASSWORD to gate it)
```

This is HTTP-only -- legacy TFTP PXE needs the `bty-tftp` sidecar in the
compose deploy above.

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

For batch / CI workflows: bind the machine on the server with
`boot_mode=bty-flash-always` + a `bty_image_ref` + a `target_disk_serial`,
then on the target run `bty --server <host> --mac <self-mac>`. The wizard
skips and the flash runs scripted (`mode=flash` from `GET
<host>/pxe/<mac>/plan`). The image streams directly from the container's
catalog through the live env to the target's disk; no operator copy step.

For one-shot ad-hoc flashes (no MAC binding), the wizard's URL accept
covers HTTP and `oras://` sources via `bty --catalog ...`.

## Gating the operator UI

The operator UI is gated by `$BTY_ADMIN_PASSWORD` (constant-time compare);
when it is unset the UI is open and bty-web logs a startup warning. **Set it
before exposing past a trusted LAN.** Pass it on the `docker run`:

```bash
docker run -d --name bty-web \
  -e BTY_ADMIN_PASSWORD=your-secret \
  -p 8080:8080 \
  -v bty-data:/var/lib/bty \
  ghcr.io/safl/bty-web:latest
```

Rotate by changing `BTY_ADMIN_PASSWORD` and restarting the container; the
setting survives image rebuilds and pulls since it lives in the run command
(or your compose file), not in the image.

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
| `BTY_ADMIN_PASSWORD` | unset | Gates the operator UI (constant-time compare); unset = open, with a startup warning |

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

## Operational notes

- **No bundled TFTP**: this container serves only HTTP. Legacy TFTP PXE is
  the `bty-tftp` sidecar's job (see `deploy/`); the Netboot page's TFTP
  daemon controls apply to a host/systemd install, not this container.
- **State on a named volume**: bty's DB and images live under
  `/var/lib/bty`; bind-mount or volume-mount it so state survives container
  re-pulls.

bty runs no DHCP role: the operator's LAN DHCP server points PXE clients at
bty (via option 60/66/67 tagging).
