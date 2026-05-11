# Walkthrough: bty-web in Docker

A pre-built bty-web container is published to
[`ghcr.io/safl/bty-web`](https://github.com/safl/bty/pkgs/container/bty-web)
on every tagged release. It hosts the **image catalog**, **machine
registry**, and **browser UI** of bty-web. `bty-tui --server URL`
clients (running from the USB live env or a workstation) connect
to it and pick images for flashing.

This is not the bty-server appliance. There is no DHCP / TFTP /
PXE proxy-DHCP in this container. Those services need bare-metal
LAN access for L2 broadcasts, which Docker bridge networking
cannot provide. For the full PXE flow, deploy the bty-server
appliance image (see [walkthrough-server.md](walkthrough-server.md)).

## When to use this

- **Trial / kicking the tires**: you want to poke at the bty-web UI
  without flashing an SD card or burning a NUC.
- **Image library**: you have a fleet of operators who all carry
  bty USB sticks and want a network-shared catalog of cooked
  images instead of copying files to every stick.
- **Local-dev backend** for `bty-tui --server` work.

If your goal is to PXE-boot targets onto network-flash, you need
the bty-server appliance instead.

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

## Connecting bty-tui

From a workstation or the USB live env:

```bash
bty-tui --server http://<host>:8080
```

The catalog pane fills with whatever the server has under
`/var/lib/bty/images`. Pick a row with `Enter`, pick a target disk,
flash. The server is the catalog source; the actual write happens
on the local machine running `bty-tui`.

## Scripted flash via URL (no TUI)

For batch / CI workflows the `bty` CLI accepts an HTTP URL
directly as `--image`, so a script doesn't need to download
images first:

```bash
sudo bty flash \
    --image  http://<host>:8080/images/my-image.img.zst \
    --target /dev/sda \
    --yes
```

`.img` and `.img.zst` URLs stream straight from the container
through `zstd -d | dd` to disk; `.qcow2` URLs download to a temp
file first. Combined with the container running on a teammate's
workstation, this turns "flash this box from the shared catalog"
into a single command - no operator copy step, no preconfigured
client.

## Rotating the default credentials

The cooked image ships with `bty / bty` so the operator can start
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
  images/            cooked image catalog (bind-mountable from host)
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

- `dnsmasq` (proxy-DHCP / TFTP) - needs L2 broadcast access
- iPXE binaries staged under `/var/lib/tftpboot/`
- The `bty-web-activate-pxe` privileged helper
- Cloud-init / first-boot rootfs grow

The PXE chain (a target machine boots into the bty network-flash
live env, gets its image assignment, writes to local disk) requires
all of the above, which is why the appliance exists. Use the
container for everything that does not require booting another
machine through this server.
