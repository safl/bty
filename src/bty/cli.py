"""Main ``bty`` command-line entry point.

Subcommand structure:

    bty list disks
    bty list images [--image-root PATH]
    bty inspect image PATH
    bty flash --image PATH --target PATH [--provision MODE] --dry-run

Each leaf command accepts ``--json`` to emit machine-readable output.

JSON outputs are envelope-wrapped with a stable schema:

    {
      "schema_version": "1",
      "command": "<subcommand-name>",
      ...command-specific fields...
    }

Agents key off ``schema_version`` and the per-command keys; the format
does not change without bumping ``SCHEMA_VERSION``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from bty import disks, flash, formatting, images

# Bump this when any --json output structure changes incompatibly.
# Document the new shape in docs/src/reference.md and AGENTS.md.
SCHEMA_VERSION = "1"


def _envelope(command: str, **fields: Any) -> dict[str, Any]:
    """Wrap command-specific JSON output in the stable envelope."""
    return {"schema_version": SCHEMA_VERSION, "command": command, **fields}


def main(argv: list[str] | None = None) -> int:
    import bty as _bty  # avoid a top-level import cycle while keeping a single source

    parser = argparse.ArgumentParser(
        prog="bty",
        description="bty - Boot & Target Utility",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"bty {_bty.__version__}",
    )
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--json",
        action="store_true",
        help="emit machine-readable JSON instead of a human-readable table",
    )

    p_list = sub.add_parser("list", help="list things")
    list_sub = p_list.add_subparsers(dest="list_what", required=True, metavar="THING")

    p_list_disks = list_sub.add_parser(
        "disks",
        parents=[common],
        help="list block devices on the local system",
    )
    p_list_disks.set_defaults(func=cmd_list_disks)

    p_list_images = list_sub.add_parser(
        "images",
        parents=[common],
        help="list supported images under the image root",
    )
    p_list_images.add_argument(
        "--image-root",
        type=Path,
        default=images.default_image_root(),
        help="directory containing image files (default: $BTY_IMAGE_ROOT or %(default)s)",
    )
    p_list_images.set_defaults(func=cmd_list_images)

    p_inspect = sub.add_parser("inspect", help="inspect things in detail")
    inspect_sub = p_inspect.add_subparsers(dest="inspect_what", required=True, metavar="THING")

    p_inspect_image = inspect_sub.add_parser(
        "image",
        parents=[common],
        help="inspect an image file",
    )
    p_inspect_image.add_argument("path", type=Path, help="path to the image file")
    p_inspect_image.set_defaults(func=cmd_inspect_image)

    p_flash = sub.add_parser(
        "flash",
        parents=[common],
        help="flash an image to a target disk",
    )
    p_flash.add_argument("--image", type=Path, required=True, help="image file to flash")
    p_flash.add_argument("--target", type=Path, required=True, help="target block device")
    p_flash.add_argument(
        "--provision",
        choices=flash.PROVISIONING_MODES,
        default="none",
        help="post-flash provisioning mode (default: %(default)s)",
    )
    p_flash.add_argument(
        "--dry-run",
        action="store_true",
        help="validate the plan without writing to the target",
    )
    p_flash.add_argument(
        "--yes",
        action="store_true",
        help="confirm the destructive write; required to actually flash the target",
    )
    p_flash.add_argument(
        "--user-data",
        type=Path,
        default=None,
        help="cloud-init user-data file (required when --provision cloud-init)",
    )
    p_flash.add_argument(
        "--meta-data",
        type=Path,
        default=None,
        help="cloud-init meta-data file (optional; synthesised if omitted)",
    )
    p_flash.add_argument(
        "--cijoe-workflow",
        type=Path,
        default=None,
        help="cijoe workflow YAML (required when --provision cijoe)",
    )
    p_flash.add_argument(
        "--cijoe-config",
        type=Path,
        default=None,
        help="cijoe TOML config (optional; passed through as -c)",
    )
    p_flash.set_defaults(func=cmd_flash)

    args = parser.parse_args(argv)
    func = getattr(args, "func", None)
    if func is None:
        parser.print_help()
        return 2
    result = func(args)
    return 0 if result is None else int(result)


def cmd_list_disks(args: argparse.Namespace) -> int:
    rows = disks.list_disks()
    if args.json:
        print(json.dumps(_envelope("list-disks", disks=rows), indent=2))
    else:
        formatting.print_table(
            rows,
            columns=[
                "path",
                "size",
                "tran",
                "vendor",
                "model",
                "serial",
                "removable",
            ],
        )
    return 0


def cmd_list_images(args: argparse.Namespace) -> int:
    found = images.list_images(args.image_root)
    rows = [img.to_dict() for img in found]
    if args.json:
        payload = _envelope(
            "list-images",
            image_root=str(args.image_root),
            images=rows,
        )
        print(json.dumps(payload, indent=2))
    else:
        formatting.print_table(
            rows,
            columns=["name", "format", "size_bytes"],
        )
    return 0


def cmd_inspect_image(args: argparse.Namespace) -> int:
    try:
        info = images.inspect_image(args.path)
    except FileNotFoundError:
        print(f"bty: no such image: {args.path}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(_envelope("inspect-image", image=info), indent=2, default=str))
    else:
        formatting.print_inspect(info)
    return 0


def cmd_flash(args: argparse.Namespace) -> int:
    if not args.dry_run and not args.yes:
        print(
            "bty: pass --dry-run to validate or --yes to actually flash the target",
            file=sys.stderr,
        )
        return 2

    if args.provision == "cloud-init" and args.user_data is None:
        print(
            "bty: --user-data is required when --provision cloud-init",
            file=sys.stderr,
        )
        return 2

    if args.provision == "cijoe" and args.cijoe_workflow is None:
        print(
            "bty: --cijoe-workflow is required when --provision cijoe",
            file=sys.stderr,
        )
        return 2

    try:
        image_info = flash.probe_image(args.image)
    except FileNotFoundError as exc:
        print(f"bty: {exc}", file=sys.stderr)
        return 2

    target_info = flash.probe_target(args.target)
    plan = flash.make_plan(image_info, target_info, args.provision)
    errors = flash.validate_plan(plan)

    # --dry-run wins over --yes if both were given.
    if args.dry_run:
        if args.json:
            payload = _envelope(
                "flash",
                dry_run=True,
                plan=plan.to_dict(),
                errors=errors,
                ok=not errors,
            )
            print(json.dumps(payload, indent=2, default=str))
        else:
            flash.print_plan(plan, errors)
        return 0 if not errors else 1

    # --yes path: actually write.
    if errors:
        flash.print_plan(plan, errors)
        return 1

    if os.geteuid() != 0:
        print(
            "bty: flash requires root to write to a block device; rerun with sudo",
            file=sys.stderr,
        )
        return 3

    flash.print_plan(plan, errors=[])
    print()
    print(f"Writing to {plan.target.path} ...")

    try:
        flash.execute_plan(plan)
    except flash.FlashRaceError as exc:
        print(f"bty: flash aborted: {exc}", file=sys.stderr)
        return 5
    except flash.FlashDependencyError as exc:
        print(f"bty: flash aborted: {exc}", file=sys.stderr)
        return 4
    except flash.FlashError as exc:
        print(f"bty: flash failed: {exc}", file=sys.stderr)
        return 1

    if plan.provisioning_mode == "cloud-init":
        print(f"Applying cloud-init seed to {plan.target.path} ...")
        try:
            flash.apply_cloud_init(
                plan.target.path,
                args.user_data,
                args.meta_data,
            )
        except flash.FlashDependencyError as exc:
            print(f"bty: cloud-init seeding failed: {exc}", file=sys.stderr)
            return 4
        except flash.FlashError as exc:
            print(f"bty: cloud-init seeding failed: {exc}", file=sys.stderr)
            return 1

    if plan.provisioning_mode == "cijoe":
        print(f"Running cijoe workflow against {plan.target.path} ...")
        try:
            flash.apply_cijoe(
                plan.target.path,
                args.cijoe_workflow,
                args.cijoe_config,
            )
        except flash.FlashDependencyError as exc:
            print(f"bty: cijoe provisioning failed: {exc}", file=sys.stderr)
            return 4
        except flash.FlashError as exc:
            print(f"bty: cijoe provisioning failed: {exc}", file=sys.stderr)
            return 1

    written = plan.image.virtual_size_bytes or plan.image.size_bytes
    print(f"Done. Wrote ~{written} bytes to {plan.target.path}.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
