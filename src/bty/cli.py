"""Main ``bty`` command-line entry point.

Subcommand structure:

    bty list disks
    bty list images [--image-root PATH]
    bty inspect image PATH

Each leaf command accepts ``--json`` to emit machine-readable output.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from bty import disks, formatting, images


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


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
