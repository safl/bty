# bty via JetKVM (KVM-over-IP)

[JetKVM](https://jetkvm.com) is a compact commercial IP-KVM
(USB-stick-shaped) with mass-storage emulation. Same constraint as
[piKVM](pikvm.md): the `.iso` is hosted as a single CD-ROM, so there is
no local storage for image files. Use a remote `bty-web` for the
catalog.

## Step 1: Set up a `bty-web` server reachable from the target

Same as [piKVM Step 1](pikvm.md#step-1-set-up-a-bty-web-server-reachable-from-the-target).
Run a `bty-web` instance somewhere on the LAN; note its IP and port.

## Step 2: Connect JetKVM to the target

Cable per the JetKVM quickstart (USB-C from JetKVM to target; JetKVM
to LAN). Pair the device with your JetKVM account, reach its web UI.

## Step 3: Upload the bty USB ISO to JetKVM

```bash
# Discover the current release + download the USB ISO. For a specific
# version, replace `latest` with a tag like v0.38.0.
VERSION=$(curl -fsSL https://github.com/safl/bty/releases/latest/download/release.toml \
  | grep -oP 'version = "\K[^"]+')
curl -fLO https://github.com/safl/bty/releases/download/v$VERSION/bty-usb-x86_64-v$VERSION.iso
```

In the JetKVM web UI:

1. Open the "Virtual Media" panel.
2. Upload `bty-usb-x86_64-v$VERSION.iso`.
3. Mount the uploaded image as a virtual CD-ROM.

## Step 4: Boot the target

1. Power-cycle the target via JetKVM's power-control page.
2. In the HDMI viewer, enter the target's boot menu and pick the
   JetKVM virtual storage device.
3. The bty live env boots; `bty` opens on tty1.

## Step 5: Point `bty` at the remote `bty-web` catalog

Same as [piKVM Step 5](pikvm.md#step-5-point-bty-at-the-remote-bty-web-catalog):
type `c` at the source-pick prompt, enter the catalog URL (e.g.
`http://10.0.0.5:8080/catalog.toml`). The catalog populates from the
server; images stream through the JetKVM-booted live env to the
target's disk.
