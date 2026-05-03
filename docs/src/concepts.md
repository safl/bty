# Concepts

The vocabulary used throughout the rest of the documentation.

## Image

A pre-built system image: the bytes that go on the target disk. bty
treats images as sealed artifacts and never authors their contents.
Supported formats: `.qcow2`, `.img`, `.img.zst`. Images live under a
configured *image root*.

## Target

The block device on the machine being provisioned. For the direct-flash
flow this is the local disk seen by the live environment (typically
`/dev/nvme0n1` or `/dev/sda`). For the network-flash flow it is the
target machine's primary disk, selected by the live environment.

## Provisioning mode

What (if anything) runs on first boot. Three modes:

- `none` — no post-flash configuration; reboot into the cooked image
  as-is.
- `cloud-init` — populate the OS's cloud-init seed (NoCloud datasource);
  the OS picks it up on first boot.
- `cijoe` — run a CIJOE workflow that adjusts the deployed system to a
  known-good state. See the [components](components.md) chapter for the
  offline (USB live) vs. online (PXE/server) execution modes.

## Machine record

A `bty-web`-only concept. A persistent entry in the server's state
keyed by MAC address that captures: assigned image, provisioning mode,
optional hostname, references to CIJOE workflows, and (after first boot)
the post-workflow known-good baseline. The server uses machine records
to render per-MAC iPXE configurations and to drive online CIJOE runs.
