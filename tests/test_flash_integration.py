"""Integration tests for ``bty.flash`` against a real loop device.

These tests actually invoke ``losetup``, ``dd``, ``zstd``, ``qemu-img``,
and ``partprobe`` - the same external tools the production code calls.
They are gated on root and the availability of those binaries because
``losetup`` and writing to ``/dev/loopN`` require privileges that
ordinary contributor checkouts won't have.

CI runs them via ``sudo -E uv run pytest -m integration`` in a
dedicated job; ``uv run pytest`` (the default) excludes them so dev
loops stay fast.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from bty import flash

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _require_integration_environment() -> None:
    """Skip when prereqs are missing locally, but fail loudly in CI.

    CI sets ``CI=true``; if integration prereqs are missing there, that is a
    misconfiguration we want to see, not silently skip. Local contributors
    without root just get a normal skip.
    """
    missing: list[str] = []
    if os.geteuid() != 0:
        missing.append("not running as root")
    if shutil.which("losetup") is None:
        missing.append("losetup not on PATH")
    if shutil.which("dd") is None:
        missing.append("dd not on PATH")

    if not missing:
        return

    reason = "; ".join(missing)
    if os.environ.get("CI"):
        pytest.fail(f"integration prerequisites missing in CI: {reason}", pytrace=False)
    pytest.skip(reason)


@pytest.fixture
def loop_device(tmp_path: Path) -> Iterator[tuple[Path, Path]]:
    """Yield ``(loop_dev_path, backing_file_path)`` and clean up after."""
    backing = tmp_path / "backing.img"
    subprocess.run(["truncate", "-s", "8M", str(backing)], check=True)

    setup = subprocess.run(
        ["losetup", "-f", "--show", str(backing)],
        capture_output=True,
        text=True,
        check=True,
    )
    loop_dev = Path(setup.stdout.strip())
    try:
        yield loop_dev, backing
    finally:
        subprocess.run(["losetup", "-d", str(loop_dev)], check=False)


def test_flash_raw_img_to_loop_device_byte_correct(
    tmp_path: Path,
    loop_device: tuple[Path, Path],
) -> None:
    loop_dev, backing = loop_device

    payload = b"BTYTEST!" * 1024  # 8 KiB of recognisable bytes
    ref = tmp_path / "ref.img"
    ref.write_bytes(payload)

    image_info = flash.probe_image(ref)
    target_info = flash.probe_target(loop_dev)
    plan = flash.make_plan(image_info, target_info)
    assert flash.validate_plan(plan) == []

    flash.execute_plan(plan)

    written = backing.read_bytes()[: len(payload)]
    assert written == payload


def test_flash_qcow2_to_loop_device_byte_correct(
    tmp_path: Path,
    loop_device: tuple[Path, Path],
) -> None:
    if shutil.which("qemu-img") is None:
        pytest.skip("qemu-img not available")
    loop_dev, backing = loop_device

    # Build a tiny qcow2 holding a known byte pattern.
    raw = tmp_path / "ref.raw"
    payload = b"QCOW2TST" * 1024  # 8 KiB
    raw.write_bytes(payload)

    qcow2 = tmp_path / "ref.qcow2"
    subprocess.run(
        ["qemu-img", "convert", "-O", "qcow2", str(raw), str(qcow2)],
        check=True,
    )

    image_info = flash.probe_image(qcow2)
    target_info = flash.probe_target(loop_dev)
    plan = flash.make_plan(image_info, target_info)
    assert flash.validate_plan(plan) == []

    flash.execute_plan(plan)

    written = backing.read_bytes()[: len(payload)]
    assert written == payload


def test_flash_zst_to_loop_device_byte_correct(
    tmp_path: Path,
    loop_device: tuple[Path, Path],
) -> None:
    if shutil.which("zstd") is None:
        pytest.skip("zstd not available")
    loop_dev, backing = loop_device

    raw = tmp_path / "ref.img"
    payload = b"ZSTDTEST" * 1024
    raw.write_bytes(payload)

    zst = tmp_path / "ref.img.zst"
    subprocess.run(["zstd", "-q", "-f", "-o", str(zst), str(raw)], check=True)

    image_info = flash.probe_image(zst)
    target_info = flash.probe_target(loop_dev)
    plan = flash.make_plan(image_info, target_info)
    assert flash.validate_plan(plan) == []

    flash.execute_plan(plan)

    written = backing.read_bytes()[: len(payload)]
    assert written == payload


# bty has no post-flash provisioning step. Image-side first-boot
# bring-up is the image builder's job (cloud-init / NoCloud);
# bty itself only writes bytes.


# ---------- Issue #10 PR1: in-pipeline integrity verification ----------------
#
# These drive the URL-streaming writers directly through the real
# ``curl | tee | sha256sum | dd`` (and ``| gzip -d |``) pipeline, with
# ``_curl_args_for_source`` stubbed to point curl at a local ``file://``
# source carrying a chosen digest. That keeps the test hermetic (no
# registry) while exercising the exact subprocess wiring production uses;
# the oras-resolve -> digest threading is covered by the unit tests.


def _file_url(p: Path) -> str:
    return "file://" + str(p)


def _sha256_ref(p: Path) -> str:
    import hashlib

    return "sha256:" + hashlib.sha256(p.read_bytes()).hexdigest()


def test_flash_img_url_verifies_matching_digest(
    tmp_path: Path,
    loop_device: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if shutil.which("curl") is None:
        pytest.skip("curl not available")
    loop_dev, backing = loop_device

    payload = b"DIGESTOK" * 1024
    ref = tmp_path / "ref.img"
    ref.write_bytes(payload)
    digest = _sha256_ref(ref)

    monkeypatch.setattr(
        flash,
        "_curl_args_for_source",
        lambda _u: (["curl", "-fsSL", _file_url(ref)], len(payload), digest),
    )

    # Correct digest: the tee|sha256sum splice agrees, so the flash
    # completes and the bytes land byte-correct.
    flash._flash_img_from_url("oras://example/ref:tag", loop_dev, total_bytes=len(payload))
    assert backing.read_bytes()[: len(payload)] == payload


def test_flash_img_url_rejects_tampered_digest(
    tmp_path: Path,
    loop_device: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if shutil.which("curl") is None:
        pytest.skip("curl not available")
    loop_dev, _backing = loop_device

    payload = b"TAMPERED" * 1024
    ref = tmp_path / "ref.img"
    ref.write_bytes(payload)
    wrong = "sha256:" + "00" * 32

    monkeypatch.setattr(
        flash,
        "_curl_args_for_source",
        lambda _u: (["curl", "-fsSL", _file_url(ref)], len(payload), wrong),
    )

    # Wrong digest: the on-wire hash disagrees -> FlashIntegrityError.
    with pytest.raises(flash.FlashIntegrityError):
        flash._flash_img_from_url("oras://example/ref:tag", loop_dev, total_bytes=len(payload))


def test_flash_img_url_no_digest_keeps_zero_copy_path(
    tmp_path: Path,
    loop_device: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if shutil.which("curl") is None:
        pytest.skip("curl not available")
    loop_dev, backing = loop_device

    payload = b"ZEROCOPY" * 1024
    ref = tmp_path / "ref.img"
    ref.write_bytes(payload)

    # digest=None -> gate closed: no tee/sha spliced, the existing
    # curl|dd path runs unchanged and writes byte-correct.
    monkeypatch.setattr(
        flash,
        "_curl_args_for_source",
        lambda _u: (["curl", "-fsSL", _file_url(ref)], len(payload), None),
    )

    flash._flash_img_from_url("https://example/ref.img", loop_dev, total_bytes=len(payload))
    assert backing.read_bytes()[: len(payload)] == payload


def test_flash_gz_url_verifies_matching_digest(
    tmp_path: Path,
    loop_device: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if shutil.which("curl") is None or shutil.which("gzip") is None:
        pytest.skip("curl / gzip not available")
    loop_dev, backing = loop_device

    payload = b"GZDIGEST" * 1024
    raw = tmp_path / "ref.img"
    raw.write_bytes(payload)
    subprocess.run(["gzip", "-k", "-f", str(raw)], check=True)
    gz = tmp_path / "ref.img.gz"
    # The digest covers the COMPRESSED blob (the splice hashes curl's
    # output, before decompression), so hash the .gz, not the raw image.
    digest = _sha256_ref(gz)

    monkeypatch.setattr(
        flash,
        "_curl_args_for_source",
        lambda _u: (["curl", "-fsSL", _file_url(gz)], None, digest),
    )

    flash._flash_gz_from_url("oras://example/ref.img.gz:tag", loop_dev, total_bytes=len(payload))
    assert backing.read_bytes()[: len(payload)] == payload


def test_flash_gz_url_rejects_tampered_digest(
    tmp_path: Path,
    loop_device: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if shutil.which("curl") is None or shutil.which("gzip") is None:
        pytest.skip("curl / gzip not available")
    loop_dev, _backing = loop_device

    payload = b"GZTAMPER" * 1024
    raw = tmp_path / "ref.img"
    raw.write_bytes(payload)
    subprocess.run(["gzip", "-k", "-f", str(raw)], check=True)
    gz = tmp_path / "ref.img.gz"
    wrong = "sha256:" + "ff" * 32

    monkeypatch.setattr(
        flash,
        "_curl_args_for_source",
        lambda _u: (["curl", "-fsSL", _file_url(gz)], None, wrong),
    )

    with pytest.raises(flash.FlashIntegrityError):
        flash._flash_gz_from_url(
            "oras://example/ref.img.gz:tag", loop_dev, total_bytes=len(payload)
        )


# ---------- Issue #10 PR2: declared-sha (non-oras) verification --------------
#
# Plain-HTTP sources carry no oras digest; the catalog / PXE plan supplies a
# declared sha that reaches the writer as ``expected_sha``. Same on-wire
# splice, gated on the declared digest instead of the resolved oras one.


def test_flash_img_url_verifies_declared_expected_sha(
    tmp_path: Path,
    loop_device: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if shutil.which("curl") is None:
        pytest.skip("curl not available")
    loop_dev, backing = loop_device

    payload = b"DECLARED" * 1024
    ref = tmp_path / "ref.img"
    ref.write_bytes(payload)
    sha = _sha256_ref(ref)

    # Plain URL -> no oras digest; expected_sha drives the verification.
    monkeypatch.setattr(
        flash,
        "_curl_args_for_source",
        lambda _u: (["curl", "-fsSL", _file_url(ref)], len(payload), None),
    )

    flash._flash_img_from_url(_file_url(ref), loop_dev, total_bytes=len(payload), expected_sha=sha)
    assert backing.read_bytes()[: len(payload)] == payload


def test_flash_img_url_declared_expected_sha_mismatch_raises(
    tmp_path: Path,
    loop_device: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if shutil.which("curl") is None:
        pytest.skip("curl not available")
    loop_dev, _backing = loop_device

    payload = b"DECLBAD!" * 1024
    ref = tmp_path / "ref.img"
    ref.write_bytes(payload)

    monkeypatch.setattr(
        flash,
        "_curl_args_for_source",
        lambda _u: (["curl", "-fsSL", _file_url(ref)], len(payload), None),
    )

    with pytest.raises(flash.FlashIntegrityError):
        flash._flash_img_from_url(
            _file_url(ref),
            loop_dev,
            total_bytes=len(payload),
            expected_sha="sha256:" + "00" * 32,
        )


def test_flash_gz_url_verifies_declared_expected_sha(
    tmp_path: Path,
    loop_device: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if shutil.which("curl") is None or shutil.which("gzip") is None:
        pytest.skip("curl / gzip not available")
    loop_dev, backing = loop_device

    payload = b"DECLGZOK" * 1024
    raw = tmp_path / "ref.img"
    raw.write_bytes(payload)
    subprocess.run(["gzip", "-k", "-f", str(raw)], check=True)
    gz = tmp_path / "ref.img.gz"
    sha = _sha256_ref(gz)  # digest covers the compressed blob

    monkeypatch.setattr(
        flash,
        "_curl_args_for_source",
        lambda _u: (["curl", "-fsSL", _file_url(gz)], None, None),
    )

    flash._flash_gz_from_url(_file_url(gz), loop_dev, total_bytes=len(payload), expected_sha=sha)
    assert backing.read_bytes()[: len(payload)] == payload


# ---------- Issue #10: qcow2-from-URL verification --------------------------
#
# qcow2 can't stream (random-access), so it lands on a temp file and the
# integrity check hashes that file via ``sha256sum`` BEFORE conversion --
# the one path that fails *before* touching the target, not after.


def test_flash_qcow2_url_verifies_matching_digest(
    tmp_path: Path,
    loop_device: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if shutil.which("curl") is None or shutil.which("qemu-img") is None:
        pytest.skip("curl / qemu-img not available")
    loop_dev, backing = loop_device

    payload = b"QCOWVER!" * 1024
    raw = tmp_path / "ref.raw"
    raw.write_bytes(payload)
    qcow2 = tmp_path / "ref.qcow2"
    subprocess.run(["qemu-img", "convert", "-O", "qcow2", str(raw), str(qcow2)], check=True)
    digest = _sha256_ref(qcow2)  # hash of the qcow2 container file

    monkeypatch.setattr(
        flash,
        "_curl_args_for_source",
        lambda _u: (["curl", "-fsSL", _file_url(qcow2)], None, digest),
    )

    flash._flash_qcow2_from_url(_file_url(qcow2), loop_dev)
    # qemu-img convert writes buffered (no O_DIRECT/fsync, unlike the dd
    # paths), so flush the loop device's buffer cache to the backing file
    # before reading it back -- otherwise the read can race the cache and
    # see stale zeros. Production does this via execute_plan's _sync_target.
    subprocess.run(["blockdev", "--flushbufs", str(loop_dev)], check=True)
    assert backing.read_bytes()[: len(payload)] == payload


def test_flash_qcow2_url_rejects_tampered_digest_before_writing(
    tmp_path: Path,
    loop_device: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if shutil.which("curl") is None or shutil.which("qemu-img") is None:
        pytest.skip("curl / qemu-img not available")
    loop_dev, backing = loop_device

    payload = b"QCOWBAD!" * 1024
    raw = tmp_path / "ref.raw"
    raw.write_bytes(payload)
    qcow2 = tmp_path / "ref.qcow2"
    subprocess.run(["qemu-img", "convert", "-O", "qcow2", str(raw), str(qcow2)], check=True)

    before = backing.read_bytes()
    monkeypatch.setattr(
        flash,
        "_curl_args_for_source",
        lambda _u: (["curl", "-fsSL", _file_url(qcow2)], None, None),
    )

    # Declared sha (no oras digest) that doesn't match -> raises, and
    # because qcow2 verifies the temp file before qemu-img convert, the
    # target is left untouched.
    with pytest.raises(flash.FlashIntegrityError):
        flash._flash_qcow2_from_url(_file_url(qcow2), loop_dev, expected_sha="sha256:" + "00" * 32)
    assert backing.read_bytes() == before
