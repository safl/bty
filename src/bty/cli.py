"""Main ``bty`` command-line entry point.

Subcommand structure:

    bty list disks
    bty list images [--image-root PATH]
    bty inspect image PATH
    bty flash --image PATH --target PATH [--provision MODE] --dry-run

Each leaf command accepts ``--json`` to emit machine-readable output.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from bty import disks, flash, formatting, images


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="bty",
        description="bty - Boot & Target Utility",
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
        print(json.dumps(rows, indent=2))
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
        print(json.dumps(rows, indent=2))
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
        print(json.dumps(info, indent=2, default=str))
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
            print(
                json.dumps(
                    {
                        "plan": plan.to_dict(),
                        "errors": errors,
                        "ok": not errors,
                    },
                    indent=2,
                    default=str,
                )
            )
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
        return 2

    if plan.provisioning_mode != "none":
        print(
            f"bty: warning: provisioning mode {plan.provisioning_mode!r} is not yet "
            "implemented (milestones 7-9); skipping post-flash provisioning",
            file=sys.stderr,
        )

    flash.print_plan(plan, errors=[])
    print()
    print(f"Writing to {plan.target.path} ...")

    try:
        flash.execute_plan(plan)
    except flash.FlashError as exc:
        print(f"bty: flash failed: {exc}", file=sys.stderr)
        return 1

    written = plan.image.virtual_size_bytes or plan.image.size_bytes
    print(f"Done. Wrote ~{written} bytes to {plan.target.path}.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
