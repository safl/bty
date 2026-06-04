# Deploy bty + withcache on a host

Run bty and [withcache](https://github.com/safl/withcache) as containers on a
host with podman (e.g. a nosi headless image). bty is the policy/PXE layer;
withcache holds the image bytes. Pull new images to upgrade.

Services:

- **bty-web** — serves the UI, PXE plans, boot artifacts, and images over HTTP.
- **withcache** — URL-keyed artifact cache; bty prefers it as the image source
  for artifacts it holds.
- **tftp** *(profile `tftp`)* — serves the iPXE NBP over TFTP for BIOS/legacy
  PXE clients that bootstrap that way.

Your LAN's DHCP server points clients at this host (next-server/bootfile, or
HTTPClient).

## Quick start (compose)

```sh
export HOST_ADDR=10.0.0.5                 # this host's LAN address
export WITHCACHE_ADMIN_PASSWORD=change-me # protects withcache's operator UI

podman compose -f deploy/compose.yml up -d
#   bty:       http://<host>:8080/ui
#   withcache: http://<host>:3000/

# add TFTP for BIOS clients that bootstrap over it:
podman compose -f deploy/compose.yml --profile tftp up -d
```

bty hands the withcache URL to *booting clients*, so `HOST_ADDR` is the host's
LAN address those machines reach. The `tftp` profile uses host networking
(TFTP's data transfer uses an ephemeral UDP port); bty-web and withcache use the
bridge with published ports.

## How the caching behaves

`BTY_WITHCACHE_URL` wires bty to the cache. For an https image origin, bty does
a cheap `HEAD` to withcache: when it's cached, the boot plan points the client
at withcache; when the cache lacks it (or is unreachable), bty serves the
origin. The probe also warms an auto-fetch withcache — a miss enqueues the
background fill — so the next machine you provision hits the cache.

## Upgrades

```sh
podman compose -f deploy/compose.yml pull
podman compose -f deploy/compose.yml up -d
```

State (bty's DB/images, withcache's blobs) lives in named volumes and persists
across image upgrades.

## Boot-autostart (Quadlet)

The Quadlet units in `deploy/quadlet/` bring the stack up on boot:

```sh
# edit HOST_ADDR / passwords in the unit files first
sudo cp deploy/quadlet/*.container /etc/containers/systemd/
sudo systemctl daemon-reload
sudo systemctl start withcache.service bty-web.service   # + bty-tftp.service for TFTP
```

`AutoUpdate=registry` lets `podman auto-update` (enable
`podman-auto-update.timer`) pull new images and restart the services.

## Notes

- **Bootfiles** live in the shared `bty-tftproot` volume: the tftp sidecar seeds
  it with stock iPXE NBPs on first run, and bty (or the operator) can place
  custom bootfiles in the same volume to take precedence.
- **Consuming the cache from code:** bty uses the stdlib `withcache.client`
  library to build cache URLs and probe the cache. The bty-web image installs
  `withcache` from PyPI.
