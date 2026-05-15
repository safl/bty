"""PXE proxy-DHCP daemon for the bty-server appliance.

Replaces dnsmasq's proxy-DHCP role. dnsmasq remains for TFTP only.

Why we replaced it: dnsmasq's proxy-DHCP sends a minimal phase-1
``DHCPOFFER`` (options 53/54/60/97) and expects the PXE client to
follow up with a phase-2 BINL request on UDP 4011 to fetch the
actual bootfile. Multiple modern UEFI firmware implementations
tested on real bty hardware skip BINL entirely and expect the
bootfile inline in the phase-1 offer; the result is a forever-
retry loop instead of a boot. No dnsmasq config combination
makes the bootfile appear in the offer (eight variations tried).

This daemon:

- listens on UDP 67 for ``DHCPDISCOVER`` broadcasts,
- filters for ``option 60`` starting with ``PXEClient`` or ``HTTPClient``,
- maps the RFC 4578 ``client-arch`` (option 93) to a bootfile name,
- sends a ``DHCPOFFER`` with the bootfile written into BOTH the
  BOOTP ``file[]`` field AND option 67 (bootfile-name) -- which is
  what modern UEFI firmware reads.

It does NOT allocate IPs; the operator's existing DHCP server
keeps doing that. The proxy just enriches the PXE-side response.

Submodules:
  * :mod:`bty.pxe.proxy` -- the asyncio daemon + console-script entry.
  * :mod:`bty.pxe.wire`  -- BOOTP/DHCP packet parse + build (pure,
    no socket dependency).
"""

from __future__ import annotations
