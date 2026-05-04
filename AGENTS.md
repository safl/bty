# AGENTS.md

This file describes the parts of `bty` that are stable surface for
automated agents (LLM tool-callers, scripts, CI runners). It complements
[`PLAN.md`](PLAN.md) (project roadmap) and the user-facing
[documentation](docs/).

## Scope of stability

- The CLI surface (`bty`, `bty-tui`, `bty-web` console scripts, their
  flags, exit codes, and `--json` output schemas) is stable within a
  given `schema_version`.
- The Python API exposed by `bty` (the modules listed under
  *Reference > Python API* in the docs) is stable within a given
  `bty.__version__` minor release.
- Internal modules (anything starting with `_`, e.g. `bty.tui._app`)
  are not stable and may change without notice.

## What every JSON output looks like

Every `--json` output is wrapped in a stable envelope:

```json
{
  "schema_version": "1",
  "command": "<subcommand-name>",
  ...command-specific fields...
}
```

Agents key off `schema_version` and the per-command keys. The format
does not change without bumping `SCHEMA_VERSION` in `bty.cli`. Any
incompatible structural change increments the version.

### Per-command schemas

`bty list disks --json`

```json
{
  "schema_version": "1",
  "command": "list-disks",
  "disks": [
    {
      "path": "/dev/sda",
      "size": "500G",
      "type": "disk",
      "vendor": "ATA",
      "model": "Samsung SSD 870",
      "serial": "S5SUNG0123456",
      "tran": "sata",
      "removable": false,
      "readonly": false,
      "mountpoints": []
    }
  ]
}
```

`bty list images --json`

```json
{
  "schema_version": "1",
  "command": "list-images",
  "image_root": "/var/lib/bty/images",
  "images": [
    {
      "name": "debian.qcow2",
      "path": "/var/lib/bty/images/debian.qcow2",
      "format": "qcow2",
      "size_bytes": 268435456
    }
  ]
}
```

`bty inspect image PATH --json`

```json
{
  "schema_version": "1",
  "command": "inspect-image",
  "image": {
    "path": "/var/lib/bty/images/debian.qcow2",
    "format": "qcow2",
    "size_bytes": 268435456,
    "detail": { ... format-specific tool output ... }
  }
}
```

`bty flash --dry-run --json`

```json
{
  "schema_version": "1",
  "command": "flash",
  "dry_run": true,
  "ok": false,
  "errors": ["target is not a block device: /dev/null"],
  "plan": {
    "image": { ... },
    "target": { ... },
    "provisioning_mode": "none",
    "notes": []
  }
}
```

## Exit codes

| Code | Meaning                                                            |
|------|--------------------------------------------------------------------|
| 0    | Success.                                                           |
| 1    | Operation failed (validation rejected the plan; subprocess returned non-zero; cloud-init / cijoe step failed). |
| 2    | Misuse — argparse error, missing required flag, missing input file (e.g. `--user-data` not on disk). |
| 3    | Privilege required — operation needs root, run via `sudo`.         |
| 4    | Required external tool is not installed (e.g. `cijoe` missing).    |
| 5    | Target raced — block device became mounted or disappeared between validation and write. |

Agents should treat `0` as success and any other code as failure. Use
the specific code to decide whether retry is meaningful (e.g. retry
on `5` after re-probing; do not retry on `3` or `4`).

## Conventions agents can rely on

- **No interactive prompts.** Destructive operations require `--yes`.
  Validation-only runs require `--dry-run`. Without one of those flags
  `bty flash` exits 2.
- **stderr for human-readable errors and notes; stdout for results
  (text or JSON).**
- **`bty --version`** prints `bty <version>` (sourced from package
  metadata) and exits 0.
- **`bty --help`** and `bty <subcommand> --help` document the surface;
  argparse's standard help output.
- **Idempotent reads.** `bty list ...` and `bty inspect ...` have no
  side effects; safe to call repeatedly.

## Don'ts

- Don't parse human-readable table output. Use `--json`.
- Don't depend on stderr message wording — only on exit codes.
- Don't depend on internal module paths (`bty.tui._app`,
  `bty.flash._partition_has_cloud_init`, etc.). They are private.
- Don't expect bty to write files outside the configured image root,
  the target block device, or the bty configuration / state areas.

## Where to look next

- [`PLAN.md`](PLAN.md) — roadmap, motivation, OS scope.
- [`docs/src/reference.md`](docs/src/reference.md) — full CLI
  reference and configuration.
- [`docs/src/quickstart.md`](docs/src/quickstart.md) — operator
  walk-through.
