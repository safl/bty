# Walkthrough: ramboot mode

`boot_mode=ramboot` is the in-place sibling of bty's flash boot modes.
The target machine never touches its local disk: it chains a slim
initrd that connects to an NBD multiplexer (`nbdmux`), mounts the
catalog image's root partition read-only, overlays a tmpfs for writes,
and pivot_roots into the catalog image's userspace. The overlay
vanishes on reboot.

Useful for CI runs where flashing is overkill, preview workflows
("just boot this for a minute and see"), or lab boxes that want to
cycle through a handful of images per day without burning through
SSD writes.

Linux-only target image (the initramfs uses overlayfs, which is a
Linux kernel feature). The bty-web side runs anywhere; the
operator's UI is unchanged.

## What you need before starting

You already have a working bty-web deploy with at least one catalog
entry bound to a machine. The `bty-lab deploy` defaults bring up the
extra moving parts automatically (since v0.62):

- The `nbdmux` sidecar in `compose.yml` listens on port 8082 (HTTP
  control plane) and 10809 (NBD listener).
- bty-web and nbdmux share `${BTY_HOST_DATA_DIR}/nbdmux/images/`. bty-web
  decompresses catalog images there; nbdmux serves the same bytes over
  NBD without a copy.
- The Settings page gains a "Ramboot" card under the existing
  Display card.

You also need the `ramboot-init` netboot artifact for the
target's architecture in `${BTY_BOOT_DIR}` (or wherever bty-web
serves `/boot/` from). It ships alongside the existing netboot-pc
trio on every bty release; the "Fetch artifacts" button on
`/ui/netboot` grabs all of them in one go.

If your deploy predates v0.62, run `uvx bty-lab init --force .` to
regenerate `compose.yml` with the new sidecar, then
`podman compose pull && podman compose up -d`.

## Configure the bytes path

`bty-lab init` writes `[nbdmux] url = "http://<host>:8082"` into the
generated `bty.toml` for you (since v0.64.1; same pattern as
withcache), so the bytes path is already configured when the stack
comes up. The **Settings -> Ramboot** card is an override surface:
open it only when you run nbdmux somewhere other than the in-stack
sidecar (a separate LAN box, a different port), or when you want to
change the default overlay size.

The card carries two fields:

- **nbdmux URL**: leave blank to inherit the deploy default; set to
  `http://<host>:8082` (or wherever your nbdmux lives) to override.
  Saving an empty value AND clearing the `[nbdmux] url` line in
  `bty.toml` disables the boot mode globally; the iPXE chain then
  falls back to bty-tui with a reason on the events feed.
- **Overlay size**: `10G` is the default. Sets the tmpfs cap for the
  in-target write overlay. Make this bigger if your workload writes
  more than a few GB of logs / scratch state; cap it lower if you
  need to leave RAM for the running OS. The Linux mount layer parses
  the value at boot time; an invalid suffix surfaces on the serial
  console as the initramfs panics, not at form submit.

The values resolve as Settings override first, then `$BTY_NBDMUX_URL`
env, then the `[nbdmux] url` field in `bty.toml`, then unset.

## Warm an image in nbdmux first

Since v0.65.0 the warming pipeline (fetch + decompress + register
the export) lives in **nbdmux**, not bty-web. Operators populate
nbdmux directly; bty-web only validates that a ref is ready before
letting the operator bind a machine to it.

Open the nbdmux dashboard at `http://<host>:8082/` and POST a warm
request. The simplest path is from a shell on the host:

```bash
curl -X POST http://<host>:8082/exports \
  -H "Content-Type: application/json" \
  -d '{
    "name": "<bty_image_ref>",
    "src_url": "<catalog-entry-src>",
    "readonly": true
  }'
```

`<bty_image_ref>` is the catalog entry's ref (visible in bty-web's
catalog table or `/images`). `<catalog-entry-src>` is the upstream
URL (`oras://...` or `https://.../...img.gz`); copy it from the same
row. nbdmux routes the fetch through the configured
`NBDMUX_WITHCACHE_URL` (set by `bty-lab init` to the in-stack
`http://withcache:8081`), so the bytes flow through the same LAN
cache as the flash path.

The nbdmux dashboard shows the state machine progressing
`queued -> fetching -> decompressing -> ready` with a percent-
complete bar. Once `ready` the export is live on nbdmux's NBD
listener (port 10809 by default).

## Bind a machine to ramboot

With the ref ready in nbdmux, open the machine's edit page in
bty-web and:

1. Pick the same catalog entry under "Image binding".
2. Set "Boot mode" to **ramboot**.
3. Save.

bty-web validates the binding at save time: it queries nbdmux's
`/exports`, finds the ref at `status='ready'`, and persists the
machine record. If the ref isn't in nbdmux's ready list (or nbdmux
is unreachable), the form rejects with a 422 plus an explanatory
message; warm the export in nbdmux first, then re-save the machine.

The machine row's small pill under the ramboot badge mirrors
nbdmux's status. The pill is read live from nbdmux's API on each
page render, so it reflects the current daemon state with no local
cache table.

## Boot the target

PXE-boot the target the same way you'd flash one: DHCP options
point at bty-web, the firmware fetches the iPXE script from
`/pxe/<mac>`, and bty-web responds with the `ipxe_ramboot.j2`
template iff:

- `boot_mode=ramboot` on the machine row, AND
- nbdmux URL is configured, AND
- the bound ref's nbdmux export is reported as `status='ready'` by
  the nbdmux daemon (bty polls via `nbdmux_client.list_exports`
  at plan-emit time; nbdmux owns the warm pipeline end-to-end
  since v0.65.0, so bty no longer keeps its own `ramboot_cache`
  table).

If any gate is open, the chain falls back to `bty-tui` so the
operator sees the wizard rather than the box panicking in the
initramfs. The reason lands on `/ui/events` as the
`netboot.pxe.offered` event's details.

When all gates close, the target chainloads:

```
kernel ${bty-base}/boot/bty-ramboot-init-x86_64-v<version>.vmlinuz
    boot=ramboot
    bty.nbd=tcp://<nbdmux-host>:10809
    bty.image=<ref>
    bty.overlay_size=10G
    bty.server=${bty-base}
    bty.mac=<mac>
initrd ${bty-base}/boot/bty-ramboot-init-x86_64-v<version>.initrd
boot
```

The initramfs (built into `bty-media`'s ramboot-init variant) does
the rest: modprobe nbd plus overlay, nbd-client to the nbdmux
endpoint, partprobe `/dev/nbd0`, mount the largest partition (or
the `bty.root_part=<devnode>` override if you set one) read-only,
mount a tmpfs sized to `bty.overlay_size`, overlayfs the two at
`/root`, POST `status=ramboot.up` to bty.server (best-effort), and
pivot_root + exec `/sbin/init`.

From the catalog image's perspective, nothing is unusual: it boots
its own /sbin/init, runs its own services, and the root filesystem
is read-only with copy-on-write semantics for changed blocks.

## When something goes wrong

The initramfs panics with a descriptive message on each failure
step, visible on the target's serial console / tty:

- `ramboot: nbd-client failed to connect to ...` -- network reach
  to the nbdmux host:10809 is broken, or nbdmux isn't running.
- `ramboot: could not pick a root partition on /dev/nbd0` -- the
  catalog image has no detected partition (or none look like a
  root). Override with `bty.root_part=/dev/nbd0pN` on the iPXE
  cmdline.
- `ramboot: failed to mount /dev/nbd0pN` -- the picked partition's
  filesystem driver isn't in the initramfs (only ext4 / xfs / btrfs
  are pre-loaded). Add the needed module to
  `bty-media/live-build/config/includes.chroot/etc/initramfs-tools/hooks/bty-ramboot`
  and bake a new ramboot-init release.

The initramfs also best-effort POSTs `status=ramboot.<step>_failed`
to `bty.server` before panicking, so the failure surfaces on
`/ui/events` for the operator timeline even if you don't have a
serial console attached.

If nbdmux's warmer itself fails (upstream URL 404, decompress error,
withcache unreachable), the export's status moves to `failed` on
nbdmux's dashboard with the error message in the row's `error`
field. The bty-side machine row mirrors the `failed` pill. Re-POST
to nbdmux's `/exports` (same payload) to retry; the row will reset
to `queued` and the worker restarts the pipeline.

## When to use ramboot, when to use flash

| Need | Pick |
|---|---|
| CI runs a job, throws away the result | ramboot |
| Operator wants the disk reimaged for a tenant | flash-once |
| Per-job CI cadence with disk persistence | flash-always |
| "Just run this in RAM for an hour" | ramboot |
| Boot the existing disk, change nothing | ipxe-exit (sanboot) |

ramboot does not write to disk; flash does. Pick on the durability
question, not the speed question. (The first ramboot of a given
ref takes the same wall-clock to pre-warm as a flash takes to
write; the second through Nth ramboot of the same ref is fast
because nbdmux is already serving.)
