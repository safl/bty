# bty via BMC / OOB-MGMT

BMCs (baseboard management controllers) and IP-KVM dongles expose a
target's HDMI output, USB ports, and power control over the network.
Most also provide **virtual media** -- mounting an ISO as a virtual
USB stick or CD-ROM that the target sees as a real boot device.
That's the path bty piggybacks on: hand the BMC the bty USB `.iso`,
set the target to boot from the virtual device, then drive the wizard
through the BMC's HDMI viewer the same way you would sitting at the
machine.

**Why this is the operator's favourite path for remote bring-up:**
[piKVM](https://pikvm.org) and [JetKVM](https://jetkvm.com) both
accept the GitHub release artifact URL directly. You don't download
the `.iso` to your workstation first -- the BMC fetches it from
GitHub itself at GitHub-CDN speed, not at your workstation's upload
speed. The flow is: paste a URL, click connect, power-cycle.

For server-class BMCs (Supermicro, iDRAC, iLO, XCC), virtual media
is usually paywalled and the firmware-side UI is sometimes a Java
applet. See [Proprietary server BMCs](#proprietary-server-bmcs) at
the bottom; the practical workaround is a [Ventoy USB stick](bty-ventoy.md)
plugged into the server's USB port.

## The release URL we'll paste

The bty release artifact lives at a stable, GitHub-CDN-backed URL:

```
https://github.com/safl/bty/releases/download/v$VERSION/bty-usbboot-pc-x86_64-v$VERSION.iso
```

Replace `$VERSION` with the release you want (e.g. `0.39.0`), or
look up the latest via `release.toml`:

```bash
VERSION=$(curl -fsSL https://github.com/safl/bty/releases/latest/download/release.toml \
  | grep -oP 'version = "\K[^"]+')
echo "https://github.com/safl/bty/releases/download/v$VERSION/bty-usbboot-pc-x86_64-v$VERSION.iso"
```

## piKVM (load by URL)

[piKVM](https://pikvm.org) is a Raspberry-Pi-based IP-KVM. HDMI in,
USB OTG out, KVM-over-IP web UI, mass-storage emulation.

### Step 1: Hand piKVM the .iso URL

In the piKVM web UI:

1. Open the **Storage** page.
2. Click **Add image from URL** (or the upload form's "URL" tab).
3. Paste the release URL from above.
4. Wait for piKVM to fetch (~600 MB at GitHub-CDN speed).
5. Set "Mode" to **CD-ROM**.
6. Click **Connect**.

### Step 2: Boot the target

1. piKVM **Power** page -> power-cycle.
2. In the HDMI viewer, watch BIOS/UEFI come up.
3. Pick the piKVM virtual storage device in the boot menu.
4. The bty live env boots; `bty` opens on tty1.

### Step 3: Point bty at a remote catalog

The piKVM-mounted `.iso` is read-only with no local storage for
target images. Point `bty` at a remote `bty-web` catalog:

1. At the source-pick prompt, type `c` (custom).
2. Enter the catalog URL: `http://<bty-host>:8080/catalog.toml`.
3. Pick an image; `bty` streams it from `bty-web` through the
   live env to the target's disk.

For bty's published default catalog without typing the URL, type
`d` at the source-pick prompt -- that's the bty release catalog
(nosi Debian / Ubuntu / Fedora / FreeBSD headless images plus a
Fedora desktop).

If you don't already have a `bty-web` instance, see
[bty via netboot](bty-netboot-pc.md) -- one `sudo uvx bty-lab deploy
/opt/bty` and you have one.

## JetKVM (load by URL)

[JetKVM](https://jetkvm.com) is a compact commercial IP-KVM
(USB-stick-shaped) with mass-storage emulation. Same constraint as
piKVM: the `.iso` is hosted as a single CD-ROM, so use a remote
`bty-web` for the image catalog.

### Step 1: Hand JetKVM the .iso URL

In the JetKVM web UI:

1. Open the **Virtual Media** panel.
2. Pick **Mount from URL** (the "URL" tab on the mount dialog).
3. Paste the release URL from above.
4. Mount as a virtual CD-ROM.

### Step 2 + 3: Boot and catalog

Identical to piKVM Steps 2 and 3 above.

## Proprietary server BMCs

Server-class hardware ships with a BMC with similar virtual-media
features, but two practical gotchas: vendor licensing and Java.

| Vendor       | BMC name                              | Virtual-media gating                   |
|---|---|---|
| Supermicro   | IPMI / SuperServer                    | **SFT-OOB-LIC** (one-time per board)   |
| Dell         | iDRAC                                 | **iDRAC Enterprise** (not Express)     |
| HPE          | iLO                                   | **iLO Advanced**                       |
| Lenovo       | XClarity Controller (XCC)             | **XCC Platinum**                       |
| ASRock Rack  | BMC                                   | Usually free in base firmware          |
| AMI MegaRAC  | many OEMs                             | Varies by vendor                       |

Older firmware also drops you into a Java applet that's painful to
keep running on modern desktops. HTML5-only firmware (post-2018-ish)
is much friendlier.

**The practical workaround for both license + Java pain: plug a
[Ventoy USB stick](bty-ventoy.md) into the server's USB port and
boot from it.** The BMC's HDMI viewer still works for video +
remote keyboard during the bty wizard; you just sidestep the
virtual-media licence and any Java applet. One Ventoy stick boots
bty alongside whatever other rescue / install ISOs you carry,
which is also handy for one-off vendor installers.

If you do have the licence and the HTML5 viewer, the flow mirrors
piKVM / JetKVM but URL-load is usually not supported -- you
download the `.iso` to your workstation first and upload via the
BMC UI:

1. Download the bty `.iso` (see [The release URL](#the-release-url-well-paste)).
2. Open the BMC's remote-console / KVM viewer, then the **Virtual
   Media** panel. Upload + attach as a virtual CD-ROM:
   - Supermicro: *Remote Control > iKVM/HTML5 > Virtual Media*.
   - iDRAC: *Configuration > Virtual Media > Connect*.
   - iLO: *Remote Console > Virtual Media > Image File CD/DVD-ROM*.
3. Set the boot order to the virtual CD-ROM (one-time boot menu is
   easiest), then power-cycle from the BMC.
4. Drive the wizard via the BMC's HDMI viewer; `bty` comes up on
   tty1.
5. Point at a remote catalog (same as piKVM Step 3 above).
