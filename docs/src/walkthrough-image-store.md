# Walkthrough: persistent state and the image store

bty-web keeps all of its mutable state under `/var/lib/bty`:

- `images/` -- the image store (`BTY_IMAGE_ROOT`): operator-typed images
  alongside catalog-fetched files named `catalog-<ref:12>-<slug>.<ext>`
  (v0.31.0+).
- `boot/` -- the netboot artifacts (`BTY_BOOT_DIR`: kernel / initrd /
  squashfs).
- `state.db` -- bty-web's SQLite database (machine inventory, catalog
  metadata, boot-mode assignments, audit log).
- `session-secret` -- the cookie-signing key.

## Where it lives in the container deploy

The container deploy backs `/var/lib/bty` with a host bind-mount at
`./data/bty/` next to the generated `compose.yml` (or the absolute path
you pass to `bty-lab init --data-dir`). The directory is the unit of
persistence: it outlives the container, so `podman compose pull` +
`up -d` upgrades bty-web to a new image while the image store, netboot
artifacts, and machine inventory stay put. See
[`deploy/README.md`](https://github.com/safl/bty/blob/main/deploy/README.md)
for the layout and the upgrade flow, and
[bty via netboot](tutorials/bty-netboot.md) for standing the stack up.

The image bytes themselves are delegated to
[withcache](https://github.com/safl/withcache): bty prefers the cache as
the image source for artifacts it holds, so a fleet pulls each image once.
withcache keeps its blobs in `./data/withcache/` next to bty's state.

## Adding images

Drop images into the store the same way regardless of deploy:

- **Operator-typed files** keep whatever filename you upload / scp / drop
  in (e.g. `my-fedora.qcow2`, `debian-13-server.img.gz`).
- **Catalog-fetched files** land under a URL-derived name,
  `catalog-<ref:12>-<slug>.<ext>`, fetched by the `/ui/images` Fetch
  button or `POST /catalog/downloads`.

`ls /var/lib/bty/images/ | grep '^catalog-'` lists every catalog-fetched
file; the rest are operator-typed.

## Backups

A minimal backup is `state.db`; a full backup is the whole
`/var/lib/bty` tree (or the `bty-data` volume). The `/ui/backups` page
drives scheduled and on-demand backups, and `bty-web export` / `import`
move the operator-owned half of the state (machine identities, bindings,
catalog, image files) to a new host. See
[operations.md](operations.md) for backup, upgrade, and migrate
procedures.
