#!/bin/sh
# Seed the shared bootfiles volume with stock iPXE NBPs on first run, then serve
# it over TFTP in the foreground. Seeding runs only on an empty volume, so it
# preserves any custom bootfiles bty (or the operator) has already written.
set -eu

ROOT=/tftproot
mkdir -p "$ROOT"

if [ -z "$(ls -A "$ROOT" 2>/dev/null)" ]; then
    echo "tftp: seeding $ROOT with stock iPXE NBPs"
    cp -a /opt/ipxe/. "$ROOT"/ 2>/dev/null || true
fi

# --foreground keeps the container's main process alive; --secure restricts
# clients to $ROOT. Host-networked, so it binds the host's udp/69.
exec in.tftpd --foreground --address 0.0.0.0:69 --secure "$ROOT"
