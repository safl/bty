# Quickstart

Two paths into bty: a one-command server deploy on the host, then any
of the per-flow tutorials for the operator side (USB stick on x86 or
a Pi, or netboot).

## Deploy the bty server

```bash
sudo uvx bty-lab deploy /opt/bty
#   bty-web:   http://<host>:8080/ui     (login: bty-lab / bty-lab)
#   withcache: http://<host>:3000/       (login: bty-lab / bty-lab)
```

That's it. `deploy` auto-detects install mode from your euid:

- **As root (recommended)** -- full **system install**: writes
  `envvars`, brings up the stack *with* the TFTP sidecar, installs
  Podman Quadlet units to `/etc/containers/systemd/`, and starts the
  services via systemctl. Stack survives host reboots.
- **As a regular user** -- **user install**: compose-only. No TFTP
  sidecar (binds privileged UDP/69), no autostart. Operator must
  re-run `podman compose up -d` after host reboot. UEFI HTTP Boot
  works; legacy BIOS PXE clients won't. The CLI prints exactly what
  was skipped and how to promote to a system install at the end.

`HOST_ADDR` is detected from the host's outbound-route IP; admin
passwords default to `bty-lab`. Change the passwords in `/opt/bty/envvars`
before exposing past trusted LAN.

- `uvx bty-lab upgrade /opt/bty` -- in-place upgrade. Auto-detects
  compose- vs Quadlet-managed; preserves `envvars` + `data/`.
- `uvx bty-lab init /opt/bty` -- emit files only, no side effects
  (inspect / customise before applying).

Bind-mount layout, env vars, the full subcommand surface:
[`deploy/README.md`](https://github.com/safl/bty/blob/main/deploy/README.md)
and [walkthrough-server-docker.md](walkthrough-server-docker.md).

## Flash a target from a USB stick

The fastest no-server path: write the bty USB ISO to a stick, boot
the target, pick image + disk, done. Full operator walkthrough --
download, `dd`, BIOS boot keys, troubleshooting -- in the
[bty via bty-usbboot-pc tutorial](tutorials/bty-usbboot-pc.md). For a
Raspberry Pi target see [bty via bty-usbboot-rpi](tutorials/bty-usbboot-rpi.md).
For the server-driven netboot flow, [bty via netboot](tutorials/bty-netboot-pc.md).
