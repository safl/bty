# Walkthrough: image catalog (SHA-keyed, manifest + cache)

bty's image catalog is **content-addressed** -- every image is
identified by its SHA-256 hash, with one or more friendly names
attached for display. Two sources feed the catalog:

1. **Directory scan**: files under `BTY_IMAGE_ROOT` (default
   `/var/lib/bty/images`). A `<file>.sha256` sidecar carries the
   hash; `bty-web` (or `bty` when it touches an unhashed image)
   computes and writes one on first access.
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

Validate before deploying via the Python API (``bty.catalog.
load_source`` raises ``CatalogError`` on parse / schema failure):

```bash
python3 -c 'import sys; from bty import catalog; catalog.load_source(sys.argv[1])' /path/to/catalog.toml
```

bty-web also parses the catalog server-side -- uploading via
`/ui/images?section=upload-catalog` (or `POST /catalog/import?
source=...`) bounces back with the parse error on a bad catalog
without clobbering the running one.

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

## CLI: the wizard is the operator surface

v0.22.11+ collapsed the historical `bty inspect` / `bty flash` /
`bty images` / `bty catalog` subcommands into the single
``bty`` console script (the wizard). Three invocation shapes:

```bash
bty                              # interactive wizard, local image-root only
bty --catalog <URL>              # interactive wizard with the catalog pre-loaded
bty --server <X> --mac <Y>       # server-driven via GET <X>/pxe/<Y>/plan
```

The wizard's catalog overlay accepts the same shapes the old
``--catalog`` accepted: a local TOML path, an HTTP URL, an
``oras://`` reference, or a bty-web instance's `/catalog.toml`.
Local-only mode (no overlay) scans `BTY_IMAGE_ROOT` (or
`/var/lib/bty/images`) and shows whatever flashable files are
there.

### .bri descriptors (per-stick remote-image pointers)

A ``.bri`` is a tiny TOML pointer at a remote image. Drop one
into BTY_IMAGES (or, on a Ventoy / IP-KVM delivery, at the
surrounding stick's partition root or in a ``bty-images/``
subfolder there) alongside your local files and it shows up in
the wizard's image list next to them, with ``source = remote``
and the upstream URL. Picking it kicks off the URL flash path.

```toml
# example .bri shape
url = "https://my.example.com/images/debian-13-server.img.gz"
# Optional: name, format, size_bytes, sha256, description
```

The ``url`` field also accepts an ``oras://`` reference pointing
at an OCI artefact published via [ORAS](https://oras.land/) (OCI
Registry As Storage -- the spec for **non-container** artefacts in
a container registry). The scheme is distinct from a ``docker pull
ghcr.io/...`` reference because nosi-style disk images are not
runnable container images; they are gzip-compressed raw disks
stored as OCI blobs:

```toml
# rolling tag, bty resolves :latest to the current layer digest at flash time
url = "oras://ghcr.io/safl/nosi/debian-sysdev:latest"

# digest-pinned: same blob forever, no manifest fetch
url = "oras://ghcr.io/safl/nosi/debian-sysdev@sha256:94e6..."
```

Any OCI v2 registry following the GHCR anonymous-pull convention
works (``oras://quay.io/...``, ``oras://registry.example.com:5000/...``);
GHCR is the one exercised in the starter set. Fresh USB sticks
ship with four such .bri files pre-staged on the BTY_IMAGES
partition (three nosi sysdev images plus the bty-server appliance).

That's deliberate: the catalog story is a **server** concern.
Operators who want the unified catalog (manifest + dir-scan +
auto-imported sidecars) interact with it through bty-web -- in
the browser, or via ``bty --catalog SOURCE`` which consumes
``GET /catalog.toml`` and gets a single ``src`` per entry that
the client just flashes from. No client-side resolution logic.

Server-side catalog management lives in `/ui/images` (sub-nav:
List / Fetch catalog / Upload catalog / Upload image / Upload
image (from URL)) and the HTTP API below.

## HTTP API

| Endpoint | Method | Purpose |
|---|---|---|
| `/images` | GET | unified catalog listing (dir-scan + catalog entries, SHA-keyed) |
| `/catalog.toml` | GET | the unified catalog rendered back as a TOML catalog (what `bty --catalog` consumes) |
| `/catalog/entries` | GET | list operator-curated catalog entries |
| `/catalog/entries` | POST | add an entry: `{"image_url": "...", "sha_url": "..." \| null}` |
| `/catalog/entries?src=URL` | DELETE | delete an entry by its `src` URL |
| `/catalog/import` | POST | import entries from a `source=` catalog (path / URL / oras) |
| `/catalog/downloads` | GET | list active + recent fetches |
| `/catalog/downloads` | POST | enqueue: `{"name": "..."}` |
| `/catalog/downloads/{name}` | DELETE | cancel |
| `/catalog/cache/{name}` | DELETE | evict the cached bytes for an entry (keeps the entry's metadata) |
| `/catalog/hashes` | GET | list active + recent hashes |
| `/catalog/hashes` | POST | enqueue: `{"name": "..."}` |
| `/catalog/hashes/{name}` | DELETE | cancel |

All endpoints are auth-gated (the same session cookie as the
browser UI).

## Environment variables

| Var | Default | Purpose |
|---|---|---|
| `BTY_IMAGE_ROOT` | `/var/lib/bty/images` | dir-scan source |
| `BTY_STATE_DIR` | `/var/lib/bty` | base for catalog + cache + state.db |
| `BTY_CATALOG_FILE` | `${BTY_STATE_DIR}/catalog.toml` | catalog file path |
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

