# Set up a bty server appliance

The bty server appliance is the network-flash flow's delivery
vehicle: a turnkey image you `dd` onto a small box (or VM, or
Raspberry Pi 4/5) that brings up `bty-web` (browser UI), iPXE
binaries, and TFTP via dnsmasq. The operator's existing LAN DHCP
server points PXE clients at this appliance; targets PXE-boot
into the server's catalog, flash themselves, reboot.

Two paths:

1. **`server-x86`** - x86_64 disk image for any small-form-factor
   PC, NUC, or virtual machine. Cloud-init bake of a Debian 13
   cloud image inside QEMU, output as a dd-able `.img.gz`.
2. **`server-rpi`** - arm64 SD-card image for Raspberry Pi 4 / 5.
   Customisation of the upstream Pi OS Lite image via
   qemu-aarch64-static chroot, output as a dd-able `.img.gz`.

Both ship the same bty-web + PXE stack with the same default
credentials (`bty` / `bty` for the browser UI, `odus` / `odus`
for SSH admin). End state after first boot: a browser URL on
tty1, ready to register machines and serve images.

## Prerequisites

| You need | Notes |
|---|---|
| **Target hardware** | x86: any small-form-factor PC, NUC, or VM with at least 4 GiB RAM and ~10 GiB disk. arm64: Raspberry Pi 4 (4 GiB+ RAM model) or 5 with an SD card 8 GiB or larger. |
| A **flashing tool** | `dd` (Linux/macOS), Balena Etcher, Raspberry Pi Imager, or Rufus DD-mode. All decompress `.gz` natively. |
| A **network cable** to the target's LAN | The PXE-flash flow needs the server reachable from the targets for TFTP + HTTP fetches. WiFi works fine for the browser UI; for PXE the operator's DHCP server still has to point clients at this appliance regardless of the medium. |

## Step 1: Get the server image

You have two options - download a pre-built artifact or build
from source.

### Option A: Download the latest pre-built image (fastest)

```bash
mkdir -p ~/system_imaging/disk && cd ~/system_imaging/disk

# x86_64
curl -fLO https://github.com/safl/bty/releases/latest/download/bty-server-x86_64.img.gz
curl -fLO https://github.com/safl/bty/releases/latest/download/bty-server-x86_64.img.gz.sha256
sha256sum -c bty-server-x86_64.img.gz.sha256

# OR Raspberry Pi 4/5
curl -fLO https://github.com/safl/bty/releases/latest/download/bty-server-rpi-arm64.img.gz
curl -fLO https://github.com/safl/bty/releases/latest/download/bty-server-rpi-arm64.img.gz.sha256
sha256sum -c bty-server-rpi-arm64.img.gz.sha256
```

For a specific version, swap `latest` for a tag (e.g. `v0.11.1`).

### Option B: Build from source (when you want to modify it)

```bash
make media-deps                       # one-time: pipx installs cijoe
sudo make build VARIANT=server-x86    # ~10-15 minutes (cloud-init bake in QEMU)
# OR
sudo make build VARIANT=server-rpi    # ~5-10 minutes (chroot customisation)
```

x86 build needs KVM access on the build host. arm64 build runs
under `qemu-aarch64-static` so any amd64 Linux box with
`binfmt_misc` works.

When the build finishes:

```text
~/system_imaging/disk/
  bty-server-x86_64.qcow2          <- intermediate (handy for QEMU smoke tests)
  bty-server-x86_64.img.gz         <- the file you'll write to the disk
  bty-server-x86_64.img.gz.sha256
```

(The `.qcow2` is left behind on a local build for convenience;
the release-page artifact only carries the `.img.gz`.)

## Step 2: Write the image to the target's disk

**Identify the target disk first** - this step is destructive.

**On Linux:**

```bash
# x86_64 server image to a SATA/NVMe disk
gunzip -d --stdout ~/system_imaging/disk/bty-server-x86_64.img.gz | \
  sudo dd of=/dev/sdX bs=4M status=progress conv=fsync
sync
```

**On Raspberry Pi 4/5:**

Plug the SD card (8 GiB or larger) into a Linux/macOS/Windows
host (NOT the Pi itself - the Pi can't write its own boot
medium). The image goes onto the SD card; you then move the
SD card to the Pi and power it on.

*Identify the SD card first.* It's the device that appeared
when you plugged it in. Easy way: run `lsblk` (Linux),
`diskutil list` (macOS), or `wmic diskdrive list brief`
(Windows PowerShell) before and after inserting the card; the
new entry is the SD card. Sizes match the card's marketed
capacity, give or take a GiB.

*Linux*:

```bash
gunzip -d --stdout ~/system_imaging/disk/bty-server-rpi-arm64.img.gz | \
  sudo dd of=/dev/sdX bs=4M status=progress conv=fsync
sync
```

Replace `/dev/sdX` with the SD card's device node (often
`/dev/mmcblk0` on a built-in card reader). Double-check with
`lsblk` - flashing the wrong disk wipes whatever is on it.

*macOS*:

```bash
diskutil list                                # find the SD card (e.g. /dev/disk4)
diskutil unmountDisk /dev/diskN              # unmount, don't eject
gunzip -d --stdout ~/system_imaging/disk/bty-server-rpi-arm64.img.gz | \
  sudo dd of=/dev/rdiskN bs=4m               # note the ``r`` prefix - raw device, much faster
sync
diskutil eject /dev/diskN
```

*Windows*: use Raspberry Pi Imager (recommended) or balenaEtcher.
Both accept `.img.gz` directly without manual decompression.

- **Raspberry Pi Imager**: choose `Operating System -> Use
  custom`, pick the `.img.gz`, choose your SD card under
  `Storage`, then Write. Imager handles the gunzip step
  internally; do NOT pre-decompress.
- **balenaEtcher**: Flash from file -> pick the `.img.gz` ->
  Select target -> Flash.

(Imager is also available on Linux and macOS if you'd rather
not type the `dd` line.)

**On a VM:**

For a quick QEMU smoke test, point the VM at the `.qcow2`
intermediate (faster than expanding the `.img.gz`):

```bash
qemu-system-x86_64 \
  -enable-kvm -m 4096 -cpu host -smp 2 \
  -drive file=~/system_imaging/disk/bty-server-x86_64.qcow2,if=virtio \
  -nic user,model=virtio,hostfwd=tcp::8080-:8080
```

Browse to <http://localhost:8080> once the VM finishes boot.

For production deployment on a hypervisor, use the `.img.gz`
directly: most hypervisors accept compressed disk images as
input or you can pre-decompress to `.img`.

## Step 3: First boot

Power on the target. The bty-server image:

1. Resizes the rootfs partition to fill the operator's disk
   (one-shot via `bty-grow-rootfs.service`).
2. Brings up systemd-networkd against the operator's LAN.
3. Runs `bty-web-init.service` once to set up the state
   directory tree and rewrite `/etc/issue` with the actual
   server URL + default credentials.
4. Starts `bty-web.service` (long-running) on port 8080.
5. Starts `dnsmasq.service` for TFTP (the appliance does NOT
   run any DHCP role -- bty deliberately stays out of DHCP and
   relies on the operator's existing LAN DHCP server to point
   PXE clients at this box).

Tty1 ends up showing something like:

```
======================================================================
  bty 0.8.2 server appliance

  Browser UI:    http://192.168.1.42:8080
  Default login: bty / bty (rotate before exposing this server)
  SSH admin:     odus / odus.321

  Configure your LAN DHCP server to point PXE clients at this
  appliance (option 60 = "PXEClient", option 66 = this IP,
  option 67 = "ipxe.efi"). See /ui/boot for the cheatsheet.
======================================================================

bty 0.8.2 on bty-server (tty1)

bty-server login: _
```

The version string updates per release.

## Step 4: Log in via the browser

Open the URL shown on tty1 from any machine on the same LAN.
Default browser-UI credentials are `bty / bty`. **Rotate before
exposing this server** to anything beyond a trusted network:

```bash
# SSH in as the admin user (default: odus / odus.321, passwordless sudo)
ssh odus@<server-ip>
sudo passwd bty
```

Initial UI tour:

- **`/ui/machines`** - register targets by MAC. Each machine
  gets a row with assigned image + boot policy.
- **`/ui/images`** - upload `*.img.zst` / `*.img.gz` / `*.img.xz`
  / `*.img.bz2` / `*.qcow2` images via PUT or drag-and-drop.
  These end up under `/var/lib/bty/images/` on the server and
  get streamed to targets at flash time.
- **`/ui/boot`** - manages the netboot trio (`vmlinuz`,
  `initrd`, `squashfs`) AND carries the router-config
  cheatsheet (option 60 / 66 / 67 values to paste into the
  LAN's DHCP server) + a Start/Stop/Restart panel for the
  local `dnsmasq.service` (which serves the TFTP root). The
  three live behind sub-nav pills: List / Fetch netboot
  artifacts / DHCP / PXE / TFTP daemon.
- **`/ui/settings`** - operator-account info for the logged-in
  user (PAM password rotation + session-cookie rotation
  hints). Reached via the gear icon in the user-bar, not from
  the top nav.

## Step 5: Flash a target over PXE

Once a target's MAC is registered with an assigned image, configure
the target's BIOS / UEFI to PXE-boot first. On power-on it'll:

1. DHCP-discover from your LAN's DHCP server, which is configured
   to return option 66/67 pointing at the bty appliance.
2. Chain into the bty iPXE script. Cmdline carries
   ``bty.server=URL`` + ``bty.mac=MAC`` only.
3. Boot the netboot kernel + initrd + squashfs trio.
   `bty-on-tty1.service` exec's `bty --server X --mac Y` on
   tty1; ``bty`` GETs `<server>/pxe/<mac>/plan`, sees
   ``mode=auto`` (because boot_policy=bty-flash-always + ref + serial),
   writes the image to the local disk, POSTs `/pxe/<mac>/done`,
   reboots.

The server's machine-detail page shows live progress + last
flashed timestamp. Subsequent boots skip PXE (BIOS falls back to
disk) and the target runs whatever the freshly-flashed image
provisions to.

## Architecture at a glance

```
+----------------+              +-------------------+
| Operator's     |   browse     | bty-server        |
| workstation    +-------------->  bty-web :8080    |
|                |              |  iPXE / TFTP / DH |
+----------------+              +-------------------+
                                          | PXE chain
                                          v
                                +-------------------+
                                | Target machine    |
                                |  bty on tty1      |
                                |  (plan: mode=auto)|
                                |  -> local disk    |
                                +-------------------+
```

## What you can do today

- PXE-flash any number of targets to a registered image, hands-
  free, in parallel.
- Mix the network-flash flow (this walkthrough) with the
  USB-stick flow ([walkthrough-usb](walkthrough-usb.md)) -- both
  end up running the same ``bty`` flash code, just driven by the
  plan endpoint vs the local wizard.
- Swap images per-target without rebooting the server.
- Add a second disk as a **persistent image store** so the cache
  survives appliance reflashes
  ([walkthrough-image-store](walkthrough-image-store.md)).

## Post-deploy hardening

The pre-built image ships with appliance defaults that prioritise
"works on first boot" over "locked down for the open internet".
A few things to address before exposing the server beyond a
trusted LAN:

- **Default credentials.** Rotate `bty / bty` (browser UI) and
  `odus / odus.321` (SSH admin) on first login: `sudo passwd bty`,
  `sudo passwd odus`. The `/etc/issue` banner reminds you on
  every console login.
- **Per-instance SSH host keys.** `bty-ssh-host-keys.service`
  runs `ssh-keygen -A` on first boot of each freshly-flashed instance, so
  every appliance has unique host keys.
- **No built-in firewall.** The image does not ship with `ufw` or
  `nftables` rules. Listening ports out of the box: `:8080`
  (bty-web HTTP), `:22` (sshd), `:69` UDP (TFTP, dormant until a
  PXE client asks), and `:67` UDP (DHCP-proxy, dormant until you
  click "Activate" in Settings). For an internet-exposed deploy,
  put the appliance behind a reverse proxy / VPN, or `apt
  install ufw` and constrain inbound to your management IP.
- **Manual security upgrades.** The image masks `apt-daily.timer`
  and `apt-daily-upgrade.timer` so a stock Debian boot does not
  wake up doing 30s of disk IO that competes with the bty
  services. Trade-off: you do `sudo apt update && sudo apt
  upgrade` yourself on whatever cadence you choose. Schedule a
  cron / systemd-timer if you want it automatic on long-running
  installs.

## Known limitations

- **DHCP stays with the operator's LAN**. bty does NOT run a DHCP
  server (proxy or full); a working LAN DHCP server is a hard
  prerequisite, and its config must be extended with option 60 /
  66 / 67 to direct PXE clients at the bty appliance. The
  `/ui/boot?section=dhcp-pxe` page has a cheatsheet with the
  exact values.
- **UEFI Secure Boot** isn't supported - the bty netboot kernel
  isn't shim-signed. Disable Secure Boot on targets you're
  PXE-flashing, or use the USB stick flow.
- **iPXE chain-loop**. After the firmware TFTPs `ipxe.efi`, iPXE
  re-DHCPs with `user-class=iPXE`; stock Debian iPXE will
  re-fetch itself unless your LAN DHCP returns a different
  bootfile (e.g. `http://<bty>/pxe-bootstrap.ipxe`) when it sees
  `user-class=iPXE`. Most modern DHCP servers (UniFi via
  config.gateway.json or Kea client-classes, dnsmasq via
  `dhcp-userclass`, ISC-DHCPd via conditional `if`) support this.
