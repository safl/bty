# Walkthrough: image catalog

The image catalog is what tells the fleet "what can I flash". Each
entry is a URL (`oras://` or `https://`) plus optional metadata: a
`sha256` (when published alongside the image), a human display
name, a format, a size hint, a one-line description.

Since v0.66.0 bty-web does **not** own the catalog. Withcache does.
Bty-web reads it from `<withcache>/catalog` (JSON envelope) through
the in-process `WithcacheCatalog` snapshot and refreshes it on
demand. Operators add + download entries on the withcache UI; bty
sees only entries whose bytes are already cached (withcache 0.11+
filters `GET /catalog` to downloaded rows). Bty owns machine
bindings (which MAC gets which catalog entry) and the flash / boot
plan; withcache owns the bytes.

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

When an entry has a `sha256` (or the source is an `oras://` ref, whose
layer digest is resolved at flash time), bty **verifies the streamed
bytes against it during the flash** and aborts with an error on
mismatch, so a corrupted or tampered download never silently lands on a
disk. The hash is computed in the pipe (`curl | tee | sha256sum | dd`),
adding no measurable overhead. Entries with no known sha flash without
verification. For PXE flashes the server passes the content sha to the
live env as `disk_image_sha` in the boot plan, so verification holds
even when the image is served from a cache or direct origin whose URL
doesn't carry the digest.

The merge collapses entries by `ref`: two manifest entries with the
same canonical src are one row. Same content under multiple refs
(operator catalogs the same image as both `oras://a` and
`https://b`) renders as two rows -- different provenance, even if
the bytes match.

## Adding entries (on withcache)

Since v0.66.0 the catalog lives on withcache; bty-web has no add /
upload / fetch forms of its own. Open the withcache UI (per your
deploy, typically `http://<host>:8081/ui/catalog`) and use the
subnav's three inline actions:

1. **Add ORAS** — paste an `oras://ghcr.io/owner/repo:tag`
   reference. Withcache resolves the manifest and prefills format /
   size / arch from the layer annotations.
2. **Add HTTPS** — paste a plain `https://` URL. Withcache derives
   the entry name from the URL basename and infers the format from
   the file suffix. sha256 + size land when Download completes.
3. **Fetch default** — re-parse the currently-configured catalog
   source (Settings > Catalog source) and merge its entries in.

Then click Download on each row you want bty to see. The
trio-facing `GET /catalog` API filters to downloaded rows only;
bty's next `WithcacheCatalog.refresh()` pulls them in.

There is **no upload form for image bytes** on either side.

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

## Browser UI

Catalog inspection + mutation moved to withcache in v0.66.0.
Bty-web's `/ui/machines/{mac}` image picker lists the current
snapshot (backed by `WithcacheCatalog`). To add or delete
entries, open the withcache UI at `<BTY_WITHCACHE_URL>/ui/catalog`
and use its Add ORAS / Add HTTPS / Delete controls. Bty picks up
the new state on the next `WithcacheCatalog.refresh()`.

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

Since v0.66.0 bty-web has no add / delete / import routes for
catalog entries. All catalog mutation lives on withcache. Bty-web
serves the read side needed by the live env + operator UI:

| Endpoint | Method | Purpose |
|---|---|---|
| `/images` | GET | unified listing of bindable images (backed by `WithcacheCatalog.entries`, filtered to downloaded rows) |
| `POST /admin/withcache/refresh` | POST | force `WithcacheCatalog.refresh()` -- pulls the current withcache `/catalog` again |

All endpoints are auth-gated (the same session cookie as the
browser UI). Add + Download + Delete happen on the withcache UI;
open Settings > Upstream to see the current URL bty-web reads from.

## Where things live

| File / dir | Owner | Purpose |
|---|---|---|
| withcache's `state.db:blobs` + `blobs/` dir | withcache | cached image bytes, keyed by origin URL |
| withcache's `state.db:events` | withcache | audit trail for catalog / download activity |
| bty-web's `state.db:machines` | bty | MAC bindings + boot mode + last-seen state |
| bty-web's `state.db:events` | bty | audit trail for machine / netboot / settings activity |
| upstream origin | publisher | canonical source when withcache is cold |

Bty-web stores no image bytes; withcache holds them. See
[walkthrough-image-store.md](walkthrough-image-store.md) for the
full picture.

## Environment variables

| Var | Default | Purpose |
|---|---|---|
| `BTY_PATHS_STATE_DIR` | `/var/lib/bty` | state directory (state.db, session-secret) |
| `BTY_WITHCACHE_URL` | (unset) | withcache HTTP endpoint the `WithcacheCatalog` reads from |
| `BTY_BOOT_RELEASE_REPO` | `safl/bty` | GitHub repo the "Fetch netboot artifacts" button pulls kernel/initrd/squashfs from |
