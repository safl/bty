# Reference

Reference material for bty's surfaces. Filled in as features land.

## CLI

The `bty` command groups operations as subcommands. Each leaf command
accepts `--json` to emit machine-readable output instead of the default
human-readable table.

`bty --version` prints the installed version (sourced from package
metadata) and exits.

### JSON output envelope

Every `--json` output is wrapped:

```json
{
  "schema_version": "1",
  "command": "<subcommand-name>",
  ...command-specific fields...
}
```

Agents key off `schema_version`; incompatible structural changes bump
the version. See [`AGENTS.md`](https://github.com/safl/bty/blob/main/AGENTS.md)
for the full per-command schema reference and the exit-code table.

### Exit codes

| Code | Meaning                                                            |
|------|--------------------------------------------------------------------|
| 0    | Success.                                                           |
| 1    | Operation failed (validation rejected the plan; write subprocess returned non-zero; cloud-init / cijoe step failed). |
| 2    | Misuse — argparse error, missing required flag, missing input file. |
| 3    | Privilege required — operation needs root, rerun via `sudo`.       |
| 4    | Required external tool is not installed (e.g. `cijoe`).            |
| 5    | Target raced — block device became mounted or otherwise unsuitable between validation and write. |

### `bty list disks`

List interesting block devices on the local system. Shells out to
`lsblk -J` and projects useful columns: `path`, `size`, `tran` (bus
transport), `vendor`, `model`, `serial`, `removable`.

```text
PATH          SIZE  TRAN  VENDOR  MODEL              SERIAL          REMOVABLE
------------  ----  ----  ------  -----------------  --------------  ---------
/dev/nvme0n1  1T    nvme          Samsung 980 PRO    NVME0X000001    False
/dev/sda      500G  sata  ATA     Samsung SSD 870    S5SUNG0123456   False
```

### `bty list images [--image-root PATH]`

List supported images directly under the image root (non-recursive).
Recognised formats: `.qcow2`, `.img`, `.img.zst`.

The image root is resolved in this order:

1. The `--image-root` argument, if given.
2. The `BTY_IMAGE_ROOT` environment variable.
3. `/var/lib/bty/images` (the path the bty USB live appliance auto-mounts
   the `BTY_IMAGES` partition at).

### `bty inspect image PATH`

Print detailed metadata for a single image file. Always reports
`path`, `format`, and `size_bytes`. Adds a format-specific `detail`
block when the relevant tool succeeds:

- `.qcow2` -> `qemu-img info --output=json`
- `.img.zst` -> `zstd -l`
- `.img` -> nothing extra (raw images have no header to query)

Exit codes:

- `0` -> success
- `2` -> the path does not exist (or argparse rejected the invocation)

### `bty flash --image PATH --target PATH [--provision MODE] [--user-data PATH] [--meta-data PATH] [--cijoe-workflow PATH] [--cijoe-config PATH] [--progress {text,ndjson,none}] [--dry-run] [--yes]`

Flash an image onto a target block device.

Either `--dry-run` or `--yes` is required:

| Flags | Behaviour |
|---|---|
| `--dry-run` | Validate the plan; no writes. Exit `0` if valid, `1` if not. |
| `--yes` | Validate, then write. Requires root. |
| (neither) | Refuse with exit `2` and a hint pointing at both flags. |
| `--dry-run --yes` | `--dry-run` wins. |

#### Validation

Both modes start by validating the plan:

- Image exists and is a recognised format (`.qcow2` / `.img` / `.img.zst`).
- Image virtual size (decompressed / qcow2-virtual size, not on-disk
  size) fits the target. Skipped with a note if the virtual size
  cannot be determined (e.g. `qemu-img info` failure).
- Target exists and is a block device.
- Target has no mounted partitions (refuses to overwrite live storage).
- Provisioning mode is one of `none`, `cloud-init`, `cijoe`.

#### Write (`--yes` only)

If validation passes and `bty` is running as root, the write proceeds
in a format-specific way:

- `.img` -> `dd if=IMG of=TARGET bs=4M conv=fsync status=progress`
- `.img.zst` -> `zstd -d --stdout IMG | dd of=TARGET bs=4M conv=fsync status=progress`
- `.qcow2` -> `qemu-img convert -p -O raw IMG TARGET`

Immediately before the write, the target is re-probed and re-validated
to catch races (e.g. the target getting mounted between dry-run and
flash). On success, `bty` runs `sync` and `partprobe TARGET` so the
kernel re-reads the new partition table.

#### Provisioning

After the flash, `bty` runs the configured post-flash step:

- **`none`** — no post-flash work; the cooked image is the result.
- **`cloud-init`** — mounts the partition on the target whose rootfs
  carries `/etc/cloud/` (the unambiguous "cloud-init lives here"
  marker), writes operator-supplied `user-data` (and either supplied
  or auto-synthesised `meta-data`) under
  `/var/lib/cloud/seed/nocloud-net/` so cloud-init's NoCloud
  datasource picks them up on first boot. **Requires `--user-data
  PATH`**; rejects with exit `2` if the flag is missing. Errors
  loudly if no partition on the target appears to have cloud-init
  installed, rather than silently writing a seed nothing will read.
- **`cijoe`** — mounts the largest partition on the target (heuristic
  for the rootfs), exports `BTY_ROOTFS` pointing at the mount, then
  invokes `cijoe <workflow> --monitor [-c <config>]`. The workflow's
  tasks read or mutate the rootfs through `$BTY_ROOTFS`; bty itself
  does not interpret what they do. **Requires `--cijoe-workflow PATH`**;
  rejects with exit `2` if missing. **Requires `cijoe` on `PATH`**
  (`pipx install cijoe`); errors clearly if absent. Workflow exit
  non-zero is propagated as a flash failure.

#### Progress

`--progress {text,ndjson,none}` controls lifecycle reporting (default
`text`).

Lifecycle events: `started`, `writing`, `synced`, `partprobed`,
`provisioning` (cloud-init / cijoe steps only), `done`, `failed`.

- `text` (default) — one line per event on stderr (`[event] note`).
- `ndjson` — one JSON object per line on stdout
  (`{"event":"started","total_bytes":12345}` etc.). Use this from
  agents and CI scripts.
- `none` — no lifecycle output. Subprocess noise (`dd status=progress`)
  still goes to stderr in all modes; redirect if you want a clean
  channel.

The same callback shape (`bty.flash.ProgressCallback` /
`bty.flash.FlashProgress`) is used by `bty-tui`'s flash modal — UI
updates and CLI output share the same event stream.

#### Exit codes (specific to `bty flash`)

- `0` -> success (validation passed for `--dry-run`; write completed for `--yes`).
- `1` -> validation failed, or a write / provisioning subprocess returned non-zero.
- `2` -> argparse error, missing image, missing `--user-data` / `--cijoe-workflow`, neither `--dry-run` nor `--yes` given.
- `3` -> `--yes` was passed without root.
- `4` -> required external tool missing (e.g. `cijoe` for `--provision cijoe`).
- `5` -> target raced (became mounted or stopped being a block device between validation and write).

The general exit-code table at the top of this section applies to all
subcommands.

## Configuration

bty resolves a small set of paths and runtime knobs from the
environment and sensible defaults.

### Environment variables

| Variable          | Purpose                                                        | Default             |
|-------------------|----------------------------------------------------------------|---------------------|
| `BTY_IMAGE_ROOT`  | Image root for `bty list images` and `bty inspect image`.      | `/var/lib/bty/images` |

The CLI `--image-root` flag, when given, takes precedence over
`BTY_IMAGE_ROOT`.

### Default paths

- `/var/lib/bty/images` — image root. The USB live appliance
  auto-mounts the `BTY_IMAGES` partition here.

## Python API

bty's modules are usable as a library. Stable entry points:

| Module           | Purpose                                                   |
|------------------|-----------------------------------------------------------|
| `bty.disks`      | `list_disks() -> list[dict]` — block-device discovery.    |
| `bty.images`     | `list_images(root)`, `inspect_image(path)`, `Image` dataclass, `detect_format(path)`, `default_image_root()`. |
| `bty.formatting` | `print_table(rows, columns)`, `print_inspect(info)`.      |

A full sphinx-autodoc surface will land alongside the first non-stub
public-API consumer (likely `bty-tui` in milestone 10). Until then
treat any module not listed above as internal.

## HTTP API

`bty-web` endpoints. Populated as the server lands in milestone 12.

## Configuration schemas

Schemas for the on-disk configuration files used by `bty` and
`bty-web`. Populated alongside the relevant features.

## State export / import format

Format of the archive produced by `bty-web`'s state export, and
expected by import. Populated alongside the export/import feature.
