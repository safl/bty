# bty-lab server setup

The network-flash flow needs a long-running `bty-web` container on
a Linux host: it serves the browser UI, the per-MAC PXE plans, the
iPXE bootfile, and proxies image bytes through a `withcache`
sidecar. `bty-lab deploy` writes the compose stack, brings it up,
and (when run as root) installs Podman Quadlet units so the stack
auto-starts on boot.

## Prerequisites

| You need | Notes |
|---|---|
| A **Linux host** with root + `podman` | A small lab box or a VM is fine. Stack idles at ~150 MiB RAM. |
| `pipx` (or `uvx`) | To run `bty-lab` without a global pip install. |
| A **LAN DHCP server** | bty runs no DHCP role; the operator's existing DHCP is the prerequisite for PXE clients to find the bty host (see [bty via netboot](bty-netboot-pc.md) for DHCP wiring). |

## Step 1: Pick a storage mode

The deploy keeps all mutable state under one parent path (default
`./data` next to `compose.yml`). The path defaults work for most
hosts; for larger fleets put state on a dedicated drive so it
survives an OS reflash.

### Mode A: single disk (everything on the system disk)

Nothing to do here. Skip to Step 2; the default `--data-dir`
puts state next to `compose.yml`.

### Mode B: dedicated drive for bty state

```bash
# Pick the target drive (must be empty); replace /dev/sdX.
lsblk -o NAME,SIZE,MODEL,SERIAL,FSTYPE,MOUNTPOINT
sudo wipefs -a /dev/sdX
sudo mkfs.ext4 -L bty-data /dev/sdX

# Mount it at /srv/bty.
sudo mkdir -p /srv/bty
UUID=$(sudo blkid -o value -s UUID /dev/sdX)
echo "UUID=$UUID  /srv/bty  ext4  defaults,noatime,nofail  0 2" | sudo tee -a /etc/fstab
sudo systemctl daemon-reload
sudo mount -a
df -h /srv/bty
```

`/srv/bty` will hold both `bty/` (state.db, backups, netboot
artifacts) and `withcache/` (cached image blobs) -- one drive,
one mount, one path to back up.

## Step 2: Deploy

```bash
# Mode A: default ./data next to compose.yml
sudo uvx bty-lab deploy /opt/bty

# Mode B: state on the dedicated drive
sudo uvx bty-lab deploy /opt/bty --data-dir /srv/bty
```

`deploy` writes `compose.yml` + `envvars` under `/opt/bty/`,
installs Podman Quadlet units to `/etc/containers/systemd/`,
and starts the services via `systemctl`. Stack survives host
reboots.

End-of-deploy output points at the two URLs:

```text
bty:       http://<host>:8080/ui     (login: bty-lab / bty-lab)
withcache: http://<host>:8081/       (login: bty-lab / bty-lab)
```

Change the passwords in `/opt/bty/envvars` before exposing past
a trusted LAN.

## Step 3: Verify

```bash
curl -fsS http://localhost:8080/healthz   # bty-web up
sudo systemctl status bty-web withcache    # systemd green
```

Open `http://<host>:8080/ui` in a browser, log in with
`bty-lab` / `bty-lab`. The dashboard shows machine + image
counts (both zero on a fresh deploy).

## Step 4: Load the default catalog

From the **Images** page click **Fetch latest catalog**. That
pulls nosi's published catalog (rolling oras tags for the bty +
nosi images) into bty-web's `catalog_entries`. PXE clients will
then see the same image list the USB flow exposes.

## Day 2

| Task | Command |
|---|---|
| Upgrade in place | `sudo uvx bty-lab upgrade /opt/bty` |
| Back up state | The `/ui/backups` page (or `bty-web export` for a portable bundle) |
| Move to a new host | `bty-web export` + `bty-web import` -- see [Operations](../operations.md) |
| Inspect raw files | `ls /opt/bty/data/` (Mode A) or `ls /srv/bty/` (Mode B) |

## Related

- [bty via netboot](bty-netboot-pc.md) -- use the deployed server
  to PXE-flash a fleet.
- [Persistent state and where image bytes live](../walkthrough-image-store.md)
  -- the storage model in detail (what state.db holds, what
  withcache holds, why bty-web is out of the bytes plane).
- [Operations](../operations.md) -- backup, upgrade, migrate.
