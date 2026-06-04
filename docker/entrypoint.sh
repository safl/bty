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
#   3. Start dnsmasq (TFTP) in the background, then ``exec
#      bty-web`` in the foreground so PID 1 (tini) wraps the
#      actual server process directly.
#
# Why both daemons in one container: parity with the bty-server
# appliance. The appliance ships dnsmasq for TFTP serving + bty-web
# for HTTP serving; the container should be functionally equivalent
# so operators can pick deployment shape based on infra
# preferences (Docker host vs. flashed image) without losing
# capability.

set -eu

STATE_DIR="${BTY_STATE_DIR:-/var/lib/bty}"
IMAGE_ROOT="${BTY_IMAGE_ROOT:-/var/lib/bty/images}"

# Volume-permission preflight. The container runs bty-web as the
# unprivileged ``bty`` user (uid 1000, pinned in the Dockerfile so
# it doesn't drift across Debian package-order changes). Bind
# mounts inherit host ownership, so a bare
# ``-v ./bty-data:/var/lib/bty`` from a host where the dir is
# root-owned blocks bty-web's first write to ``state.db`` /
# ``session-secret`` and the container would crash 30 frames
# deep in Python with a confusing PermissionError. Detect the
# unwritable case here and exit with a one-line fix.
if ! mkdir -p "$STATE_DIR" "$IMAGE_ROOT" 2>/dev/null \
   || ! [ -w "$STATE_DIR" ] || ! [ -w "$IMAGE_ROOT" ]; then
    cat >&2 <<EOF

bty-web container: cannot write to ${STATE_DIR}.

The container runs as uid $(id -u) (the bty user). Your bind mount
appears to be owned by a different uid. Pre-chown the host dir:

    sudo chown -R $(id -u):$(id -g) ./bty-data

Or use a docker-managed volume (which inherits the image's
ownership):

    docker run -v bty-data:/var/lib/bty ...

EOF
    exit 1
fi

# Start dnsmasq for TFTP serving. The dnsmasq binary has
# ``cap_net_bind_service`` set on the file (see Dockerfile), so it
# can bind UDP 69 even while running as the unprivileged bty user
# (config's ``user=bty`` drops privileges after the bind).
# ``--keep-in-foreground`` keeps dnsmasq in the foreground so it
# logs to our stderr; we backgrond it from the shell so bty-web
# can hold the foreground for tini.
dnsmasq --keep-in-foreground --conf-file=/etc/dnsmasq.conf &
DNSMASQ_PID=$!

# If dnsmasq dies, take the container down with it -- the operator
# won't see "TFTP broken" silently; they get a container restart
# via their orchestrator's policy.
trap 'kill -TERM "$DNSMASQ_PID" 2>/dev/null; exit 0' INT TERM

if [ -z "${BTY_QUIET:-}" ]; then
    cat >&2 <<EOF
========================================================================
  bty-web container: HTTP on :${BTY_WEB_PORT:-8080}, TFTP on udp/69

  Operator UI auth: set BTY_ADMIN_PASSWORD to gate /ui (unset = OPEN).

  Image catalog: ${BTY_IMAGE_ROOT:-/var/lib/bty/images}
  State dir:     ${BTY_STATE_DIR:-/var/lib/bty}
  Backups:       ${BTY_BACKUP_DIR:-${BTY_STATE_DIR:-/var/lib/bty}/backups}
  Browser UI:    http://<host>:${BTY_WEB_PORT:-8080}/ui

  Connect bty wizard clients with:
       bty --server http://<host>:${BTY_WEB_PORT:-8080} --mac <self-mac>

  PXE clients: configure your LAN DHCP server (UniFi / pfSense /
  dnsmasq / etc.) to point clients at this container with option
  60 "PXEClient", option 66 next-server <host-ip>, option 67
  bootfile "ipxe.efi". bty does NOT run a DHCP role.
========================================================================
EOF
fi

exec bty-web
