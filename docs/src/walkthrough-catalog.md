# Walkthrough: image catalog

The image catalog is bty-web's only source of truth for "what can the
fleet flash". Each entry is a URL (`oras://` or `https://`) plus
optional metadata: a `sha256` (when published alongside the image), a
human display name, a format, a size hint, a one-line description.

v0.40+: bty-web doesn't own image bytes. The live env fetches the
catalog entry's URL at flash time -- through
[withcache](https://github.com/safl/withcache) when the cache is warm,
direct from the upstream origin when it isn't. There is no
`BTY_IMAGE_ROOT`, no dir-scan, no on-disk cache under bty-web's state
dir. One rule: bty has catalogs; withcache has bytes.

## Catalog identity: `ref` vs `sha256`

Two distinct identifiers on every catalog entry:

- `ref` = `sha256(canonicalise_src(src))`. A 64-hex digest of the
  canonical URL. **Always present** (it's pure math on the URL).
  This is what machine bindings target -- a rolling oras tag's ref
  stays stable across re-pushes, so binding to the tag survives the
  next rebuild upstream.
- `sha256` = the **observed content** hash of the image bytes.
  Optional. Rolling-tag entries (`oras://...:latest`,
  `releases/latest/download/...`) have no stable content sha at
  catalog-publish time and carry `sha256 = None`. Pinned entries
  carry the digest the publisher computed.

The merge collapses entries by `ref`: two manifest entries with the
same canonical src are one row. Same content under multiple refs
(operator catalogs the same image as both `oras://a` and
`https://b`) renders as two rows -- different provenance, even if
the bytes match.

## Three ways to add an entry

All paths write rows to the `catalog_entries` table in `state.db`.
None of them puts bytes anywhere on bty-web's filesystem.

1. **Upload a `catalog.toml`** -- `POST /ui/catalog/upload`. Use this
   when you have a curated multi-entry manifest. bty-web parses the
   TOML, imports every row, then replaces `BTY_PATHS_CATALOG_FILE` so the
   manifest survives restarts.

2. **Fetch from release** -- `POST /ui/catalog/fetch-release`. Pulls
   `releases/latest/download/catalog.toml` from
   `BTY_BOOT_RELEASE_REPO` (default `safl/bty`) and imports it. This
   is the "load the default nosi catalog" button.

3. **Add by URL** -- `POST /catalog/entries`. Body:
   `{"image_url": "...", "sha_url": "..." | null}`. For an ad-hoc
   image not in a curated catalog: host it on whatever HTTP server
   you have (nginx / GHCR via oras / S3) and add the URL. If the
   upstream publishes a `.sha256` sidecar, point `sha_url` at it;
   bty-web fetches + parses + stores the digest.

There is **no upload form for image bytes**.

## Manifest schema

```toml
version = 1

[[images]]
name        = "nosi-debian-sysdev-x86_64.img.gz"
src         = "oras://ghcr.io/safl/nosi/debian-sysdev:latest"
sha256      = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
format      = "img.gz"             # optional: auto-detected from name
size_bytes  = 1234567890            # optional
description = "Debian sysdev image, rolling"  # optional
```

Required fields per entry: `name`, `src`. Optional: `sha256`,
`format`, `size_bytes`, `description`. `name` must be unique within
the manifest. `sha256` (when set) must be a 64-char lower-case hex
string.

Validate before deploying:

```bash
python3 -c 'import sys; from bty import catalog; catalog.load_source(sys.argv[1])' /path/to/catalog.toml
```

bty-web also parses server-side: an upload with a parse error bounces
back with the error message and does NOT replace the running
manifest.

## Browser UI

`/ui/images` renders the catalog as a flat table: name, content sha
(or "unset"), format, source (with an icon for oras vs https), and a
per-row entry-delete button. Header controls: **Fetch latest catalog**
+ **Upload catalog**. In-page sub-nav jumps to the **List** + the
recent **Activity** card.

There is no Fetch / Hash / Cache-delete column. Those buttons were
the DownloadManager + HashManager affordances; v0.40 took them out.

## CLI

The `bty` console script (the wizard) is the operator-facing flash
surface:

```bash
bty                              # interactive wizard, local image-root only
bty --catalog <URL>              # interactive wizard with the catalog pre-loaded
bty --server <X> --mac <Y>       # server-driven via GET <X>/pxe/<Y>/plan
```

`--catalog` accepts a local TOML path, an HTTP URL, an `oras://`
reference, or a bty-web instance's `/catalog.toml`. Local-only mode
(no overlay) scans `BTY_IMAGE_ROOT` (or `/var/lib/bty/images`) on
the host running `bty` -- typically the USB-stick `BTY_IMAGES`
partition or a developer's directory -- and shows whatever flashable
files are there. This is **the bty CLI's** image root, distinct from
the deleted bty-web image-store.

## HTTP API (catalog surface)

| Endpoint | Method | Purpose |
|---|---|---|
| `/images` | GET | unified catalog listing (one row per `catalog_entries` row) |
| `/catalog.toml` | GET | the unified catalog as a TOML manifest (what `bty --catalog` consumes) |
| `/catalog/entries` | GET | list operator-curated catalog entries |
| `/catalog/entries` | POST | add an entry: `{"image_url": "...", "sha_url": "..." \| null}` |
| `/catalog/entries?src=URL` | DELETE | delete an entry by its `src` URL |
| `/catalog/import` | POST | import entries from a `source=` catalog (path / URL / oras) |
| `/ui/catalog/upload` | POST | (form) upload a `catalog.toml` multipart |
| `/ui/catalog/fetch-release` | POST | (form) pull the default catalog from `BTY_BOOT_RELEASE_REPO` |

All endpoints are auth-gated (the same session cookie as the
browser UI).

The byte-handling endpoints (`/catalog/downloads`, `/catalog/hashes`,
`/catalog/cache/{name}`, `PUT /images/{name}`) are gone in v0.40.
Image bytes are withcache's domain; the live env flashes from
whatever URL the plan endpoint hands it.

## Environment variables

| Var | Default | Purpose |
|---|---|---|
| `BTY_PATHS_STATE_DIR` | `/var/lib/bty` | state directory (state.db, catalogs, session-secret) |
| `BTY_PATHS_CATALOG_FILE` | `${BTY_PATHS_STATE_DIR}/catalog.toml` | catalog manifest path |
| `BTY_BOOT_RELEASE_REPO` | `safl/bty` | GitHub repo the "Fetch latest" button pulls from |

## Where the bytes actually live

| File / dir | Owner | Purpose |
|---|---|---|
| `state.db:catalog_entries` | bty-web | the catalog (rows) |
| `${BTY_PATHS_STATE_DIR}/catalog.toml` | bty-web | the operator-uploaded manifest (re-importable) |
| withcache's data dir | withcache | cached image blobs, keyed by origin URL |
| upstream origin | publisher | the source of truth when withcache is cold |

bty-web stores no image bytes; withcache holds the cache; the
upstream origin is the canonical source. See
[walkthrough-image-store.md](walkthrough-image-store.md) for the
full picture.
