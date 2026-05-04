# Reference

Reference material for bty's surfaces. Filled in as features land.

## CLI

The `bty` command groups operations as subcommands. Each leaf command
accepts `--json` to emit machine-readable output instead of the default
human-readable table.

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

### `bty flash --image PATH --target PATH [--provision MODE] [--dry-run] [--yes]`

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

Provisioning modes other than `none` are accepted but produce a
warning ("not yet implemented; skipping post-flash provisioning") at
this milestone. Real provisioning lands in milestones 7-9.

#### Exit codes

- `0` -> success (validation passed for `--dry-run`; write completed for `--yes`)
- `1` -> validation failed, or the write subprocess returned non-zero
- `2` -> argparse error, missing image, neither flag given, or `--yes`
  was passed without root

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
