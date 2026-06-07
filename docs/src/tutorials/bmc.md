# bty via a server BMC (Supermicro / iDRAC / iLO)

Server-class hardware ships with a baseboard management controller
(BMC): a small always-on management computer with its own NIC, web UI,
and KVM-over-IP / virtual-media features. The major vendors:

| Vendor | BMC name |
|---|---|
| Supermicro | IPMI / SuperServer |
| Dell | iDRAC |
| HPE | iLO |
| Lenovo | XClarity Controller (XCC) |
| ASRock Rack | BMC |
| AMI MegaRAC | many OEMs |

bty's USB ISO is just a bootable hybrid ISO, so any BMC with a
**virtual CD-ROM / virtual USB** feature can mount it and boot the
host into bty. The flash flow is then identical to a physical stick:
the bty wizard runs on tty1, you drive it through the BMC's HTML5 /
Java KVM viewer, and the image bytes are fetched over the target's
main NIC.

## License caveats (read before you buy hardware)

Virtual-media is the gateway feature -- and most BMC vendors paywall it.
Plan for it up front:

- **Supermicro** -- virtual media is part of the **Out-Of-Band (OOB)**
  license, a one-time per-board upgrade
  ([SFT-OOB-LIC](https://www.supermicro.com/en/solutions/management-software/bmc-resources)).
  Without it, the BMC's KVM viewer works but the **Virtual Media** /
  CD-ROM mount option is greyed out. Cheap, but you have to remember to
  buy it before you need it.
- **Dell iDRAC** -- virtual media requires **iDRAC Enterprise** (or
  Datacenter), not the Express tier some servers ship with. Check the
  service tag's entitlements before you assume virtual media works.
- **HPE iLO** -- virtual media requires the **iLO Advanced** license
  (vs the bundled iLO Standard). Same situation as iDRAC: paid tier.
- **Lenovo XCC** -- "remote presence" (KVM + virtual media) is the **XCC
  Platinum** upgrade on most current ThinkSystems.
- **ASRock Rack / open-firmware OpenBMC variants** -- typically include
  virtual media in the base firmware; no separate license.

For one-off bring-up of a few machines, a [PiKVM](pikvm.md) or
[JetKVM](jetkvm.md) dongle is often cheaper than the BMC upgrade and
gives you the same virtual-media flow without the vendor paywall.

## Steps

The vendor UIs differ in detail but the flow is the same:

1. **Download the bty ISO** on the workstation talking to the BMC.
   For a specific version, replace `latest` with a tag like `v0.38.0`:
   ```bash
   VERSION=$(curl -fsSL https://github.com/safl/bty/releases/latest/download/release.toml \
     | grep -oP 'version = "\K[^"]+')
   curl -fLO https://github.com/safl/bty/releases/download/v$VERSION/bty-usb-x86_64-v$VERSION.iso
   ```
2. **Open the BMC's remote-console / KVM viewer**, then open the
   **Virtual Media** panel. Attach `bty-usb-x86_64-v$VERSION.iso` as
   a virtual CD-ROM.
   - Supermicro: *Remote Control > iKVM/HTML5 > Virtual Media*.
   - iDRAC: *Configuration > Virtual Media > Connect*.
   - iLO: *Remote Console > Virtual Media > Image File CD/DVD-ROM*.
3. **Set the boot order to the virtual CD-ROM** (typically via the BMC's
   one-time boot menu so it doesn't persist), then power-cycle the host
   from the BMC.
4. **Drive the wizard.** bty comes up on tty1 inside the KVM viewer:
   pick image, pick disk, confirm flash, reboot. Image bytes flow over
   the target's main NIC; the BMC channel only carries video + HID.

## Caveats

- **Image catalog source.** Same as with PiKVM / JetKVM: the virtual
  ISO is read-only, so `BTY_IMAGES` is empty. Use `[d] default` (streams
  from GHCR via `oras://`) or `--catalog` against your bty-web instance.
- **Java KVM viewers.** Older iDRAC / iLO firmware drops you into a
  Java applet that's painful to keep running on modern desktops.
  HTML5-only firmware (post-2018-ish) is much friendlier.
- **Network reach at flash time.** The target's main NIC must have a
  DHCP lease and reach the image source (GHCR over the internet, or
  your bty-web on the LAN). The BMC's management network is irrelevant
  to the actual image transfer.
