# Deploy bty + withcache on a host

bty's recommended deploy is two containers on a podman/docker host: `bty-web`
(policy + PXE + UI) and [`withcache`](https://github.com/safl/withcache) (the
URL-keyed image cache). An optional `bty-tftp` sidecar serves the iPXE NBP
over TFTP for BIOS-PXE clients (UEFI HTTP-Boot does not need it).

## Quickstart -- `uvx bty-lab deploy`

No clone required. With `uv` (or `pipx`) on the host:

```sh
sudo mkdir -p /opt/bty && sudo chown "$USER:$USER" /opt/bty
uvx bty-lab deploy /opt/bty
#   bty-web:   http://<host>:8080/ui     (login: bty / bty)
#   withcache: http://<host>:3000/       (login: bty / bty)
```

`deploy` writes compose.yml + an auto-filled `envvars` (HOST_ADDR
detected from the host's outbound-route IP; admin passwords default to
`bty`; session secret stays random) and runs `podman compose --profile
tftp pull` + `up -d`. Change the passwords in `/opt/bty/envvars` before
exposing past trusted LAN.

`bty-web` reads `$BTY_WITHCACHE_URL` from the compose file at boot and
auto-wires withcache as its image source -- no UI configuration step on
first boot.

## Three subcommands

- **`bty-lab init [DEST]`** -- emit compose.yml + `envvars.example` +
  README only. No side effects: no envvars filled, no pulls, no service
  starts. Use this when you want to inspect / customise before applying.
- **`bty-lab deploy [DEST]`** -- emit files + auto-fill envvars + bring
  up the stack via `podman compose --profile tftp up -d`. Pass
  `--systemd` to also install Podman Quadlet units to
  `/etc/containers/systemd/` and start them via systemctl (requires
  root). `-f` / `--force` overwrites existing files; it does NOT bypass
  missing prereqs.
- **`bty-lab upgrade [DEST]`** -- re-emit compose against this CLI's
  bty version (image-tag pin moves forward), preserve `envvars` + `data/`,
  `podman compose pull`, then restart. Auto-detects a Quadlet-managed
  stack (units present under `/etc/containers/systemd/`) and uses
  `systemctl daemon-reload` + `restart` in that case.

## Where the state lives

The generated compose uses host bind-mounts so the operator can see exactly
where state goes:

- `./data/bty/`       -- bty-web's `/var/lib/bty` (state.db, image cache,
  backups).
- `./data/withcache/` -- withcache's `/data` (cached image blobs).

Put state on a dedicated disk by passing `--data-dir /srv/bty/data` to `init`
(or by setting `BTY_HOST_DATA_DIR=/srv/bty/data` in `envvars`). Migrating to a
different host = copy these two directories + the `envvars` + `compose.yml`.

## Auto-start on boot (systemd via Quadlet)

`bty-lab deploy --systemd` is the one-shot path -- it writes the Quadlet
units to the deploy dir, copies them to `/etc/containers/systemd/`, runs
`daemon-reload`, and starts the services:

```sh
sudo mkdir -p /opt/bty && sudo chown "$USER:$USER" /opt/bty
sudo uvx bty-lab deploy /opt/bty --systemd
```

The Quadlet units bake `data/` as an absolute path (systemd does not run in
the operator's cwd). `AutoUpdate=registry` + `podman-auto-update.timer`
pull new images and restart services in place. To upgrade against a newer
bty release in one shot:

```sh
sudo uvx bty-lab upgrade /opt/bty
```

If you'd rather inspect the units before installing, `bty-lab init
--systemd` emits them under `<DEST>/quadlet/` and you can `sudo cp`,
`daemon-reload`, and `systemctl start` manually.

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
