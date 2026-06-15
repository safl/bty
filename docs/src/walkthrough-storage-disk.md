# Walkthrough: dedicated drive for bty-lab storage

Put bty-lab's data (`state.db`, cached image bytes, backups) on a
separate drive so it survives an OS reflash. All of bty-web +
withcache's state lives under one parent path, set via
`BTY_HOST_DATA_DIR`.

## 1. Pick the drive

```bash
lsblk -o NAME,SIZE,MODEL,SERIAL,FSTYPE,MOUNTPOINT
```

Pick an **empty** target. `/dev/sdX` stands in below; replace with
your device.

## 2. Wipe + format ext4

```bash
sudo wipefs -a /dev/sdX
sudo mkfs.ext4 -L bty-data /dev/sdX
```

## 3. Mount via /etc/fstab

```bash
sudo blkid /dev/sdX                 # copy the UUID
sudo mkdir -p /srv/bty
```

Append one line to `/etc/fstab`:

```text
UUID=<paste-uuid>  /srv/bty  ext4  defaults,noatime,nofail  0 2
```

Then mount and verify:

```bash
sudo systemctl daemon-reload
sudo mount -a
df -h /srv/bty
```

## 4. Point bty-lab at it

```bash
sudo BTY_HOST_DATA_DIR=/srv/bty bty-lab deploy --dest /opt/bty
```

That's it. `state.db` lands under `/srv/bty/bty/`, cached image
blobs under `/srv/bty/withcache/`. Reinstall the OS, re-run the
same command, you're back online against the same fleet.

## Migrating an existing deploy

If you already deployed with the default `./data`:

```bash
cd /opt/bty && sudo podman compose down
sudo mv data/bty data/withcache /srv/bty/
sudo sed -i 's|^# BTY_HOST_DATA_DIR=.*|BTY_HOST_DATA_DIR=/srv/bty|' envvars
sudo bty-lab upgrade --dest /opt/bty
```

## Related

- [Operations](operations.md) -- backup, upgrade, migrate.
- [Persistent state and where image bytes live](walkthrough-image-store.md)
  -- what lives where, and why.
