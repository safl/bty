# Deploy bty + withcache on a host

bty's recommended deploy is two containers on a podman/docker host: `bty-web`
(policy + PXE + UI) and [`withcache`](https://github.com/safl/withcache) (the
URL-keyed image cache). An optional `bty-tftp` sidecar serves the iPXE NBP
over TFTP for BIOS-PXE clients (UEFI HTTP-Boot does not need it).

## Quickstart -- `uvx bty-lab init`

No clone required. With `uv` (or `pipx`) on the host:

```sh
uvx bty-lab init ./bty-host                   # writes compose.yml + .env.example + README
cd bty-host
cp .env.example .env
"${EDITOR:-vi}" .env                                  # set HOST_ADDR + WITHCACHE_ADMIN_PASSWORD
podman compose up -d
#   bty-web:   http://<host>:8080/ui
#   withcache: http://<host>:3000/
```

`init` pins the `bty-web` and `bty-tftp` image tags to the bty CLI version
that ran it -- so the compose file you get and the image bytes you pull are
guaranteed to match. To upgrade, re-run `uvx bty-lab init --force .` and
`podman compose pull && podman compose up -d`. State under `data/` survives
the restart.

`bty-web` reads `$BTY_WITHCACHE_URL` (set by the compose file) on boot and
auto-wires withcache as its image source. No UI configuration is needed for
first boot; just `HOST_ADDR` + a password in `.env`.

For BIOS clients that PXE-boot via TFTP:

```sh
podman compose --profile tftp up -d
```

## Where the state lives

The generated compose uses host bind-mounts so the operator can see exactly
where state goes:

- `./data/bty/`       -- bty-web's `/var/lib/bty` (state.db, image cache,
  backups).
- `./data/withcache/` -- withcache's `/data` (cached image blobs).

Put state on a dedicated disk by passing `--data-dir /srv/bty/data` to `init`
(or by setting `BTY_HOST_DATA_DIR=/srv/bty/data` in `.env`). Migrating to a
different host = copy these two directories + the `.env` + `compose.yml`.

## Auto-start on boot (systemd via Quadlet)

`bty-lab init --systemd` additionally writes Podman Quadlet units:

```sh
uvx bty-lab init ./bty-host --systemd
cd bty-host && cp .env.example .env && "${EDITOR:-vi}" .env
sudo cp quadlet/*.container /etc/containers/systemd/
sudo systemctl daemon-reload
sudo systemctl start withcache.service bty-web.service
# Optional, for BIOS PXE clients:
sudo systemctl start bty-tftp.service
```

The Quadlet units bake `data/` as an absolute path (systemd does not run in
the operator's cwd). `AutoUpdate=registry` + `podman-auto-update.timer`
pull new images and restart services in place.

## How the caching behaves

`BTY_WITHCACHE_URL` wires bty to the cache. For an https image origin, bty
does a cheap `HEAD` to withcache: when it's cached, the boot plan points the
client at withcache; when the cache lacks it (or is unreachable), bty serves
the origin directly. The probe also warms an auto-fetch withcache -- a miss
enqueues the background fill -- so the next machine you provision hits the
cache.

## DHCP

Your LAN's DHCP server is the only piece bty does NOT run. Point it at this
host (next-server/bootfile for legacy PXE, or HTTPClient/UEFI HTTP-Boot for
modern clients). The bty-web UI's **Netboot** tab has a per-interface
cheatsheet.

## Files in this directory

- `compose.yml`        -- reference compose (operators usually consume it via
  `uvx bty-lab init`, not directly).
- `quadlet/`           -- reference Quadlet units (same: usually consumed via
  `init --systemd`).
- `tftp/`              -- Containerfile for the `bty-tftp` sidecar.
