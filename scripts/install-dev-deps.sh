#!/usr/bin/env bash
# Install everything needed to develop, test, and build bty on a
# Debian-family host (Debian 12+, Ubuntu 24.04+).
#
# What this covers:
#   - Python toolchain (uv via pipx; cijoe via pipx)
#   - bty CLI flash dependencies (qemu-img, zstd, parted, ...)
#   - bty-web tests (no extra system tooling beyond Python deps)
#   - PXE chain test (qemu-system + KVM helpers + sshpass for debug)
#   - bty-media bake pipelines:
#       * server-x86: qemu-system + genisoimage for cloud-init seed
#       * usb-x86 / netboot-x86: live-build + debootstrap +
#                squashfs-tools + xorriso + exfatprogs (plus root for
#                the chroot phase - sudo prompts you interactively
#                when ``sudo make build VARIANT=usb-x86`` /
#                ``netboot-x86``)
#       * server-rpi: qemu-user-static + binfmt-support + xz-utils
#                for the arm64 chroot customisation of Raspberry Pi
#                OS Lite
#
# Idempotent: re-running upgrades package versions when newer ones
# are available but does no harm if everything is already installed.

set -euo pipefail

if ! command -v apt-get >/dev/null 2>&1; then
    echo "This script targets Debian / Ubuntu (apt-based)." >&2
    echo "On other distros, install equivalents of the package list" >&2
    echo "below by hand:" >&2
    echo "See the PACKAGES array later in this file for the canonical list." >&2
    exit 1
fi

# Use sudo only when not already root (e.g. in a CI container running
# as root, no sudo is needed and may not be installed).
sudo_if_needed() {
    if [ "$(id -u)" -eq 0 ]; then
        "$@"
    else
        sudo "$@"
    fi
}

PACKAGES=(
    # Core dev tooling.
    git
    make
    curl
    ca-certificates
    jq

    # Python.
    python3
    python3-venv
    pipx

    # ``bty flash`` runtime + integration tests against a real loop
    # device (gdisk / parted partitioning, dosfstools / e2fsprogs /
    # exfatprogs filesystem creation, nvme-cli for NVMe inspection,
    # zstd for ``.img.zst`` decompression, qemu-img for qcow2 ->
    # raw conversions).
    qemu-utils
    zstd
    parted
    gdisk
    dosfstools
    e2fsprogs
    exfatprogs
    nvme-cli

    # PXE chain test:
    #  - qemu-system-x86 to boot the server + client VMs
    #  - cpu-checker (kvm-ok) for the KVM sanity check
    #  - sshpass for the ad-hoc password-SSH used during debug
    #    (the chain test itself uses paramiko; sshpass is only
    #    handy when you ssh in by hand to poke a running VM)
    qemu-system-x86
    cpu-checker
    sshpass

    # bty-media server-x86 bake (cloud-init in QEMU + NoCloud cidata ISO).
    genisoimage

    # bty-media usb-x86 + netboot-x86 bakes (live-build's debootstrap
    # / chroot / squashfs / xorriso pipeline). xz-utils is also used
    # by usb_iso_build.py to compress the cooked ISO to .iso.xz.
    live-build
    debootstrap
    squashfs-tools
    xorriso
    xz-utils

    # bty-media server-rpi variant (Raspberry Pi 4/5). The build
    # mounts a Pi OS Lite arm64 image, chroots into it via
    # qemu-aarch64-static (registered transparently by
    # binfmt-support), and customises in place. xz-utils (above)
    # also handles the upstream image's .xz compression.
    qemu-user-static
    binfmt-support
)

echo "Updating apt index..."
sudo_if_needed apt-get update -y

echo "Installing ${#PACKAGES[@]} packages..."
sudo_if_needed apt-get install -y --no-install-recommends "${PACKAGES[@]}"

# pipx-installed user tools. ``|| true`` on each so a re-run that
# finds them already installed doesn't fail the script.
echo "Installing pipx tools (cijoe, uv)..."
pipx install cijoe || pipx upgrade cijoe || true
pipx install uv    || pipx upgrade uv    || true
pipx ensurepath >/dev/null 2>&1 || true

cat <<'EOF'

==============================================================
  bty dev environment ready.

  Next steps:

    uv sync --all-extras --group dev    # project venv + deps
    uv run pytest                       # unit tests (~7s)
    make ci                             # lint + types + tests
    sudo make build VARIANT=usb-x86         # USB live ISO via live-build (~15m, root)
    make build VARIANT=server-x86           # server appliance (~15m, cloud-init)
    make build VARIANT=server-rpi           # RPi 4/5 server appliance
    sudo make build VARIANT=netboot-x86     # PXE netboot trio (~10m, root)
    make test-pxe                       # end-to-end PXE chain

  Note: ``make build VARIANT=usb-x86`` and ``netboot-x86`` need root
  because live-build does a chroot + mount-bind dance; ``server-x86``
  and ``server-rpi`` don't (cloud-init in QEMU and qemu-user-static
  chroot, both unprivileged).

  The ``bty`` user inside the cooked server image defaults to
  password ``bty`` (rotate with ``passwd bty``). The admin user
  is ``odus`` / ``odus`` (passwordless sudo).
==============================================================
EOF
