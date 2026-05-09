#!/bin/sh
# bty-web container entrypoint.
#
# Three responsibilities:
#   1. Make sure the state + image directories exist (a fresh
#      volume mount is empty; bty-web's own ``mkdir -p`` would
#      handle it but the image dir wants to be there before
#      bty-web tries to list it).
#   2. Print a loud banner with the default credentials so an
#      operator never accidentally exposes ``bty/bty`` past a
#      trusted LAN. ``BTY_QUIET=1`` suppresses (CI / automation).
#   3. ``exec bty-web`` so PID 1 (tini) wraps the actual server
#      process directly -- no extra shell layer to fight with on
#      ``docker stop``.

set -eu

mkdir -p "${BTY_STATE_DIR:-/var/lib/bty}" \
         "${BTY_IMAGE_ROOT:-/var/lib/bty/images}"

if [ -z "${BTY_QUIET:-}" ]; then
    cat >&2 <<EOF
========================================================================
  bty-web container -- listening on :${BTY_WEB_PORT:-8080}

  Default credentials: bty / bty
  -> ROTATE before exposing past a trusted LAN:
       docker exec -it <container> passwd bty

  Image catalog: ${BTY_IMAGE_ROOT:-/var/lib/bty/images}
  State dir:     ${BTY_STATE_DIR:-/var/lib/bty}
  Browser UI:    http://<host>:${BTY_WEB_PORT:-8080}/ui

  Connect bty-tui clients with:
       bty-tui --server http://<host>:${BTY_WEB_PORT:-8080}

  No dnsmasq / TFTP / PXE proxy-DHCP in this container -- those
  need bare-metal LAN access. Use the bty-server appliance for
  the full PXE flow (docs/walkthrough-server.md).
========================================================================
EOF
fi

exec bty-web
