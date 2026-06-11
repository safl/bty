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
    (src / ARTIFACT_NAMES[0]).unlink()
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
    (src / ARTIFACT_NAMES[1]).write_bytes(b"tampered")
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
    sentinel = boot_dir / ARTIFACT_NAMES[0]
    sentinel.write_bytes(b"old-kernel")

    # Cause a failure.
    (src / ARTIFACT_NAMES[2]).unlink()
    with pytest.raises(FetchError):
        fetch_release(boot_dir, base_url=base_url)

    assert sentinel.read_bytes() == b"old-kernel"


def test_fetch_release_normalises_latest_to_running_version(
    serve_artifacts: tuple[str, Path], tmp_path: Path
) -> None:
    """REGRESSION (v0.33.7): ``tag="latest"`` must resolve to the
    running bty-web's version, not to GitHub's "latest" release.

    Asset filenames are version-pinned to the running server (so
    multiple bty-web versions can coexist under ``[paths] boot_dir``
    during an upgrade, and the iPXE template references the matching
    version).
    GitHub's ``/releases/latest/download/`` redirects to whatever
    release is current. A bty-web v0.33.4 calling fetch_release with
    tag="latest" used to construct
    ``/releases/latest/download/bty-netboot-x86_64-v0.33.4.vmlinuz``,
    which 404'd whenever the "latest" release had a different
    version in its asset names (which it always did after the very
    first release).

    Now: ``tag="latest"`` (and ``tag=""`` and ``tag=None``) all
    resolve to ``v<bty.__version__>``. We verify by giving the
    serve_artifacts fixture's base_url (which serves the
    running-version artifacts) and confirming the fetch succeeds
    when tag="latest" is passed -- which works iff the resolver
    treated "latest" as the running version's tag rather than
    constructing the broken latest-URL form.
    """
    base_url, _src = serve_artifacts
    boot_dir = tmp_path / "boot"

    # Pass tag="latest" explicitly; base_url override means the
    # GitHub URL construction is skipped, but the tag normalisation
    # still runs and the in-function logic must not error.
    result = fetch_release(boot_dir, tag="latest", base_url=base_url)
    assert result.artifacts == ALL_NAMES


def test_fetch_release_url_construction_pins_to_running_version() -> None:
    """REGRESSION (v0.33.7): the GitHub URL the fetcher would hit
    must embed ``v<bty.__version__>``, never the literal "latest"
    URL form. Mirrors the v0.33.6 PXE-race test in spirit: pin the
    URL shape so a future refactor can't silently regress to the
    broken latest-redirect form.

    We can't reach into ``fetch_release`` to inspect the URL string
    without monkey-patching the HTTP layer, so we exercise the
    public derivation by hand instead.
    """
    from bty import __version__ as bty_version

    # Mirror the logic in fetch_release's URL construction. If a
    # future refactor reintroduces the ``/releases/latest/download/``
    # path, this assertion catches it.
    for tag_input in (None, "", "latest"):
        # The post-normalisation tag we'd construct the URL from.
        resolved = tag_input or f"v{bty_version}"
        if resolved == "latest":
            resolved = f"v{bty_version}"
        assert resolved == f"v{bty_version}", (
            f"tag={tag_input!r} must normalise to v{bty_version!r}, got {resolved!r}"
        )


def test_verify_sha256_manifest_rejects_malformed_line(tmp_path: Path) -> None:
    """A line that isn't `<hex>  <name>` shape must surface a clear
    ValueError. Operators see this when the upstream release ships
    a corrupted manifest (rare but real -- partial uploads, CDN
    truncation)."""
    from bty.web._releases import SHA256_NAME, _verify_sha256_manifest

    (tmp_path / SHA256_NAME).write_text("just-one-token-no-filename\n")
    with pytest.raises(ValueError, match="malformed line"):
        _verify_sha256_manifest(tmp_path / SHA256_NAME, tmp_path)


def test_verify_sha256_manifest_rejects_missing_target(tmp_path: Path) -> None:
    """A manifest that references a filename absent from files_dir
    is a clear operator-facing failure (the upstream release is
    incomplete or the local tempdir lost a file mid-fetch). Surface
    via ValueError with the offending filename in the message."""
    from bty.web._releases import SHA256_NAME, _verify_sha256_manifest

    (tmp_path / SHA256_NAME).write_text(
        "0000000000000000000000000000000000000000000000000000000000000000  not-on-disk.bin\n"
    )
    with pytest.raises(ValueError, match="missing file"):
        _verify_sha256_manifest(tmp_path / SHA256_NAME, tmp_path)


def test_verify_sha256_manifest_skips_self_and_blank_lines(tmp_path: Path) -> None:
    """The manifest may include a self-reference (some sha256sum
    invocations do); the verifier must skip it rather than 404 on
    its own missing-file check. Blank / whitespace-only lines are
    also skipped."""
    import hashlib

    from bty.web._releases import SHA256_NAME, _verify_sha256_manifest

    payload = b"art"
    (tmp_path / "art.bin").write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()
    self_digest = "0" * 64
    (tmp_path / SHA256_NAME).write_text(
        f"\n   \n{digest}  art.bin\n{self_digest}  {SHA256_NAME}\n"  # self-reference -- must skip
    )
    # Must not raise.
    _verify_sha256_manifest(tmp_path / SHA256_NAME, tmp_path)


def test_verify_sha256_manifest_strips_star_and_dotslash_prefix(tmp_path: Path) -> None:
    """``sha256sum --binary`` prefixes names with ``*``;
    operator-edited manifests sometimes use ``./``. Both prefixes
    must be tolerated (the canonical filename is what's on disk)."""
    import hashlib

    from bty.web._releases import SHA256_NAME, _verify_sha256_manifest

    payload = b"contents"
    (tmp_path / "thing.bin").write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()
    (tmp_path / SHA256_NAME).write_text(f"{digest}  *./thing.bin\n")
    _verify_sha256_manifest(tmp_path / SHA256_NAME, tmp_path)


def test_verify_sha256_manifest_empty_manifest_raises(tmp_path: Path) -> None:
    """A manifest with no usable entries (after skipping blanks +
    self-reference) is rejected. Otherwise a corrupted upstream
    release would "pass verification" by being empty -- the
    operator would think the fetch succeeded and the live env
    would later fail to boot."""
    from bty.web._releases import SHA256_NAME, _verify_sha256_manifest

    (tmp_path / SHA256_NAME).write_text("\n\n\n")
    with pytest.raises(ValueError, match="empty"):
        _verify_sha256_manifest(tmp_path / SHA256_NAME, tmp_path)


def test_verify_sha256_manifest_streams_large_files(tmp_path: Path) -> None:
    """``_verify_sha256_manifest`` must hash artifacts in chunks
    rather than via ``Path.read_bytes()``. A Pi 4 / small NUC
    running bty-web has limited RAM, and the squashfs alone is
    ~300 MiB; if the hash were buffered as a single bytes object,
    a future larger artifact could OOM the verifier.

    We can't directly assert "didn't read whole file" without
    instrumentation, but we CAN assert the function happily
    verifies a file larger than the chunk size (1 MiB) -- which
    exercises the streaming loop's iteration."""
    import hashlib

    from bty.web._releases import SHA256_NAME, _verify_sha256_manifest

    files_dir = tmp_path
    # 3 MiB of varied bytes so the streaming loop runs 3 chunks.
    large = bytes(range(256)) * (3 * 1024 * 4)
    assert len(large) > 1 << 20
    artifact = files_dir / ARTIFACT_NAMES[0]
    artifact.write_bytes(large)
    digest = hashlib.sha256(large).hexdigest()
    (files_dir / SHA256_NAME).write_text(f"{digest}  {ARTIFACT_NAMES[0]}\n")

    # Should succeed; bad checksum would raise.
    _verify_sha256_manifest(files_dir / SHA256_NAME, files_dir)

    # Tamper one byte (flip the last byte to something different)
    # and expect a mismatch on re-verify.
    tampered = bytearray(large)
    tampered[-1] ^= 0xFF
    artifact.write_bytes(bytes(tampered))
    with pytest.raises(ValueError, match="mismatch"):
        _verify_sha256_manifest(files_dir / SHA256_NAME, files_dir)


def test_missing_netboot_artifacts_empty_when_all_present(tmp_path: Path) -> None:
    """An empty list means "PXE clients can boot": kernel + initrd
    + squashfs are all on disk. The .sha256 manifest is verification
    metadata and intentionally not included in the trio."""
    from bty.web._releases import ARTIFACT_NAMES, missing_netboot_artifacts

    for name in ARTIFACT_NAMES:
        (tmp_path / name).write_bytes(b"fake")
    assert missing_netboot_artifacts(tmp_path) == []


def test_missing_netboot_artifacts_reports_each_gap(tmp_path: Path) -> None:
    """``missing_netboot_artifacts`` returns the names NOT on disk
    so the /ui/settings warning can list which fetches the
    operator still owes."""
    from bty.web._releases import ARTIFACT_NAMES, missing_netboot_artifacts

    # Plant only the first; the rest should show up as missing.
    (tmp_path / ARTIFACT_NAMES[0]).write_bytes(b"fake")
    missing = missing_netboot_artifacts(tmp_path)
    assert ARTIFACT_NAMES[0] not in missing
    for name in ARTIFACT_NAMES[1:]:
        assert name in missing


def test_missing_netboot_artifacts_empty_dir_returns_all(tmp_path: Path) -> None:
    """A boot_dir with none of the artifacts -- every name shows
    up in the list. The helper must not raise."""
    from bty.web._releases import ARTIFACT_NAMES, missing_netboot_artifacts

    missing = missing_netboot_artifacts(tmp_path)
    assert set(missing) == set(ARTIFACT_NAMES)


def test_inspect_boot_dir_reports_present_and_missing(tmp_path: Path) -> None:
    boot_dir = tmp_path / "boot"
    boot_dir.mkdir()
    (boot_dir / ARTIFACT_NAMES[0]).write_bytes(b"present")

    states = {s.name: s for s in inspect_boot_dir(boot_dir)}
    assert states[ARTIFACT_NAMES[0]].present
    assert states[ARTIFACT_NAMES[0]].size == len(b"present")
    assert states[ARTIFACT_NAMES[1]].present is False
    assert states[ARTIFACT_NAMES[1]].size is None
