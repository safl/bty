# bty.web static assets

Vendored client-side assets so the bty-web appliance does **not** need
internet access at runtime. The bty server image is intended to live on
homelab / CI networks; pulling resources from a CDN every page load
would defeat the offline-friendly design.

## What is here

| File                          | Source                                                                                       | Version |
|-------------------------------|----------------------------------------------------------------------------------------------|---------|
| `bootstrap.min.css`           | Bootswatch *Sandstone* themed Bootstrap, <https://cdn.jsdelivr.net/npm/bootswatch@5.3.3/dist/sandstone/bootstrap.min.css> | 5.3.3   |
| `bootstrap-icons.min.css`     | <https://cdn.jsdelivr.net/npm/bootstrap-icons@1.13.1/font/bootstrap-icons.min.css>           | 1.13.1  |
| `fonts/bootstrap-icons.woff2` | <https://cdn.jsdelivr.net/npm/bootstrap-icons@1.13.1/font/fonts/bootstrap-icons.woff2>       | 1.13.1  |
| `fonts/bootstrap-icons.woff`  | <https://cdn.jsdelivr.net/npm/bootstrap-icons@1.13.1/font/fonts/bootstrap-icons.woff>        | 1.13.1  |
| `htmx.min.js`                 | <https://cdn.jsdelivr.net/npm/htmx.org@2.0.4/dist/htmx.min.js>                               | 2.0.4   |
| `sse.js`                      | <https://cdn.jsdelivr.net/npm/htmx-ext-sse@2.2.3/sse.js>                                     | 2.2.3   |
| `bty-mascot.png`              | square mascot art (2000x2000 in repo; rendered scaled in CSS).                               | 2000px  |
| `bty-favicon.png`             | wider variant used in the navbar brand pill; aspect ratio is not 1:1 -- the navbar CSS sets ``width: auto`` so the aspect is preserved when the mascot or favicon art is refreshed. | 1017x698 |

All served by FastAPI under ``/static/`` at runtime. Hatchling includes
this directory in the wheel automatically (no special config needed -
all files under ``src/bty/`` ship by default).

**Strict no-CDN guarantee.** Neither the appliance nor the operator's
browser fetches anything from a third-party origin while using
bty-web. Specifically, the upstream Bootswatch Sandstone CSS ships
with an ``@import url(https://fonts.googleapis.com/...)`` declaration
for Roboto at the very top of the file; we **strip that line** when
vendoring so the browser falls back to the system sans-serif stack
and never reaches out to fonts.googleapis.com. The remaining ``http``
URLs in the vendored assets are all in license comments
(``getbootstrap.com``, ``bootswatch.com``, ``github.com``) or the
SVG namespace identifier (``www.w3.org/2000/svg``); none get fetched.

A regression test in ``tests/test_web_ui.py`` checks the bundled CSS
for a re-introduced ``@import url(http...)`` so a future refresh
can't quietly bring the Google Fonts call back.

## Refreshing

When bumping versions, re-download with the URLs above (e.g.
``curl -sSfL <url> -o <filename>`` from this directory), bump the
version row in this table, run the test suite, and commit. **For
``bootstrap.min.css``**: after downloading, strip the leading
``@import url(https://fonts.googleapis.com/...)`` line. The
regression test in ``tests/test_web_ui.py`` will fail if you
forget.

We deliberately do **not** auto-fetch at build time: pinned, committed
assets keep the build reproducible and let air-gapped contributors
build the project without internet access.
