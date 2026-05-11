#!/usr/bin/env bash
# Canned demo: scripted flash via the bty CLI on the live env.
#
# Run on the target machine after booting the USB live env:
#
#   asciinema rec --cols 100 --rows 28 \
#       --title "bty: CLI flash walkthrough" \
#       -c './usb-flash-cli.sh' usb-flash-cli.cast
#
# The script PRINTS each command, pauses for the viewer to read,
# then runs it. Real outputs are captured (block devices on this
# host, the contents of BTY_IMAGES, the qcow2 metadata, the flash
# progress). Adjust the IMAGE / TARGET vars below to match the box
# you're recording on.

set -eu

IMAGE="${IMAGE:-/mnt/BTY_IMAGES/my-image.img.gz}"
TARGET="${TARGET:-/dev/sda}"
IMAGE_ROOT="${IMAGE_ROOT:-/mnt/BTY_IMAGES}"

prompt() { printf '\033[1;32m$\033[0m %s\n' "$*"; sleep 0.4; eval "$*"; sleep 1; }

clear

cat <<BANNER

  ┌────────────────────────────────────────────────────────────┐
  │                                                            │
  │   bty CLI - scripted flash walkthrough                     │
  │                                                            │
  │   Image:  $IMAGE
  │   Target: $TARGET
  │                                                            │
  │   1. List disks + images                                   │
  │   2. Inspect the image                                     │
  │   3. Dry-run the flash plan                                │
  │   4. Flash for real                                        │
  │                                                            │
  └────────────────────────────────────────────────────────────┘

BANNER
sleep 2

prompt "lsblk -d -e7"

prompt "bty images --image-root '$IMAGE_ROOT'"

prompt "bty inspect '$IMAGE'"

prompt "bty flash '$IMAGE' '$TARGET' --dry-run"

cat <<'GAP'

  Plan looks right. The next command is destructive - it overwrites
  the target disk. The ``--yes`` flag is bty's explicit consent
  token; without it the command refuses. (Press Ctrl-C in the
  recording if you want to bail out here.)

GAP
sleep 4

prompt "sudo bty flash '$IMAGE' '$TARGET' --yes"

cat <<'TAIL'

  Done. ``bty flash`` writes the image bytes, syncs, and re-reads
  the partition table. Reboot the target without the USB stick to
  boot into the freshly-flashed disk.

TAIL
sleep 3
