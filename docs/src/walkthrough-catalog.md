# Walkthrough: image catalog (SHA-keyed, manifest + cache)

bty's image catalog is **content-addressed** -- every image is
identified by its SHA-256 hash, with one or more friendly names
attached for display. Two sources feed the catalog:

1. **Directory scan**: files under `BTY_IMAGE_ROOT` (default
   `/var/lib/bty/images`). A `<file>.sha256` sidecar carries the
   hash; `bty-web` (or the CLI) computes and writes one on first
   access.
2. **Manifest**: a TOML file at `BTY_CATALOG_FILE` (default
   `${BTY_STATE_DIR}/catalog.toml`) listing named entries with
   upstream `src` URLs and pinned `sha256` digests. Entries are
   fetched on demand and cached by hash under
   `${BTY_STATE_DIR}/cache/<sha256>`.

Both sources merge by SHA-256: an image present locally AND
declared in the manifest renders as a single row with both names
and both sources.

## Why a manifest

The super-catalog pattern: a single `catalog.toml` published at a
stable URL refers to artifacts spread across many GitHub releases /
S3 buckets / wherever. A fleet of `bty-web` instances pulls the
same manifest and lazily caches the blobs each one actually flashes.
Adding a new image is a manifest PR, not "copy bytes to every
server" by hand.

## Manifest schema

```toml
version = 1

[[images]]
name        = "ubuntu-server-22.04-bty.img.zst"
src         = "https://github.com/safl/bty-images/releases/download/v0.1/ubuntu-22.04.img.zst"
sha256      = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
format      = "img.zst"             # optional: auto-detected from name
size_bytes  = 1234567890            # optional
description = "Ubuntu Server 22.04, bty-tuned"  # optional

[[images]]
name   = "freebsd-14-test.img.zst"
src    = "https://example.com/freebsd-14.img.zst"
sha256 = "fedcba9876543210fedcba9876543210fedcba9876543210fedcba9876543210"
```

Required fields per entry: `name`, `src`, `sha256`. Optional:
`format`, `size_bytes`, `description`. `name` must be unique
within the manifest. SHA-256 must be a 64-char lower-case hex
string.

Validate before deploying:

```bash
bty catalog validate /path/to/catalog.toml
```

## SHA-256 sidecars for dir-scan images

Files dropped into `BTY_IMAGE_ROOT` get an associated sidecar at
`<file>.sha256` -- standard `sha256sum`-compatible format so an
operator can verify by hand:

```bash
sha256sum -c demo.img.zst.sha256
```

bty-web **auto-imports** at startup: it walks `BTY_IMAGE_ROOT`
once and enqueues a hash job for every file without a sidecar.
The HashManager runs them serially in the background (default
parallelism is **1** -- on small hardware like a Pi 4 or an old
NUC, two simultaneous SHA-256 computations saturate IO + CPU
and both finish at half speed, so serial uses the same wall
clock without tanking responsiveness; override via
`BTY_HASH_MAX_PARALLEL` on fast hosts). Until a file's sidecar
lands, the file does not appear in `/images` listings -- it
becomes flashable once imported.

Operators who drop a file *after* server startup can either:

- Restart bty-web (the next startup picks the new file up).
- Pre-compute the sidecar with `sha256sum` before dropping --
  the auto-import skips it as already-hashed:

  ```bash
  sha256sum demo.img.zst > demo.img.zst.sha256
  ```
- Hit the Hash button in the bty-web UI for an explicit
  re-trigger (useful when you want to confirm the bytes you
  copied across haven't been corrupted by the transfer).

## Browser UI

`/ui/images` is a single page with two cards:

- **Unified catalog** table: SHA prefix, names, format, sources
  (icons distinguish local file vs manifest URL), cached state,
  per-row Action button. Action shows "Hash" for unhashed
  dir-scan rows, "Fetch" for manifest entries not yet in the
  cache, or "-" for cached entries.
- **Downloads** pane + **Hashes** pane below: live progress for
  each in-flight job, with Cancel per row. Auto-refreshes every
  ~2s via polling.

When a Fetch or Hash transitions to `completed`, the page
auto-reloads (after a brief delay so the 100% bar renders) so
the catalog table picks up the new `cached` / `sha256` state
without manual refresh.

## CLI (local mode -- intentionally simple)

```bash
bty list images [--image-root PATH]
bty inspect image <path>
bty flash --image PATH_OR_URL --target /dev/sdX --yes
```

The local CLI is dir-scan only -- no manifest, no SHA, no
catalog. ``bty list images`` answers "what flashable files are
in this directory?" and stops there. ``bty flash --image``
accepts a path, an HTTP URL, or a ``.bri`` (bty Remote Image)
descriptor file.

A ``.bri`` is a tiny TOML pointer at a remote image. Drop one
into BTY_IMAGES alongside your local files and it shows up in
``bty list images`` next to them, with ``source = remote`` and
the upstream URL. ``bty flash --image foo.bri`` resolves the
descriptor and falls into the URL flash path. The bty-usb stick
bake drops a starter ``bty-server-x86_64.bri`` directly into the
BTY_IMAGES exFAT partition so an operator browsing the partition
from a host OS sees the format up front and can copy / edit /
delete it freely.

```toml
# ~/BTY_IMAGES/bty-server.bri
url = "https://github.com/safl/bty/releases/latest/download/bty-server-x86_64.img.gz"
# Optional: name, format, size_bytes, sha256, description
```

That's deliberate: the catalog story is a **server** concern.
Operators who want the unified catalog (manifest + dir-scan +
auto-imported sidecars) interact with it through bty-web -- in
the browser, or via ``bty-tui --server URL`` which consumes
``GET /images`` and gets a single ``url`` per entry that the
client just flashes from. No client-side resolution logic.

For server-side manifest management:

```bash
bty catalog validate [PATH]   # parse + schema-check a TOML manifest
bty catalog list              # show entries with cached state
bty catalog fetch <name>      # blocks while downloading; useful for
                              #   batch / scripted cache-warming
```

These run against the configured manifest path
(``BTY_CATALOG_FILE`` or ``${BTY_STATE_DIR}/catalog.toml``); they
are administrative tools, not part of the operator's flash flow.

## HTTP API

| Endpoint | Method | Purpose |
|---|---|---|
| `/catalog/downloads` | GET | list active + recent fetches |
| `/catalog/downloads` | POST | enqueue: `{"name": "..."}` |
| `/catalog/downloads/{name}` | DELETE | cancel |
| `/catalog/hashes` | GET | list active + recent hashes |
| `/catalog/hashes` | POST | enqueue: `{"name": "..."}` |
| `/catalog/hashes/{name}` | DELETE | cancel |
| `/images` | GET | unified catalog listing |

All endpoints are auth-gated (the same session cookie as the
browser UI). The schemas have been stable since v0.5.x; the
shape will be locked in for 1.0.

## Environment variables

| Var | Default | Purpose |
|---|---|---|
| `BTY_IMAGE_ROOT` | `/var/lib/bty/images` | dir-scan source |
| `BTY_STATE_DIR` | `/var/lib/bty` | base for catalog + cache + state.db |
| `BTY_CATALOG_FILE` | `${BTY_STATE_DIR}/catalog.toml` | manifest path |
| `BTY_CATALOG_CACHE_DIR` | `${BTY_STATE_DIR}/cache` | content-addressed cache |
| `BTY_CATALOG_MAX_PARALLEL` | `2` | concurrent downloads |
| `BTY_HASH_MAX_PARALLEL` | `1` | concurrent hashes (low: small hardware) |

## Cache eviction

The cache is unbounded in v1. Manual eviction:

```bash
sudo rm -rf /var/lib/bty/cache/*
```

A future release may add LRU + size-cap eviction; until then,
plan for cache size = sum of every image you've fetched since
last manual rm.

## Upgrading from pre-M22 (pre-v0.5.16)

**Breaking schema change**: `machines.image` (filename) was
replaced by `machines.image_sha256` (content hash). Existing
state.db files do not migrate automatically. Two paths:

1. **Wipe + re-bind.** Easiest for homelab / CI deployments
   where the machine list is short:

   ```bash
   sudo systemctl stop bty-web
   sudo rm /var/lib/bty/state.db
   sudo systemctl start bty-web
   ```
   Then re-add machines via the browser UI; the picker now
   binds by SHA, so each machine ends up bound to a specific
   image content rather than a filename.
2. **Hand-migrate**: open `state.db` with `sqlite3`,
   `ALTER TABLE machines RENAME COLUMN image TO image_old;
   ALTER TABLE machines ADD image_sha256 TEXT;`, populate
   `image_sha256` per row by hashing the named file, then
   `ALTER TABLE machines DROP COLUMN image_old`. Tedious;
   wipe + re-bind is usually faster.
