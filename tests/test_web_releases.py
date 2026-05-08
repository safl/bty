"""Tests for ``bty.web._releases``: GitHub release fetcher.

Each test spins up a tiny ``ThreadingHTTPServer`` serving a temporary
directory of test artifacts, then points the fetcher at that local URL
via the ``base_url`` kwarg. No network access required.
"""

from __future__ import annotations

import hashlib
import threading
from collections.abc import Iterator
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

import pytest

from bty.web._releases import (
    ALL_NAMES,
    ARTIFACT_NAMES,
    SHA256_NAME,
    FetchError,
    fetch_release,
    inspect_boot_dir,
)


@pytest.fixture
def serve_artifacts(tmp_path: Path) -> Iterator[tuple[str, Path]]:
    """Yield ``(base_url, source_dir)`` of a local HTTP server.

    The source dir is seeded with a valid manifest covering the three
    canonical artifacts. Tests can mutate it (e.g. drop a file or
    corrupt the manifest) before calling fetch_release.
    """
    src = tmp_path / "src"
    src.mkdir()
    contents: dict[str, bytes] = {}
    for name in ARTIFACT_NAMES:
        # Vary content so sha256s differ.
        payload = f"fake-{name}".encode()
        (src / name).write_bytes(payload)
        contents[name] = payload

    manifest_lines = []
    for name in ARTIFACT_NAMES:
        digest = hashlib.sha256(contents[name]).hexdigest()
        manifest_lines.append(f"{digest}  {name}")
    (src / SHA256_NAME).write_text("\n".join(manifest_lines) + "\n")

    handler_cls = type(
        "RootedHandler",
        (SimpleHTTPRequestHandler,),
        {"directory": str(src)},
    )

    def make_handler(*args, **kwargs):
        return handler_cls(*args, directory=str(src), **kwargs)

    server = HTTPServer(("127.0.0.1", 0), make_handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        yield f"http://127.0.0.1:{port}", src
    finally:
        server.shutdown()
        thread.join(timeout=2)


def test_fetch_release_round_trips(serve_artifacts: tuple[str, Path], tmp_path: Path) -> None:
    base_url, src = serve_artifacts
    boot_dir = tmp_path / "boot"

    result = fetch_release(boot_dir, base_url=base_url)

    # All artifacts (kernel, initrd, squashfs, sha256) landed.
    for name in ALL_NAMES:
        assert (boot_dir / name).read_bytes() == (src / name).read_bytes(), name
    assert result.total_bytes > 0
    assert result.artifacts == ALL_NAMES


def test_fetch_release_404_propagates_as_fetch_error(
    serve_artifacts: tuple[str, Path], tmp_path: Path
) -> None:
    base_url, src = serve_artifacts
    # Drop a required file to provoke a 404.
    (src / "bty-netboot-x86_64.vmlinuz").unlink()
    boot_dir = tmp_path / "boot"

    with pytest.raises(FetchError, match="HTTP 404"):
        fetch_release(boot_dir, base_url=base_url)
    # Atomic-install promise: failure leaves boot_dir untouched.
    if boot_dir.exists():
        assert list(boot_dir.iterdir()) == []


def test_fetch_release_sha256_mismatch_raises(
    serve_artifacts: tuple[str, Path], tmp_path: Path
) -> None:
    base_url, src = serve_artifacts
    # Corrupt one file post-manifest so the digest no longer matches.
    (src / "bty-netboot-x86_64.initrd").write_bytes(b"tampered")
    boot_dir = tmp_path / "boot"

    with pytest.raises(FetchError, match="sha256 mismatch"):
        fetch_release(boot_dir, base_url=base_url)
    # No partial install survived.
    if boot_dir.exists():
        assert list(boot_dir.iterdir()) == []


def test_fetch_release_atomic_install(serve_artifacts: tuple[str, Path], tmp_path: Path) -> None:
    """Pre-existing files in boot_dir should survive a failed fetch."""
    base_url, src = serve_artifacts
    boot_dir = tmp_path / "boot"
    boot_dir.mkdir()
    sentinel = boot_dir / "bty-netboot-x86_64.vmlinuz"
    sentinel.write_bytes(b"old-kernel")

    # Cause a failure.
    (src / "bty-netboot-x86_64.squashfs").unlink()
    with pytest.raises(FetchError):
        fetch_release(boot_dir, base_url=base_url)

    assert sentinel.read_bytes() == b"old-kernel"


def test_inspect_boot_dir_reports_present_and_missing(tmp_path: Path) -> None:
    boot_dir = tmp_path / "boot"
    boot_dir.mkdir()
    (boot_dir / "bty-netboot-x86_64.vmlinuz").write_bytes(b"present")

    states = {s.name: s for s in inspect_boot_dir(boot_dir)}
    assert states["bty-netboot-x86_64.vmlinuz"].present
    assert states["bty-netboot-x86_64.vmlinuz"].size == len(b"present")
    assert states["bty-netboot-x86_64.initrd"].present is False
    assert states["bty-netboot-x86_64.initrd"].size is None
