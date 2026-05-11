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
| `bty-mascot.png`              | downscale of ``docs/src/_static/bty-mascot.png`` (``magick ... -resize 384x384``)            | 384px   |
| `bty-favicon.png`             | downscale of ``docs/src/_static/bty-mascot.png`` (``magick ... -resize 64x64``)              | 64px    |

All served by FastAPI under ``/static/`` at runtime. Hatchling includes
this directory in the wheel automatically (no special config needed -
all files under ``src/bty/`` ship by default).

**Caveat:** the bundled Sandstone CSS starts with an
``@import url(https://fonts.googleapis.com/css2?family=Roboto:wght@400;500;700&display=swap)``
declaration. Browsers on networks with internet access will pull
Roboto from Google Fonts; air-gapped browsers fall back to the
system sans-serif. The appliance itself never reaches out -- only
the operator's browser does, and only for the font.

## Refreshing

When bumping versions, re-download with the URLs above (e.g.
``curl -sSfL <url> -o <filename>`` from this directory), bump the
version row in this table, run the test suite, and commit.

We deliberately do **not** auto-fetch at build time: pinned, committed
assets keep the build reproducible and let air-gapped contributors
build the project without internet access.
