# bty via piKVM (KVM-over-IP)

[piKVM](https://pikvm.org) is a Raspberry-Pi-based IP-KVM. It exposes a
target's HDMI + USB over the network and can emulate a USB mass-storage
device, so the target boots from a `.iso` uploaded via the piKVM web
UI -- ideal for flashing a colocated / remote box with no on-site
hands.

**Constraint: piKVM hosts the `.iso` as a single CD-ROM.** The kernel
inside the bty live env cannot reach the `.iso`'s internal partitions,
so there is no place on the piKVM for image files. Use a remote
`bty-web` instance for the catalog instead.

## Step 1: Set up a `bty-web` server reachable from the target

The simplest option is the canonical container deploy; see
[walkthrough-server-docker.md](../walkthrough-server-docker.md) for the
full setup. On any host the target can reach over the LAN:

```bash
sudo mkdir -p /opt/bty && sudo chown "$USER:$USER" /opt/bty
uvx bty-lab deploy /opt/bty
```

Default UI login is `bty` / `bty` -- change in `/opt/bty/envvars`
before exposing past trusted LAN. Note the host's IP (e.g. `10.0.0.5`);
the target needs to reach it on TCP 8080.

Upload your pre-built images via the bty-web Images page
(`http://10.0.0.5:8080/ui/images`).

## Step 2: Connect piKVM to the target

1. HDMI: target's HDMI out -> piKVM HDMI in.
2. USB: piKVM's USB-C OTG port -> target's USB port.
3. Network: piKVM to your LAN.
4. Open piKVM's web UI in a browser.

## Step 3: Upload the bty USB ISO to piKVM

```bash
# Discover the current release + download the USB ISO. For a specific
# version, replace `latest` with a tag like v0.38.0.
VERSION=$(curl -fsSL https://github.com/safl/bty/releases/latest/download/release.toml \
  | grep -oP 'version = "\K[^"]+')
curl -fLO https://github.com/safl/bty/releases/download/v$VERSION/bty-usb-x86_64-v$VERSION.iso
```

In the piKVM web UI:

1. Open the "Storage" page.
2. Click "Upload" and select `bty-usb-x86_64-v$VERSION.iso`.
3. Wait for the upload to finish.
4. Pick the entry; in the dialog, set "Mode" to **CD-ROM**.
5. Click "Connect" (or the analogous "Attach" button).

## Step 4: Boot the target

1. From piKVM's "Power" page, power-cycle the target.
2. In the piKVM HDMI viewer, watch the target's BIOS/UEFI come up.
3. Enter the target's boot menu, pick the piKVM virtual storage
   device.
4. The bty live env boots; `bty` opens on tty1.

## Step 5: Point `bty` at the remote `bty-web` catalog

The local catalog is empty (no images on the piKVM). Pick the custom
catalog option on the SELECT_CATALOG stage:

1. At the source-pick prompt, type `c` (custom).
2. Type the catalog URL when asked:
   `http://10.0.0.5:8080/catalog.toml` (substitute your host).
3. Confirm with Enter.

`bty` fetches the catalog from `GET /catalog.toml`, advances to
SELECT_IMAGE, and you pick + flash from there. The image streams
directly from `bty-web` through the live env to the target's disk;
piKVM only carried the boot env.

For bty's published default catalog without typing the URL, type `d`
instead at the source-pick prompt: that's the bty release catalog (nosi
Debian / Ubuntu / Fedora / FreeBSD headless images plus a Fedora
desktop).
