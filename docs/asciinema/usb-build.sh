#!/usr/bin/env bash
# Canned demo: building the bty USB live image.
#
# Run under asciinema:
#   asciinema rec --cols 100 --rows 28 \
#       --title "bty: USB build walkthrough" \
#       -c './usb-build.sh' usb-build.cast
#
# This script PRINTS the commands a viewer should imagine typing,
# pauses for readability, and then runs them. The build itself takes
# ~20 minutes; the script doesn't fast-forward through that, so the
# resulting cast is real-time. If you want a shorter cast, post-edit
# with ``asciinema-edit`` or record only the start + end of the
# build.

set -eu

# Print + execute helper. Renders the prompt, pauses, "types" the
# command at a typing-cadence, then runs it. Tweaked for readable
# pacing.
prompt() { printf '\033[1;32m$\033[0m %s\n' "$*"; sleep 0.3; eval "$*"; sleep 0.6; }

clear

cat <<'BANNER'

  ┌────────────────────────────────────────────────────────────┐
  │                                                            │
  │   bty USB live image - build walkthrough                   │
  │                                                            │
  │   Builds ~/system_imaging/disk/bty-usb-x86_64.img.zst,     │
  │   the file you'll write to a USB stick.                    │
  │                                                            │
  │   Wall clock: ~20 minutes (KVM-accelerated cloud-init      │
  │   in QEMU).                                                │
  │                                                            │
  └────────────────────────────────────────────────────────────┘

BANNER
sleep 2

prompt 'cd ~/git/bty'

prompt 'make media-deps      # one-time: pipx install cijoe'

prompt 'make build VARIANT=usb'

prompt 'ls -lh ~/system_imaging/disk/bty-usb-x86_64.img.zst'

cat <<'TAIL'

  Image is ready. The .img.zst goes onto a USB stick:

      zstd -d --stdout ~/system_imaging/disk/bty-usb-x86_64.img.zst | \
          sudo dd of=/dev/sdX bs=4M status=progress conv=fsync

  Then drop your system images into the BTY_IMAGES partition and
  boot a target machine from the stick.

TAIL
sleep 3
