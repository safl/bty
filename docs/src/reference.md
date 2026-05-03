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

## HTTP API

`bty-web` endpoints. Populated as the server lands in milestone 12.

## Configuration schemas

Schemas for the on-disk configuration files used by `bty` and
`bty-web`. Populated alongside the relevant features.

## State export / import format

Format of the archive produced by `bty-web`'s state export, and
expected by import. Populated alongside the export/import feature.
