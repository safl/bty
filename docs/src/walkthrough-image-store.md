# Walkthrough: persistent state and where image bytes live

bty-web's mutable state under `/var/lib/bty` is small and metadata-only:

- `state.db` -- SQLite database (machine inventory, catalog entries,
  boot-mode assignments, audit log, settings).
- `boot/` -- netboot artifacts (kernel / initrd / squashfs) served via
  TFTP + HTTP-Boot. `BTY_PATHS_BOOT_DIR` overrides the path.
- `catalog.toml` -- the manifest the operator uploaded (or that
  fetch-from-release pulled). Re-importable; not load-bearing once
  rows are in `catalog_entries`.
- `session-secret` -- the cookie-signing key.

There is **no image store**. v0.40 (catalogs, not bytes) moved image
bytes entirely out of bty-web. Operators add a catalog entry pointing
at a URL; the live env streams from that URL when it flashes.

## Who holds the bytes

- **withcache** (`./data/withcache/`) -- transparent HTTP cache.
  Warms on first flash of a given URL; subsequent flashes hit the
  warm cache. Backs up independently of bty-web; restore is just
  dropping its on-disk blobs back into the data dir.
- **upstream** -- the URL the catalog entry references. GHCR via
  `oras://`, GitHub release assets, an operator's own nginx, S3 --
  whatever serves HTTP(S) or the OCI registry protocol.

bty-web's plan endpoint returns one of two URLs to the live env:

1. **withcache warm** -- the `/b/<urlsafe-b64(origin)>/<basename>`
   serve URL. The HEAD bty-web does before returning the URL warms
   withcache's auto-fetch path on a miss; subsequent boots flip to
   cached.
2. **withcache cold or unconfigured** -- the origin URL itself.
   bty-web is out of the bytes path; the live env streams direct.

For `oras://` catalog entries the plan ships either the withcache
serve URL (when withcache is configured) or the raw `oras://` URL
(otherwise). withcache 0.6.0+ is oras-aware -- it parses the ref,
mints its own bearer, and absorbs ghcr.io's mid-stream cuts via
Range-resume. Without withcache, the live env's bty TUI does the
same OCI dance itself via `withcache.oras`. The v0.41-era
`/images/<ref>` stream-proxy was removed in v0.60.0 once both
backstops landed.

## Container deploy

The container deploy backs `/var/lib/bty` with a host bind-mount at
`./data/bty/` next to the generated `compose.yml`. The directory is
the unit of persistence: it outlives the container, so
`podman compose pull` + `up -d` upgrades bty-web while state.db,
catalogs, netboot artifacts, and machine inventory stay put. See
[`deploy/README.md`](https://github.com/safl/bty/blob/main/deploy/README.md)
for the layout and the upgrade flow, and
[bty via netboot](tutorials/bty-netboot-pc.md) for standing the stack up.

## Adding images

Three paths, all of which write rows to `catalog_entries`; none of
which puts bytes anywhere on bty-web's filesystem:

- **Catalog upload** -- `POST /ui/catalog/upload` of a TOML manifest.
  Use this when you have a curated catalog file (the upstream
  ``safl/nosi`` image-builder publishes one auto-generated per
  release).
- **Fetch from release** -- `POST /ui/catalog/fetch-release`.
  Pulls the default catalog from
  `https://github.com/safl/nosi/releases/latest/download/catalog.toml`.
- **Add by URL** -- `POST /catalog/entries` with `image_url` (and
  optionally `sha_url`). For an ad-hoc image that doesn't live in a
  catalog: host it on your own HTTP server / push to a registry, then
  add the URL.

There is no upload form for image bytes. There is no drop-zone
directory.

## Backups

A backup is `state.db` + the catalog files. The whole `/var/lib/bty`
tree tars cleanly. The `/ui/backups` page drives scheduled and
on-demand backups (v3 bundle, metadata-only); `bty-web export` /
`import` move the operator-owned half of the state (machines + their
hardware identity) to a new host.

Withcache is backed up independently -- its data dir is just a
directory of blobs keyed by `urlsafe-b64(origin)`. Re-deploy bty-web,
re-deploy withcache, and bty-web HEADs the catalog URLs on first
request to learn the hit/miss state of the world without any
re-download orchestration.

See [operations.md](operations.md) for backup, upgrade, and migrate
procedures.
