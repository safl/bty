# Set up a bty server

The network-flash flow needs a long-running `bty-web`: it serves the browser
UI, the per-MAC PXE plans, the iPXE bootfiles, and the image bytes. The
operator's existing LAN DHCP server points PXE clients at this host; targets
PXE-boot into the server's catalog, flash themselves, reboot.

Run `bty-web` as a container. The deploy ships three pieces:

1. **bty-web** (`ghcr.io/safl/bty-web`) -- the policy / PXE layer: UI, PXE
   plans, boot artifacts, and images over HTTP on `:8080`.
2. **withcache** (`ghcr.io/safl/withcache`) -- a URL-keyed artifact cache that
   holds the image bytes, so a fleet pulls each image once.
3. **tftp** (`ghcr.io/safl/bty-tftp`, optional) -- a small TFTP sidecar that
   serves the ~1 MB iPXE bootfile for legacy BIOS / older UEFI clients that
   bootstrap over TFTP. It serves only the bootfile; the kernel / initrd /
   squashfs come from bty-web over HTTP.

The operator UI is gated by `$BTY_ADMIN_PASSWORD` (unset = open, with a startup
warning). End state after `up`: a browser URL ready to register machines and
serve images.

## Stand it up

The canonical, step-by-step instructions live with the deploy assets:

- [`deploy/README.md`](https://github.com/safl/bty/blob/main/deploy/README.md)
  -- compose quick-start, the optional TFTP profile, Quadlet units for
  boot-autostart, the withcache wiring, and the named-volume layout.
- [`walkthrough-server-docker.md`](walkthrough-server-docker.md) -- the
  bty-web container in depth: both PXE boot lanes (UEFI HTTP Boot and TFTP
  PXE), bind-mount permissions, env vars, and password rotation.

A minimal start with `uvx bty-lab init` -- no clone required, `uv` (or
`pipx`) on the host is enough:

```sh
uvx bty-lab init ./bty-host       # writes compose.yml + .env.example + README
cd bty-host
cp .env.example .env
"${EDITOR:-vi}" .env                      # set HOST_ADDR + WITHCACHE_ADMIN_PASSWORD
podman compose up -d
#   bty:       http://<host>:8080/ui
#   withcache: http://<host>:3000/

# add TFTP for BIOS clients that bootstrap over it:
podman compose --profile tftp up -d
```

`init` pins the `bty-web` / `bty-tftp` image tags to the bty CLI version that
emitted the compose, so the file you get and the bytes you pull match. State
(bty's DB and images, withcache's blobs) lives in host bind-mounts under
`./data/` and survives container restarts and image re-pulls. The image cache
is delegated to withcache, so multiple targets pull each image once. To
upgrade, re-run `uvx bty-lab init --force .` then
`podman compose pull && podman compose up -d`.

## Configure DHCP

bty runs no DHCP role: a working LAN DHCP server is a hard prerequisite, and its
config carries the PXE / HTTP-Boot pointers (option 60 / 66 / 67) that direct
clients at this host. The exact values for your deploy are on the bty-web
Settings page under **DHCP / Network boot**.

| | PXE (TFTP) | UEFI HTTP Boot |
|---|---|---|
| Vendor class (option 60) | `PXEClient` | `HTTPClient` |
| Next-server (option 66) | host IP | host IP (still required) |
| Bootfile (option 67) | `ipxe.efi` | `http://<host>:8080/boot/ipxe.efi` |

Option 66 stays pointed at the host for HTTP Boot too: bty's iPXE binary chains
on to `http://<host>:8080/pxe-bootstrap.ipxe`, so it needs the next-server even
though the bootfile is a full URL. Once iPXE is running, both paths fetch
`/pxe-bootstrap.ipxe`, then the per-MAC plan.

## Flash a target over PXE

Once a target's MAC is registered with an assigned image, set the target's
BIOS / UEFI to **boot from the network (PXE) first**. bty then drives every
subsequent boot via `boot_mode`; you set the firmware order once. Mind the
post-flash boot: with `boot_mode=ipxe-exit` (the default) bty boots the disk
via iPXE -- UEFI exits to the firmware boot order, legacy BIOS sanboots the
drive -- and the `bty-flash-*` modes boot the just-flashed disk the same way.
On legacy BIOS the drive is `0x80` (first disk) unless you set `sanboot_drive`;
on UEFI there's nothing to set. (A flashed box that won't boot is almost always
a firmware / drive-number problem; see
[Firmware boot order](concepts.md#firmware-boot-order).) On power-on it will:

1. DHCP-discover from your LAN's DHCP server, configured to return option
   66/67 pointing at the bty host.
2. Chain into the bty iPXE script. Cmdline carries `bty.server=URL` +
   `bty.mac=MAC` only.
3. Boot the netboot kernel + initrd + squashfs trio. `bty-on-tty1.service`
   exec's `bty --server X --mac Y` on tty1; `bty` GETs
   `<server>/pxe/<mac>/plan`, sees `mode=flash` (because
   boot_mode=bty-flash-always + ref + serial), writes the image to the
   local disk, POSTs `/pxe/<mac>/done`, reboots.

The server's machine-detail page shows live progress + last flashed timestamp.
Subsequent boots skip PXE (BIOS falls back to disk) and the target runs whatever
the freshly-flashed image provisions to.

## What you can do today

- PXE-flash any number of targets to a registered image, hands-free, in
  parallel.
- Mix the network-flash flow (this walkthrough) with the USB-stick flow
  ([walkthrough-usb](walkthrough-usb.md)): both run the same `bty` flash code,
  driven by the plan endpoint vs the local wizard.
- Swap images per-target without rebooting the server.

## Known limitations

- **DHCP stays with the operator's LAN**. bty runs no DHCP server (proxy or
  full); a working LAN DHCP server is a hard prerequisite, and its config must
  be extended with option 60 / 66 / 67 to direct PXE clients at the bty host.
- **UEFI Secure Boot** isn't supported: the bty netboot kernel isn't
  shim-signed. Disable Secure Boot on targets you're PXE-flashing, or use the
  USB stick flow.
- **Single bootfile, no DHCP userclass logic** (UEFI). Stock iPXE re-DHCPs
  after it loads and re-fetches the DHCP bootfile -- itself -- unless the DHCP
  server hands iPXE a *different* bootfile by matching `user-class=iPXE`. bty
  sidesteps that: its custom `ipxe.efi` (baked into the bty-web and bty-tftp
  images) embeds `chain http://${next-server}:8080/pxe-bootstrap.ipxe`, so the
  operator's DHCP only ever needs one bootfile and no userclass rules. The
  legacy-BIOS `undionly.kpxe` is stock, so BIOS still needs the userclass trick
  (UniFi / Kea client-classes, dnsmasq `dhcp-userclass`, ISC-DHCPd conditional
  `if`) -- and BIOS is unverified anyway (below).
- **UEFI tested, legacy BIOS not yet**. bty's netboot path has so far been
  exercised only on UEFI targets. The legacy-BIOS branch (`sanboot --drive`
  instead of the UEFI hand-back to firmware) is implemented but not field-tested;
  treat BIOS as unverified for now.
