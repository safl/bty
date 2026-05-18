"""Main ``bty`` command-line entry point.

Subcommand structure:

    bty images [--image-root PATH | --catalog SOURCE]
    bty inspect PATH
    bty flash IMAGE TARGET [--dry-run | --yes]
    bty tui [--catalog SOURCE --mac MAC]
    bty catalog ACTION ...

Each subcommand except ``tui`` accepts ``--json`` to emit machine-
readable output (``tui`` is a forwarder to the textual app; its
output is the interactive terminal session itself).

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
from collections.abc import Callable
from pathlib import Path
from typing import Any

from bty import catalog, flash, formatting, images

# Bump this when any --json output structure changes incompatibly.
# Document the new shape in docs/src/reference.md and AGENTS.md.
SCHEMA_VERSION = "1"


def _envelope(command: str, **fields: Any) -> dict[str, Any]:
    """Wrap command-specific JSON output in the stable envelope."""
    return {"schema_version": SCHEMA_VERSION, "command": command, **fields}


def main(argv: list[str] | None = None) -> int:
    import bty as _bty  # avoid a top-level import cycle while keeping a single source

    args_list = list(argv) if argv is not None else sys.argv[1:]
    # Forward ``bty tui ...`` directly to the TUI's own argparse.
    # ``argparse.REMAINDER`` mishandles ``--flag VALUE`` positionals
    # (Python bug 17050 / 28543), so we route around the main argparse
    # for this one subcommand. Side benefit: ``bty tui --help`` shows
    # the TUI's own help.
    if args_list and args_list[0] == "tui":
        try:
            from bty.tui import main as tui_main
        except ImportError as exc:
            print(
                f"bty tui: failed to import the TUI ({exc}); "
                f"reinstall bty-lab with the ``[tui]`` extra "
                f"(e.g. ``pipx install 'bty-lab[tui]'``).",
                file=sys.stderr,
            )
            return 2
        tui_main(args_list[1:], prog="bty tui")
        return 0

    parser = argparse.ArgumentParser(
        prog="bty",
        description="bty - flash images onto target disks, offline or networked with and without PXE",
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

    p_images = sub.add_parser(
        "images",
        parents=[common],
        help="list supported images under the image root OR from a catalog source",
    )
    p_images.add_argument(
        "--image-root",
        type=Path,
        default=images.default_image_root(),
        help=(
            "directory containing image files (default: $BTY_IMAGE_ROOT or "
            "%(default)s). Ignored when --catalog is set."
        ),
    )
    p_images.add_argument(
        "--catalog",
        type=str,
        default=None,
        metavar="SOURCE",
        help=(
            "load the catalog from a TOML manifest instead of scanning a "
            "local directory. SOURCE is a file path, an ``http(s)://`` URL "
            "(e.g. a bty-web's ``/catalog.toml``), or an ``oras://`` "
            "reference (e.g. ``oras://ghcr.io/owner/bty-catalog:latest``). "
            "Same source argument shape as ``bty tui --catalog``."
        ),
    )
    p_images.set_defaults(func=cmd_images)

    # ``bty tui`` is handled at the top of ``main`` (before argparse
    # is built) because ``argparse.REMAINDER`` mishandles ``--flag``
    # positionals. The subparser exists here purely for ``bty --help``
    # listings; the actual dispatch never reaches its ``func``.
    sub.add_parser(
        "tui",
        help="launch the bty terminal UI (requires the ``[tui]`` extra)",
    )

    p_inspect = sub.add_parser(
        "inspect",
        parents=[common],
        help="inspect an image file or .bri descriptor",
    )
    p_inspect.add_argument("path", type=Path, help="path to the image or .bri file")
    p_inspect.set_defaults(func=cmd_inspect)

    p_flash = sub.add_parser(
        "flash",
        parents=[common],
        help="flash an image to a target disk",
    )
    p_flash.add_argument(
        "image",
        type=str,
        help=(
            "image to flash. One of: a local file path "
            "(``/path/to/image.img.gz``), an HTTP/HTTPS URL "
            "(``http://server/images/foo.img.gz``), an ``oras://`` "
            "reference to an OCI artefact in a container registry "
            "(``oras://ghcr.io/owner/repo:tag``; resolved through the "
            "registry's anonymous-pull flow), or a ``.bri`` descriptor "
            "path (a TOML file whose ``url`` field carries any of the "
            "above). URLs stream directly to disk for ``.img`` / "
            "``.img.{gz,zst,xz,bz2}`` and download to a temp file "
            "first for ``.qcow2``. ``bty tui --catalog`` operators get "
            "the URL from the catalog entry's ``src`` field."
        ),
    )
    p_flash.add_argument("target", type=Path, help="target block device")
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
        "--progress",
        choices=["text", "ndjson", "none"],
        default="text",
        help=(
            "progress reporting (default: 'text' to stderr; 'ndjson' emits "
            "one JSON event per line on stdout; 'none' silences lifecycle output)"
        ),
    )
    p_flash.set_defaults(func=cmd_flash)

    p_catalog = sub.add_parser(
        "catalog",
        help="manage the bty-web catalog manifest (TOML) + local SHA-verified cache",
    )
    catalog_sub = p_catalog.add_subparsers(dest="catalog_what", required=True, metavar="ACTION")

    p_catalog_validate = catalog_sub.add_parser(
        "validate",
        parents=[common],
        help="parse a manifest and report any schema / field errors",
    )
    p_catalog_validate.add_argument(
        "path",
        type=Path,
        nargs="?",
        default=None,
        help=("manifest path (default: $BTY_CATALOG_FILE or ${BTY_STATE_DIR}/catalog.toml)"),
    )
    p_catalog_validate.set_defaults(func=cmd_catalog_validate)

    p_catalog_list = catalog_sub.add_parser(
        "list",
        parents=[common],
        help="list manifest entries with cached / available status",
    )
    p_catalog_list.add_argument(
        "--manifest",
        type=Path,
        default=None,
        help="manifest path (default: env-derived; see ``catalog validate``)",
    )
    p_catalog_list.set_defaults(func=cmd_catalog_list)

    p_catalog_fetch = catalog_sub.add_parser(
        "fetch",
        parents=[common],
        help="download a manifest entry's bytes into the SHA-keyed cache",
    )
    p_catalog_fetch.add_argument(
        "name",
        help="image name as declared in the manifest",
    )
    p_catalog_fetch.add_argument(
        "--manifest",
        type=Path,
        default=None,
        help="manifest path (default: env-derived; see ``catalog validate``)",
    )
    p_catalog_fetch.set_defaults(func=cmd_catalog_fetch)

    args = parser.parse_args(argv)
    func = getattr(args, "func", None)
    if func is None:
        parser.print_help()
        return 2
    result = func(args)
    return 0 if result is None else int(result)


def cmd_images(args: argparse.Namespace) -> int:
    """List images either from a local image-root or a catalog source.

    Local mode (default): dir-scan, no catalog manifest, no content-
    hash merge. Includes ``.bri`` (bty Remote Image) descriptors as
    ``remote`` rows alongside local images. Operators who want the
    unified catalog look at the bty-web browser UI; this command
    answers "what flashable files are sitting in this directory?"
    and nothing more.

    Catalog mode (``--catalog SOURCE``): fetches and parses a TOML
    manifest from the source (path, ``http(s)://``, or ``oras://``)
    and renders its entries. Shares the same fetcher as
    ``bty tui --catalog`` (see :func:`bty.catalog.load_source`).
    """
    if args.catalog is not None:
        return _cmd_images_catalog(args)
    return _cmd_images_local(args)


def _cmd_images_local(args: argparse.Namespace) -> int:
    # Distinguish "image root doesn't exist" from "exists but empty"
    # so operators don't silently get an empty listing for a typo'd
    # ``--image-root`` path. Stderr warning preserves stdout for the
    # JSON / table consumer.
    if not args.image_root.exists():
        print(
            f"bty: image root {args.image_root} does not exist; listing empty",
            file=sys.stderr,
        )
    found = images.list_images(args.image_root)
    remotes = images.list_remote_images(args.image_root)
    local_rows = [{**img.to_dict(), "source": "local"} for img in found]
    remote_rows = [
        {
            "name": r.name,
            "format": r.format,
            "size_bytes": r.size_bytes,
            "source": "remote",
            "url": r.url,
            "path": str(r.path),
            "sha256": r.sha256,
            "description": r.description,
        }
        for r in remotes
    ]
    rows = local_rows + remote_rows
    if args.json:
        payload = _envelope(
            "images",
            image_root=str(args.image_root),
            images=rows,
        )
        print(json.dumps(payload, indent=2))
    else:
        formatting.print_table(
            rows,
            columns=["name", "format", "size_bytes", "source"],
        )
    return 0


def _cmd_images_catalog(args: argparse.Namespace) -> int:
    """``--catalog SOURCE`` path: fetch + parse a TOML manifest and
    render it. Same row shape as local mode so a script can
    ``bty images --catalog $SOURCE --json | jq ...`` without
    branching on mode."""
    import urllib.error

    try:
        parsed_catalog = catalog.load_source(args.catalog)
    except (ValueError, catalog.CatalogError) as exc:
        print(f"bty: --catalog {args.catalog!r}: {exc}", file=sys.stderr)
        return 2
    except (urllib.error.URLError, OSError) as exc:
        print(f"bty: failed to fetch catalog from {args.catalog!r}: {exc}", file=sys.stderr)
        return 2
    rows: list[dict[str, object]] = [
        {
            "name": entry.name,
            "format": entry.format,
            "size_bytes": entry.size_bytes,
            "source": "remote",
            "url": entry.src,
            "sha256": entry.sha256,
            "description": entry.description,
        }
        for entry in parsed_catalog.entries
    ]
    if args.json:
        payload_out = _envelope(
            "images",
            catalog=args.catalog,
            images=rows,
        )
        print(json.dumps(payload_out, indent=2))
    else:
        formatting.print_table(
            rows,
            columns=["name", "format", "size_bytes", "source"],
        )
    return 0


def cmd_inspect(args: argparse.Namespace) -> int:
    try:
        info = images.inspect_image(args.path)
    except FileNotFoundError:
        print(f"bty: no such image: {args.path}", file=sys.stderr)
        return 2
    except IsADirectoryError:
        print(f"bty: not a file: {args.path}", file=sys.stderr)
        return 2
    except images.BriError as exc:
        print(f"bty: malformed .bri descriptor: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(_envelope("inspect", image=info), indent=2, default=str))
    else:
        formatting.print_inspect(info)
    return 0


def cmd_flash(
    args: argparse.Namespace,
    *,
    probe_image: Callable[[Path], flash.ImageInfo] = flash.probe_image,
    probe_image_url: Callable[[str], flash.ImageInfo] = flash.probe_image_url,
    probe_target: Callable[[Path], flash.TargetInfo] = flash.probe_target,
    execute_plan: Callable[..., None] = flash.execute_plan,
    geteuid: Callable[[], int] = os.geteuid,
) -> int:
    """Drive a flash. Outside-world dependencies are kwargs with real defaults.

    Tests pass fakes directly instead of monkey-patching module-level
    references; production callers (``main()``) use the defaults and the
    real ``bty.flash`` / ``os`` machinery is invoked.

    bty is a flasher, not an image builder: first-boot bring-up
    belongs in the image builder upstream (cloud-init / NoCloud /
    whatever the operator bakes in). bty itself only writes bytes.
    """
    if not args.dry_run and not args.yes:
        print(
            "bty: pass --dry-run to validate or --yes to actually flash the target",
            file=sys.stderr,
        )
        return 2

    try:
        image_str = str(args.image)
        image_path = Path(image_str)
        # ``.bri`` (bty Remote Image) descriptor: tiny TOML file
        # whose ``url`` field is the real source. Resolve here so
        # the rest of the flash path treats it as a regular URL
        # flash with no extra branching downstream.
        if image_str.lower().endswith(images.BRI_EXTENSION) and image_path.is_file():
            image_str = images.read_bri(image_path).url
        if image_str.startswith(("http://", "https://", "oras://")):
            image_info = probe_image_url(image_str)
        else:
            image_info = probe_image(Path(image_str))
    except (FileNotFoundError, ValueError, images.BriError) as exc:
        print(f"bty: {exc}", file=sys.stderr)
        return 2

    target_info = probe_target(args.target)
    plan = flash.make_plan(image_info, target_info)
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

    if geteuid() != 0:
        print(
            "bty: flash requires root to write to a block device; rerun with sudo",
            file=sys.stderr,
        )
        return 3

    flash.print_plan(plan, errors=[])
    print()
    print(f"Writing to {plan.target.path} ...")

    progress_cb = _build_progress_callback(args.progress)

    try:
        execute_plan(plan, progress=progress_cb)
    except flash.FlashRaceError as exc:
        print(f"bty: flash aborted: {exc}", file=sys.stderr)
        return 5
    except flash.FlashError as exc:
        print(f"bty: flash failed: {exc}", file=sys.stderr)
        return 1

    # No post-flash provisioning step here. First-boot bring-up
    # belongs in the image (cloud-init / NoCloud); bty itself
    # only writes bytes.

    if progress_cb is not None:
        progress_cb(flash.FlashProgress(event="done"))

    written = plan.image.virtual_size_bytes or plan.image.size_bytes
    print(f"Done. Wrote ~{written} bytes to {plan.target.path}.")
    return 0


def _build_progress_callback(mode: str) -> flash.ProgressCallback | None:
    """Return a progress callback matching ``--progress`` mode, or ``None``."""
    if mode == "none":
        return None

    if mode == "ndjson":

        def emit(event: flash.FlashProgress) -> None:
            payload: dict[str, Any] = {"event": event.event}
            if event.note:
                payload["note"] = event.note
            if event.total_bytes is not None:
                payload["total_bytes"] = event.total_bytes
            if event.bytes_written is not None:
                payload["bytes_written"] = event.bytes_written
            print(json.dumps(payload), flush=True)

        return emit

    # default text mode
    def emit_text(event: flash.FlashProgress) -> None:
        line = f"[{event.event}]"
        if event.note:
            line += f" {event.note}"
        # writing_progress emits ~1/sec while dd is running. Show
        # bytes_written / total_bytes (if known) so the operator
        # gets live feedback. The other events fire once each so
        # one line per event is fine.
        if event.bytes_written is not None and event.total_bytes:
            pct = 100.0 * event.bytes_written / event.total_bytes
            line += f" {event.bytes_written}/{event.total_bytes} bytes ({pct:.1f}%)"
        elif event.bytes_written is not None:
            line += f" {event.bytes_written} bytes"
        elif event.total_bytes is not None:
            line += f" total_bytes={event.total_bytes}"
        print(line, file=sys.stderr, flush=True)

    return emit_text


def cmd_catalog_validate(args: argparse.Namespace) -> int:
    """Load a manifest and report any errors.

    Exit 0 on a clean parse + schema check; exit 1 on
    ``CatalogError`` with the message printed to stderr. JSON
    mode emits an envelope with ``valid`` / ``error`` so a
    script can branch.
    """
    path = args.path or catalog.default_manifest_path()
    if path is None:
        print(
            "bty: no manifest configured "
            "(set BTY_CATALOG_FILE or place ${BTY_STATE_DIR}/catalog.toml)",
            file=sys.stderr,
        )
        return 1
    try:
        cat = catalog.load(path)
    except catalog.CatalogError as exc:
        if args.json:
            print(
                json.dumps(
                    _envelope("catalog-validate", path=str(path), valid=False, error=str(exc)),
                    indent=2,
                )
            )
        else:
            print(f"bty: catalog: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(
            json.dumps(
                _envelope("catalog-validate", path=str(path), valid=True, count=len(cat)), indent=2
            )
        )
    else:
        print(f"catalog at {path}: ok ({len(cat)} entr{'y' if len(cat) == 1 else 'ies'})")
    return 0


def cmd_catalog_list(args: argparse.Namespace) -> int:
    """Print each manifest entry with its cached / available status.

    Cache lookup is by SHA only -- no re-hashing of the cached file
    on every list, since that would be expensive for multi-GiB
    images. Trust is "we wrote this under the right SHA".
    """
    path = args.manifest or catalog.default_manifest_path()
    if path is None:
        print(
            "bty: no manifest configured "
            "(set BTY_CATALOG_FILE or place ${BTY_STATE_DIR}/catalog.toml)",
            file=sys.stderr,
        )
        return 1
    try:
        cat = catalog.load(path)
    except catalog.CatalogError as exc:
        print(f"bty: catalog: {exc}", file=sys.stderr)
        return 1
    cache_dir = catalog.default_cache_dir()
    if args.json:
        rows = [
            {
                "name": e.name,
                "src": e.src,
                "sha256": e.sha256,
                "format": e.format,
                "size_bytes": e.size_bytes,
                "description": e.description,
                "cached": catalog.is_cached(e, cache_dir),
            }
            for e in cat
        ]
        print(
            json.dumps(
                _envelope(
                    "catalog-list",
                    manifest=str(path),
                    cache_dir=str(cache_dir),
                    entries=rows,
                ),
                indent=2,
            )
        )
    else:
        print(f"catalog: {path}    cache: {cache_dir}\n")
        formatting.print_table(
            [
                {
                    "Name": e.name,
                    "Format": e.format or "-",
                    "Status": "cached" if catalog.is_cached(e, cache_dir) else "available",
                    "Source": e.src,
                }
                for e in cat
            ],
            ["Name", "Format", "Status", "Source"],
        )
    return 0


def cmd_catalog_fetch(args: argparse.Namespace) -> int:
    """Download a single named entry into the cache.

    Idempotent: if the entry is already cached, prints a no-op
    note and exits 0.
    """
    path = args.manifest or catalog.default_manifest_path()
    if path is None:
        print(
            "bty: no manifest configured "
            "(set BTY_CATALOG_FILE or place ${BTY_STATE_DIR}/catalog.toml)",
            file=sys.stderr,
        )
        return 1
    try:
        cat = catalog.load(path)
    except catalog.CatalogError as exc:
        print(f"bty: catalog: {exc}", file=sys.stderr)
        return 1
    entry = cat.by_name(args.name)
    if entry is None:
        print(f"bty: catalog: no entry named {args.name!r} in {path}", file=sys.stderr)
        return 1
    cache_dir = catalog.default_cache_dir()
    try:
        cached = catalog.fetch_to_cache(entry, cache_dir)
    except catalog.CatalogError as exc:
        print(f"bty: catalog fetch: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"bty: catalog fetch: I/O error: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(
            json.dumps(
                _envelope(
                    "catalog-fetch",
                    name=entry.name,
                    sha256=entry.sha256,
                    cached_path=str(cached),
                ),
                indent=2,
            )
        )
    else:
        print(f"cached: {cached}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
