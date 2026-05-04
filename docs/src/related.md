# Related work

bty does not exist in a vacuum. This chapter places it relative to the
existing landscape so both human readers and automated agents can
quickly see where bty overlaps with other tools and where it carves out
its own niche. None of bty's individual features is unique; the
*combination* is unusual.

## Heavy fleet orchestrators - MAAS, Foreman, Tinkerbell, OpenStack Ironic

Same problem domain, an order of magnitude bigger.

- **MAAS** (Canonical) needs Postgres and a cluster mindset, and is
  Ubuntu-anchored.
- **Foreman** comes with Puppet, smart-proxy, and the rest of Red Hat's
  lifecycle universe.
- **Tinkerbell** (Equinix) is Kubernetes-native.
- **Ironic** (OpenStack) is a bare-metal service inside a cloud
  platform.

These scale to data centers. bty fits on a NUC and runs in a homelab.
Different tier of operator, different tier of complexity tax.

## Image-deploy in similar shape - FOG Project, iVentoy, Clonezilla SE

The closest neighborhood.

- **FOG Project** is the strongest direct parallel - MAC-keyed PXE
  image deploy, web UI, very similar mental model. PHP, dated UX,
  Linux-target-leaning.
- **iVentoy** is iPXE-based network deploy, lightweight,
  server-image-style. Closer to bty in feel; fewer features, less
  polish, partially closed.
- **Clonezilla SE** (Server Edition) is what the legacy bty was wrapped
  around - multicast disk imaging from a server. Powerful but more of a
  tool than a platform.

bty differs by being modern Python + iPXE, by serving both ad-hoc USB
and DevOps server in one project, by explicitly supporting non-Linux
images, and by the online CIJOE post-clone customisation with
known-good tracking.

## Installer-based provisioners - Cobbler, Microsoft WDS/MDT

Different model. They orchestrate OS *installers* (kickstart, preseed,
unattend) rather than deploying disk images. The result is a freshly
installed OS, not a bit-identical clone.

bty deliberately picks the image-clone model for speed and
reproducibility. "Install Ubuntu from scratch" is too slow for the
per-job CI cadence that motivates bty.

## OS-specific image platforms - Fedora CoreOS, Flatcar, Bottlerocket, NixOS

Same image-deploy philosophy, but each pins you to a specific OS.

NixOS is the most capable of the bunch in this niche. bty's positioning
is exactly the case NixOS does not cover: when the image is dictated by
hardware vendor or upstream (Ubuntu+NVIDIA driver tree, FreeBSD,
Windows). bty lets you deploy whatever someone else has already cooked.

## Image creation - Packer, mkosi, debian-live, jkab

Complementary, not competing. These build the cooked images that bty
consumes. `bty-media` is itself in this category for its appliance
images, but that is an internal concern; users mostly bring their own
pre-built images for the targets they provision.

## Manual flashers - dd, Etcher, Rufus, Ventoy

Ventoy is the closest in spirit to bty's USB live (carry images on a
stick, boot from it). Critical difference: Ventoy boots OS *installers*
from a stick; bty's USB live carries `bty` plus images and *flashes* a
target disk. Ventoy is "I want to install Linux on this laptop"; bty
is "I want this disk to look exactly like my reference image, fast,
with optional first-boot tuning."

## What makes bty distinct

The combination:

1. Image-deploy (not installer-driven).
2. OS-agnostic (any image, including vendor-pinned and non-Linux).
3. Single-appliance-server topology (homelab-scale, not data-center).
4. Both ad-hoc USB and DevOps server in one project (most pick one).
5. Modern, lightweight stack (NUC-class hardware, not a cluster).
6. CIJOE-driven post-clone customisation with server-tracked known-good
   state (most image-deploy tools stop at "bytes on disk").

That combination does not have a clean drop-in alternative.
"MAAS-without-the-MAAS-overhead, OS-agnostic, dual-mode" is the niche.
