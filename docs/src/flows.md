# Flows

The four end-to-end paths bty supports. Pick by what infrastructure
you have:

- **Direct flash** - one-off provisioning, no server, USB live stick
  with the catalog baked onto its `BTY_IMAGES` partition.
- **USB + network catalog** - USB live stick boots a target as in the
  direct path, but the catalog comes from a remote `bty-web` instance
  (commonly the `ghcr.io/safl/bty-web` Docker container on a teammate's
  workstation). Same flash mechanics, shared catalog. No PXE.
- **Interactive PXE flash** - server is up, operator picks an image
  from the `bty` wizard on first PXE contact (default for unknown MACs).
- **Server-driven PXE flash** - fleet image flashing, machines reflash
  themselves on schedule / on demand / on failure.

## Direct flash (USB live, offline)

Used for ad-hoc provisioning of a single box, with no infrastructure on
the network.

1. Operator boots the target machine from the bty USB live image
   (built by `bty-media`).
2. The live env auto-launches ``bty`` on tty1 via
   ``bty-on-tty1.service``. Without ``bty.mac=`` on the kernel
   cmdline, the wizard runs in local-only mode: scans the
   ``BTY_IMAGES`` partition + any ``.bri`` descriptors there.
3. Operator picks an image (Enter), picks a target disk (Enter),
   confirms the flash plan (y / Enter).
4. bty writes the image and reports success.
5. Operator removes the USB stick and reboots; the target boots into
   the freshly-flashed image.

The whole flow runs offline. No network, no server, no MAC registration.

## USB + network catalog (`bty --catalog SOURCE`)

A middle shape between the strictly-offline direct flash and the
PXE-driven flows. The operator boots from the same USB live stick
but points the wizard at a network-shared `bty-web` for the catalog.
Useful for a small team that wants one place to keep pre-built
images without setting up the appliance + PXE stack.

1. Someone (operator's workstation, a homelab server, a dev box)
   runs `bty-web`. The lowest-friction shape is the published
   container:

   ```bash
   docker run -d -p 8080:8080 -v bty-data:/var/lib/bty \
     ghcr.io/safl/bty-web:latest
   ```

   Pre-built images get dropped into the volume; they show up in
   the `/images` endpoint. The bare-metal `bty-server` appliance
   also serves `/images` and works identically as a catalog source
   - any `bty-web` instance does.

2. Operator boots a target from the bty USB live stick. ``bty``
   auto-launches in local-only mode (no ``--mac`` on cmdline). On
   the first stage (SELECT_CATALOG) the operator picks ``[c]
   custom`` and types ``http://<host>:8080/catalog.toml``. Or:
   relaunch the wizard with ``bty --catalog
   http://<host>:8080/catalog.toml`` from a shell on Alt+F2.

3. The wizard fetches `GET /catalog.toml` from the server, merges
   it with the local image-root, and advances to the image picker.
   Operator picks an image + a target disk, confirms. Image bytes
   stream from `GET /images/{name}` directly through `curl | dd`
   to the target disk -- no temp file.

4. On completion the operator removes the stick and reboots; the
   target boots into the freshly-flashed image. The server has no
   per-MAC record (this isn't PXE), so no follow-up state to
   manage. The operator's pick is also never reported back: the
   catalog source is a one-way data feed in this mode.

No PXE, no DHCP-proxy, no L2 broadcasts. The container can
therefore live anywhere reachable - operator's laptop, an EC2
instance, anywhere with HTTP. The cost: the operator still has
to plug in the USB stick and stand at the target.

### Sub-case: virtual USB via IP-KVM (PiKVM, JetKVM)

The "USB live stick" in step 2 above does not have to be a
physical stick. IP-KVM appliances (PiKVM, JetKVM, BMC IPMI
virtual media, vendor-specific OoB consoles) can mount the bty
`.iso.gz` artifact and expose it to the target as if it were a
USB or CD-ROM device. The target boots into the bty live env
exactly as it would from a physical stick; ``bty`` auto-launches
on tty1; the operator types ``c``, fills in
``http://<host>:8080/catalog.toml``, picks an image, picks a
target disk, flashes. The whole sequence runs through the IP-KVM
session -- no one has to be at the rack.

Practical notes:

- Use the `.iso.gz` artifact. Decompress it host-side first if
  your IP-KVM only accepts plain `.iso` (most do).
- bty's hybrid ISO works as either USB or CD-ROM; pick whichever
  your IP-KVM offers and the target's BIOS/UEFI prefers.
- Keystroke latency over IP-KVM is real; the wizard's `Enter`-
  forward / `Esc`-back UX keeps the per-step input minimal.
- The bty live env's tty1 framebuffer renders cleanly through
  every IP-KVM I have tested (PSF console fonts, no nerd-font /
  emoji dependencies). The plain-ASCII /etc/issue banner and the
  wizard's Rich panels both render identically over IP-KVM and
  locally.

This is what "bare-metal provisioning over the internet" looks
like in practice for a small fleet without PXE infrastructure: a
PiKVM at each site, a `bty-web` container somewhere with the
catalog, and an operator at home with a browser tab.

### Sub-case: Ventoy multi-ISO stick

[Ventoy](https://www.ventoy.net/) replaces the bootloader on a
USB stick with a menu that boots any `.iso` dropped onto its
data partition. bty-usb.iso works there: boot the stick, pick
`bty-usb-x86_64.iso` from the Ventoy menu, the target boots
into the bty live env exactly as if it had been `dd`'d directly.

Two ways to use Ventoy with bty:

1. **bty-usb plus a remote catalog.** Same shape as the IP-KVM
   sub-case above: ``bty`` auto-launches, the operator presses
   ``c`` and types the `bty-web` URL. Ventoy is just a different
   boot mechanism; the catalog source is unchanged.

2. **bty-usb plus images on the same Ventoy partition.** Drop
   `.img.zst` / `.qcow2` / `.img.gz` files onto the Ventoy data
   partition next to the bty ISO. After bty boots, the
   partition is still attached to the host (it's the physical
   USB stick the live env booted from). Mount it and point
   `bty` at the path via the ``BTY_IMAGE_ROOT`` env var:

   ```bash
   # On the booted bty live env's tty1 (drop to a shell first):
   mount /dev/sdaN /mnt          # Ventoy data partition
   BTY_IMAGE_ROOT=/mnt bty
   ```

   No `bty-web` server needed for this variant - same
   self-contained shape as a stock bty-usb stick, just with
   Ventoy's multi-ISO bootloader replacing the bty bootloader.

The auto-mount of `BTY_IMAGES` that a stock bty-usb stick uses
relies on the partition label; Ventoy's data partition is labeled
`Ventoy` by default, so the auto-mount does not trigger. Either
relabel that partition `BTY_IMAGES` (if you want auto-mount) or
mount it manually as in option 2.

## Interactive PXE flash (`boot_policy=tui`)

The "bty-on-a-USB but over the network" path. Default behaviour for
any MAC the server has never seen, so onboarding a new box needs
zero per-MAC configuration.

1. Operator stands up the bty server image (`dd` to disk, boot).
   Default credentials are `bty / bty` (web UI) and `odus / odus`
   (SSH); rotate with `passwd` and activate the dnsmasq proxy-DHCP
   block from the Settings page.
2. A target machine PXE-boots on the same segment for the first time.
   `bty-web` auto-discovers the MAC, creates a `Machine` record with
   `boot_policy=tui`, and serves the iPXE-tui template
   (`ipxe_tui.j2`).
3. The target chains into the bty live env with `bty.server=URL`
   + `bty.mac=MAC` on the kernel cmdline (the iPXE template
   carries nothing else; every other knob comes from the plan
   endpoint). `bty-on-tty1.service` takes over tty1 in
   place of the agetty and exec's `bty --server URL --mac MAC`.
4. **bty auto-posts the local disk inventory** to
   `POST /pxe/{mac}/inventory` on startup. The operator does not
   have to press anything for this. bty-web stores it on the
   machine row; the operator's `/ui/machines/{mac}` page now shows
   a real path / model / serial dropdown for picking a target
   disk. Then bty GETs `<server>/pxe/<mac>/plan` and sees
   ``mode=interactive`` for boot_policy=tui.
5. bty drops into the wizard with the server's catalog pre-loaded
   (`GET /catalog.toml`). The operator picks an image and a target
   disk, confirms the flash. Image bytes stream from
   `GET /images/{name}` straight through `curl | dd` to the
   target disk - no temp file, no intermediate download.
6. On success bty `POST`s `/pxe/{mac}/done` so `last_flashed_at`
   updates server-side. **The image pick itself is NOT reported
   back** -- the machine's `bty_image_ref` stays whatever it was
   (or null). For server-tracked flashes, set boot_policy=flash
   with a bound ref + serial. The next reboot chains the wizard
   again unless the operator flips `boot_policy`.

This flow is also useful for the operator who just wants a
one-off remote flash without preparing a USB stick: any unknown
MAC on the segment becomes a TUI session reachable via IPMI / serial
console.

## Server-driven PXE flash (`boot_policy=flash`)

Used for fleet-managed provisioning, where targets are reflashed on
schedule, on demand, or on failure.

1. Server appliance is already up (same setup as the interactive
   flow above).
2. The target's first PXE contact creates a `Machine` record with
   `boot_policy=tui`. The live env runs ``bty`` on tty1, which
   AUTOMATICALLY posts the box's disk inventory to
   `POST /pxe/{mac}/inventory` on startup. The operator does not
   have to push a button for this.
3. Operator assigns `MAC -> image + target_disk + boot_policy` in
   the web UI:
   - `bty_image_ref` (image binding) - picked from the catalog.
   - `target_disk_serial` (which disk to flash) - picked from the
     inventory dropdown populated in step 2.
   - `boot_policy=flash` arms the auto-flash.
4. Target machine PXE-boots; bty-web's `/pxe/{mac}` returns the
   iPXE flash chain. Cmdline carries just `bty.server` +
   `bty.mac`; iPXE chains into the bty live env served over HTTP
   by `bty-web`.
5. `bty-on-tty1.service` exec's `bty --server URL --mac MAC`.
   ``bty`` GETs `/pxe/<mac>/plan`, sees ``mode=auto`` with the
   image URL + target_disk_serial filled in, resolves the serial
   to a `/dev/...` path via lsblk, fetches the assigned image
   from `GET /images/{ref}/{name}`, runs the flash, and `POST`s
   `/pxe/{mac}/done` to update `last_flashed_at`. Then it reboots
   automatically.
6. The next reboot still chains the live env unless the operator
   flips the machine to `boot_policy=local` (or used `flash-once`,
   which flips itself - see below). Per-job CI cadences that want
   every boot to reflash leave the policy on `flash`.
7. First-boot bring-up (users, network, packages, hostnames) is the
   pre-built image's job, baked in via cloud-init / NoCloud user-data
   at image-build time. bty has no online provisioning step.

Both BIOS and UEFI clients are supported via iPXE.

## Machine state model

Every machine record on bty-web carries five operator-controlled
fields plus three timestamps the server maintains:

| Field                | Meaning                                                                 |
|----------------------|--------------------------------------------------------------------------|
| `bty_image_ref`      | sha256 of canonicalised catalog `src`. Stable provenance ID; binds the image to flash. |
| `hostname`           | RFC-1123 hostname (optional). Cosmetic; not consumed by the flash chain. |
| `boot_policy`        | One of `local` / `flash` / `flash-once` / `tui` (default `local`).      |
| `target_disk_serial` | Operator-picked serial number from the most recent inventory post.       |
| `known_disks`        | JSON array of disks the live env's ``bty`` reported on startup.          |
| `last_seen_at`       | Updated on every `GET /pxe/{mac}` hit.                                   |
| `last_flashed_at`    | Updated on every `POST /pxe/{mac}/done`.                                 |
| `known_disks_at`     | Updated on every `POST /pxe/{mac}/inventory`.                            |

The `boot_policy` is the primary control knob; the rest provide the
parameters the policy needs.

### `boot_policy` values

| Policy        | What `GET /pxe/{mac}` returns                                                                                    | Auto-flip on `pxe_done`? |
|---------------|-----------------------------------------------------------------------------------------------------------------|--------------------------|
| `local`       | `ipxe.j2` (sanboot - boot from local disk).                                                                      | No.                      |
| `flash`       | `ipxe_flash.j2` (auto-reflash via live env). Refuses (falls back to `ipxe.j2`) if no `target_disk_serial`.        | No. Stays `flash`.       |
| `flash-once`  | Same chain as `flash`. Same `target_disk_serial` gate.                                                            | Yes. Flips to `local`.   |
| `tui`         | `ipxe_tui.j2` (live env chain; ``bty`` on tty1 GETs /pxe/<mac>/plan -> ``mode=interactive``, drops into wizard). ``bty`` auto-posts inventory on startup. | No.                      |

The flash-once policy is the "I want this box reimaged now, then
leave it alone" pattern. It's distinct from `flash` (which reimages
every PXE boot, the per-job CI cadence) and from manually flipping
`flash` -> `local` after one reimage (which is operator-error-prone).

## Inventory + safety-gate flow

The target_disk_serial gate exists to prevent "wrong disk wiped"
incidents on multi-disk hosts. The full picture, in event order:

1. **First contact, no inventory yet.** Operator powers on a new
   box. The firmware PXE-DHCPs, gets `ipxe.efi` via TFTP, runs
   the embedded chain script, fetches `/pxe-bootstrap.ipxe` from
   bty-web, chains to `/pxe/{mac}`. bty-web records the MAC
   (`machine.discovered` event), sets `boot_policy=tui`, returns
   the interactive chain (`ipxe_tui.j2`). Audit log gets a
   `pxe.offered` row with `offer_kind=tui`.
2. **Live env boots, ``bty`` starts.** ``bty`` runs on tty1; on
   startup it shells out to `lsblk` and POSTs the result to
   `/pxe/{mac}/inventory`. bty-web stores the inventory as JSON
   on the machine row, updates `known_disks_at`, records
   `machine.inventory` event. Fire-and-forget: failures land in
   the tty1 status bar but don't block the operator.
3. **Operator opens `/ui/machines/{mac}`.** The Target disk
   dropdown is now populated from `known_disks`, showing
   path / size / model / serial for each disk. The operator
   picks one + binds an image + sets `boot_policy=flash`.
4. **Operator power-cycles the target.** Next PXE contact:
   `/pxe/{mac}` sees `boot_policy=flash`, `bty_image_ref` bound,
   and `target_disk_serial` picked. Returns `ipxe_flash.j2` with
   `bty.server=` + `bty.mac=` on the cmdline (the image URL +
   target serial come from the plan endpoint, not the cmdline).
5. **Live env flashes.** ``bty`` on tty1 GETs `/pxe/<mac>/plan`,
   sees ``mode=auto`` with image + target_disk_serial filled in,
   shells out `lsblk -o SERIAL`, matches the serial to a path,
   runs the flash on that path, `POST`s `/pxe/{mac}/done`
   (audit: `machine.flashed`), reboots.

The gate fires at multiple points:

- **`/ui/machines/{mac}` POST refuses `boot_policy=flash` when
  `target_disk_serial` is empty.** The form bounces to
  `/ui/machines/{mac}?error=...` so the operator sees a flash
  banner explaining how to fix it.
- **`/pxe/{mac}` refuses the flash chain when `target_disk_serial`
  is empty.** Returns `ipxe.j2` (local fallback) and records a
  `pxe.flash.no_target_disk` event so the operator can see on
  `/ui/events` why their box isn't reflashing.
- **``bty`` in auto-flash mode refuses when the plan's serial
  doesn't match any current disk.** Prints an operator-readable
  red Panel listing the current disks and their serials, exits
  non-zero. The bty-on-tty1 service stays at the failed
  banner; the operator can re-pick on the server and try again.

The serial-match (vs path-match) at flash time is the durable
guarantee: `/dev/sda` can flip to `/dev/nvme0n1` across kernel
versions, but the disk's serial number is fixed.

## Automated event-driven transitions

bty-web triggers a small number of automated mutations in response
to incoming HTTP requests from the live env. None of them require
operator action.

### `POST /pxe/{mac}/done` (live env signals completion)

Always:

- Updates `last_flashed_at` + `updated_at`.
- Records `machine.flashed` event with the requesting IP.

If the machine's `boot_policy == "flash-once"`:

- Flips `boot_policy` to `local` in the same transaction. The next
  PXE contact returns sanboot.
- The `machine.flashed` event summary calls this out:
  `"... (flash-once -> local)"`.

### `POST /pxe/{mac}/inventory` (``bty`` reports disks)

- Replaces the entire `known_disks` JSON column with the new
  payload (no merge - the live env is authoritative for "what
  disks does this box have right now").
- Updates `known_disks_at`.
- Records `machine.inventory` event with the disk count + list of
  serials.
- 404s if the MAC has no machine record (prevents a renegade
  ``bty`` from creating ghost machines).

### `GET /pxe/{mac}` (firmware fetches the per-MAC chain)

Always:

- Inserts or updates the machine row (`machine.discovered` event
  fires on first contact; subsequent hits just touch
  `last_seen_at` + `last_seen_ip`).
- Records `pxe.offered` event with the offer kind so an operator
  can ask "what did the server hand back to MAC X at time T?"
  without enabling debug logging.

Conditional:

- `pxe.flash.no_target_disk` fires when `boot_policy=flash` /
  `flash-once` is set, an image is bound, the ref resolves, but
  `target_disk_serial` is empty. Distinct kind so the operator can
  filter for "why isn't this reflashing?" cases.
- `pxe.flash.orphan_ref` fires when `boot_policy=flash` is set and
  an image is bound but the ref has no resolvable `catalog_entries`
  row. Different failure mode from `no_target_disk`; the binding
  itself is stale.

## Audit log: event kinds by trigger

| Kind                            | Fires when...                                                                                              |
|---------------------------------|------------------------------------------------------------------------------------------------------------|
| `machine.discovered`            | A MAC not in `machines` hits `GET /pxe/{mac}`.                                                              |
| `machine.created`               | Operator `PUT /machines/{mac}` for a MAC not yet recorded.                                                  |
| `machine.upserted`              | Operator `PUT /machines/{mac}` for an existing record.                                                      |
| `machine.deleted`               | Operator `DELETE /machines/{mac}`.                                                                          |
| `machine.flashed`               | Live env `POST /pxe/{mac}/done`.                                                                            |
| `machine.inventory`             | Live env `POST /pxe/{mac}/inventory`.                                                                       |
| `pxe.offered`                   | Every `GET /pxe/{mac}` hit. Details record what was returned.                                              |
| `pxe.flash.orphan_ref`          | Flash chain refused due to dangling `bty_image_ref`.                                                       |
| `pxe.flash.no_target_disk`      | Flash chain refused due to empty `target_disk_serial`.                                                     |
| `image.uploaded`                | Operator `PUT /images/{name}` succeeds.                                                                     |
| `image.upload_failed`           | `PUT /images/{name}` failed (oversize, disk full, etc.).                                                   |
| `image.hashed`                  | HashManager finishes computing the sha256 for an image.                                                     |
| `image.hash_failed`             | HashManager errored on an image.                                                                            |
| `catalog.entry.added`           | Operator `POST /catalog/entries` (form or JSON) succeeds.                                                  |
| `catalog.entry.add_failed`      | sha resolve / oras resolve failed on `/catalog/entries`.                                                   |
| `catalog.entry.deleted`         | Operator `DELETE /catalog/entries`.                                                                         |
| `boot.release.fetched`          | `/ui/boot/fetch-release` (or `POST /boot/releases`) successfully pulled artefacts.                          |
| `boot.release.fetch_failed`     | Same path failed (404, sha mismatch, etc.).                                                                 |
| `settings.tftp.controlled`      | Operator `POST /ui/settings/tftp-control` succeeded.                                                        |
| `settings.tftp.control_failed`  | Same path failed (`sudo -n` denied, helper exit non-zero, etc.).                                            |
| `settings.pxe.activated`        | Legacy: operator armed the now-removed proxy-DHCP block.                                                    |
| `settings.pxe.activate_failed`  | Legacy: same path failed.                                                                                    |
| `auth.login.succeeded`          | Operator `POST /ui/login` with a valid OS password.                                                         |
| `auth.login.failed`             | Same path with PAM rejection.                                                                                |
| `auth.logout`                   | Operator `POST /ui/logout` from an authed session.                                                          |

Every row carries `subject_kind` (`machine` / `image` / `catalog` /
`boot` / `settings` / `auth`), a `subject_id`, the requesting
`source_ip`, the `actor` (`operator` / `pxe-client` / `system`),
and a JSON `details` blob with kind-specific extras.

## Operator UI actions: a quick map

| Action                                    | UI path                              | What happens server-side                                                                           |
|-------------------------------------------|--------------------------------------|----------------------------------------------------------------------------------------------------|
| Log in                                    | `POST /ui/login`                     | PAM authenticate -> session cookie. Records `auth.login.{succeeded,failed}`.                       |
| Log out                                   | `POST /ui/logout`                    | Clears session cookie. Records `auth.logout`.                                                       |
| Bind image + disk + policy on a machine   | `POST /ui/machines/{mac}`            | UPSERT. Refuses `boot_policy=flash` without `target_disk_serial`. Records `machine.{created,upserted}`. |
| Delete a machine record                   | `POST /ui/machines/{mac}/delete`     | DELETE row. Records `machine.deleted`.                                                              |
| Add catalog entry by URL                  | `POST /ui/catalog/entries`           | sha-resolve (if `sha_url` given) -> INSERT `catalog_entries`. Records `catalog.entry.{added,add_failed}`. |
| Delete a catalog entry                    | `DELETE /catalog/entries?src=...`    | Removes the row; image cache is left in place. Records `catalog.entry.deleted`.                     |
| Upload a `catalog.toml` manifest          | `POST /ui/catalog/upload`            | Validates + atomic-renames into `${BTY_STATE_DIR}/catalog.toml`. Reloads DownloadManager.            |
| Fetch `catalog.toml` from the project release | `POST /ui/catalog/fetch-release` | Pulls `releases/latest/download/catalog.toml`, same persist + reload as upload.                     |
| Upload an image                           | `PUT /images/{name}` (XHR from form) | Streams into `BTY_IMAGES`. Auto-enqueues sha256 hash. Records `image.{uploaded,upload_failed}`.     |
| Hash an unhashed image                    | `POST /catalog/hashes`               | Enqueues a HashManager job. Records `image.{hashed,hash_failed}` on completion.                      |
| Fetch a catalog image                     | `POST /catalog/downloads`            | Enqueues a DownloadManager job.                                                                      |
| Fetch boot artefacts (kernel + initrd + squashfs) | `POST /ui/boot/fetch-release` | Pulls release artefacts into `BTY_BOOT_ROOT`. Records `boot.release.{fetched,fetch_failed}`.        |
| Start / Stop / Restart the TFTP daemon    | `POST /ui/settings/tftp-control`     | `sudo bty-web-tftp <action>` -> `systemctl <action> dnsmasq`. Records `settings.tftp.{controlled,control_failed}`. |

## Safety gates summary

The places bty-web refuses to do what the operator asked, and what
the operator sees:

| Gate                                                  | Trigger condition                                                  | Where it fires                          | Operator surface                                   |
|-------------------------------------------------------|--------------------------------------------------------------------|-----------------------------------------|----------------------------------------------------|
| Refuse flash chain without `target_disk_serial`        | `boot_policy=flash`/`flash-once`, image bound, target empty.        | `GET /pxe/{mac}`                        | `pxe.flash.no_target_disk` event; ipxe.j2 sanboot. |
| Refuse `boot_policy=flash` upsert without target       | Form posts `boot_policy=flash` and `target_disk_serial=""`.         | `POST /ui/machines/{mac}`               | 303 to `/ui/machines/{mac}?error=...` flash banner. |
| Refuse flash on serial mismatch at boot time           | Live env can't find a current disk whose serial matches the plan's `target_disk_serial`. | `bty` auto-flash on tty1 (live env)     | `bty` prints a red "No matching disk" Panel + non-zero exit; bty-on-tty1.service stays at the failed banner. |
| Refuse oversize catalog upload                         | `/ui/catalog/upload` body > 1 MiB.                                  | `POST /ui/catalog/upload`               | 303 with `?error=...exceeded...`.                  |
| Refuse oversize image upload                           | `PUT /images/{name}` body > `BTY_MAX_UPLOAD_BYTES` (200 GiB).      | `PUT /images/{name}`                    | 413 Content Too Large; `image.upload_failed`.       |
| Refuse non-TOML catalog upload                         | Filename extension not `.toml`/`.tml` OR TOML parse fails.          | `POST /ui/catalog/upload`               | 303 with `?error=...` flash. On-disk manifest preserved on parse failure. |
| Refuse non-2xx catalog fetch-release body             | HTTPError 404, URLError, TimeoutError, or non-TOML body.            | `POST /ui/catalog/fetch-release`        | 303 with `?error=...`.                              |
| Refuse PAM-rejected login                              | `pamela.authenticate` raises PAMError.                              | `POST /ui/login`                        | Login form re-rendered with `Invalid password for ...`. |
| Refuse unknown `boot_policy`                           | Pydantic pattern check on `BOOT_POLICIES`.                          | `PUT /machines/{mac}` + form sibling   | 422 (JSON) / 303 with flash (form).                |
| Refuse path-traversal in upload `{name}`               | `..%2F` or `..` segments in `PUT /images/{name}` / `PUT /boot/{name}`. | `_safe_path` boundary check         | 400 / 404 / 405 depending on the request shape.     |
