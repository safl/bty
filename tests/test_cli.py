"""End-to-end-ish tests for bty.cli — modules underneath are mocked."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from bty import cli, images


def test_main_help_exits_cleanly() -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["--help"])
    assert excinfo.value.code == 0


def test_main_version_prints_and_exits_cleanly(
    capsys: pytest.CaptureFixture[str],
) -> None:
    import bty as _bty

    with pytest.raises(SystemExit) as excinfo:
        cli.main(["--version"])
    assert excinfo.value.code == 0
    out = capsys.readouterr().out
    assert _bty.__version__ in out
    assert out.startswith("bty ")


def test_main_no_subcommand_exits_with_usage() -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli.main([])
    # argparse exits 2 on missing required subcommand
    assert excinfo.value.code == 2


def test_list_disks_table(capsys: pytest.CaptureFixture[str]) -> None:
    with patch(
        "bty.cli.disks.list_disks",
        return_value=[
            {
                "path": "/dev/sda",
                "size": "500G",
                "tran": "sata",
                "vendor": "ATA",
                "model": "Samsung SSD",
                "serial": "ABC",
                "removable": False,
            },
        ],
    ):
        rc = cli.main(["list", "disks"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "/dev/sda" in out
    assert "PATH" in out  # uppercased header


def test_list_disks_json(capsys: pytest.CaptureFixture[str]) -> None:
    fake_rows = [{"path": "/dev/sda", "size": "500G"}]
    with patch("bty.cli.disks.list_disks", return_value=fake_rows):
        rc = cli.main(["list", "disks", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema_version"] == "1"
    assert payload["command"] == "list-disks"
    assert payload["disks"] == fake_rows


def test_list_images_uses_image_root_argument(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "alpha.qcow2").write_bytes(b"")
    rc = cli.main(["list", "images", "--image-root", str(tmp_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "alpha.qcow2" in out


def test_inspect_image_missing_returns_two(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = cli.main(["inspect", "image", str(tmp_path / "nope.qcow2")])
    assert rc == 2
    err = capsys.readouterr().err
    assert "no such image" in err


def test_inspect_image_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    img = tmp_path / "x.img"
    img.write_bytes(b"\0" * 5)
    rc = cli.main(["inspect", "image", "--json", str(img)])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema_version"] == "1"
    assert payload["command"] == "inspect-image"
    assert payload["image"]["format"] == "img"
    assert payload["image"]["size_bytes"] == 5


def test_default_image_root_respects_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BTY_IMAGE_ROOT", "/tmp/custom")
    assert images.default_image_root() == Path("/tmp/custom")


def test_default_image_root_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BTY_IMAGE_ROOT", raising=False)
    assert images.default_image_root() == images.DEFAULT_IMAGE_ROOT


# ---------- bty flash --------------------------------------------------------


def test_flash_requires_dry_run_or_yes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    img = tmp_path / "x.img"
    img.write_bytes(b"\0")
    rc = cli.main(["flash", "--image", str(img), "--target", "/dev/null"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "--dry-run" in err and "--yes" in err


def test_flash_dry_run_still_works(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    img = tmp_path / "x.img"
    img.write_bytes(b"\0" * 1024)
    rc = cli.main(["flash", "--image", str(img), "--target", "/dev/null", "--dry-run"])
    # /dev/null is not a block device; dry-run reports validation failure.
    assert rc == 1
    out = capsys.readouterr().out
    assert "Validation: FAILED" in out
    assert "not a block device" in out


def test_flash_yes_path_refuses_when_not_root(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When validation passes, the --yes path must still refuse without root.

    Exit code 3 = "needs root"; distinct from 2 (misuse) so agents can
    respond to the privilege case specifically.
    """
    img = tmp_path / "x.img"
    img.write_bytes(b"\0" * 1024)

    monkeypatch.setattr(
        "bty.cli.flash.probe_target",
        lambda p: cli.flash.TargetInfo(
            path=p,
            exists=True,
            is_block_device=True,
            size_bytes=10**9,
            mountpoints=[],
        ),
    )
    monkeypatch.setattr("bty.cli.os.geteuid", lambda: 1000)

    rc = cli.main(["flash", "--image", str(img), "--target", "/dev/loop9", "--yes"])
    assert rc == 3
    assert "requires root" in capsys.readouterr().err


def test_flash_yes_path_invokes_execute_plan_when_root(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    img = tmp_path / "x.img"
    img.write_bytes(b"\0" * 1024)

    monkeypatch.setattr(
        "bty.cli.flash.probe_target",
        lambda p: cli.flash.TargetInfo(
            path=p,
            exists=True,
            is_block_device=True,
            size_bytes=10**9,
            mountpoints=[],
        ),
    )
    monkeypatch.setattr("bty.cli.os.geteuid", lambda: 0)

    called = []
    monkeypatch.setattr(
        "bty.cli.flash.execute_plan",
        lambda plan, **kw: called.append(plan.target.path),
    )

    rc = cli.main(["flash", "--image", str(img), "--target", "/dev/loop9", "--yes"])
    assert rc == 0
    assert called == [Path("/dev/loop9")]
    assert "Done" in capsys.readouterr().out


def test_flash_yes_path_propagates_validation_failure(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Validation errors stop the write path before the root check."""
    img = tmp_path / "x.img"
    img.write_bytes(b"\0" * 1024)

    # /dev/null is not a block device -> validation fails.
    rc = cli.main(["flash", "--image", str(img), "--target", "/dev/null", "--yes"])
    assert rc == 1
    out = capsys.readouterr().out
    assert "Validation: FAILED" in out


def test_flash_cloud_init_requires_user_data(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    img = tmp_path / "x.img"
    img.write_bytes(b"\0")
    rc = cli.main(
        [
            "flash",
            "--image",
            str(img),
            "--target",
            "/dev/null",
            "--provision",
            "cloud-init",
            "--dry-run",
        ]
    )
    assert rc == 2
    err = capsys.readouterr().err
    assert "--user-data is required" in err


def test_flash_cloud_init_invokes_apply_cloud_init(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    img = tmp_path / "x.img"
    img.write_bytes(b"\0" * 1024)
    user_data = tmp_path / "user-data"
    user_data.write_text("#cloud-config\n")

    monkeypatch.setattr(
        "bty.cli.flash.probe_target",
        lambda p: cli.flash.TargetInfo(
            path=p,
            exists=True,
            is_block_device=True,
            size_bytes=10**9,
            mountpoints=[],
        ),
    )
    monkeypatch.setattr("bty.cli.os.geteuid", lambda: 0)
    monkeypatch.setattr("bty.cli.flash.execute_plan", lambda plan, **kw: None)

    captured: list[tuple[Path, Path, Path | None]] = []
    monkeypatch.setattr(
        "bty.cli.flash.apply_cloud_init",
        lambda target, ud, md=None: captured.append((target, ud, md)),
    )

    rc = cli.main(
        [
            "flash",
            "--image",
            str(img),
            "--target",
            "/dev/loop9",
            "--provision",
            "cloud-init",
            "--user-data",
            str(user_data),
            "--yes",
        ]
    )
    assert rc == 0
    assert captured == [(Path("/dev/loop9"), user_data, None)]
    captured_io = capsys.readouterr()
    # Progress events flow to stderr in default text mode.
    assert "[provisioning] cloud-init" in captured_io.err
    assert "Done" in captured_io.out


def test_flash_cijoe_requires_workflow(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    img = tmp_path / "x.img"
    img.write_bytes(b"\0")
    rc = cli.main(
        [
            "flash",
            "--image",
            str(img),
            "--target",
            "/dev/null",
            "--provision",
            "cijoe",
            "--dry-run",
        ]
    )
    assert rc == 2
    err = capsys.readouterr().err
    assert "--cijoe-workflow is required" in err


def test_flash_cijoe_invokes_apply_cijoe(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    img = tmp_path / "x.img"
    img.write_bytes(b"\0" * 1024)
    workflow = tmp_path / "wf.yaml"
    workflow.write_text("steps: []\n")

    monkeypatch.setattr(
        "bty.cli.flash.probe_target",
        lambda p: cli.flash.TargetInfo(
            path=p,
            exists=True,
            is_block_device=True,
            size_bytes=10**9,
            mountpoints=[],
        ),
    )
    monkeypatch.setattr("bty.cli.os.geteuid", lambda: 0)
    monkeypatch.setattr("bty.cli.flash.execute_plan", lambda plan, **kw: None)

    captured: list[tuple[Path, Path, Path | None]] = []
    monkeypatch.setattr(
        "bty.cli.flash.apply_cijoe",
        lambda target, wf, cfg=None: captured.append((target, wf, cfg)),
    )

    rc = cli.main(
        [
            "flash",
            "--image",
            str(img),
            "--target",
            "/dev/loop9",
            "--provision",
            "cijoe",
            "--cijoe-workflow",
            str(workflow),
            "--yes",
        ]
    )
    assert rc == 0
    assert captured == [(Path("/dev/loop9"), workflow, None)]
    captured_io = capsys.readouterr()
    assert "[provisioning] cijoe" in captured_io.err
    assert "Done" in captured_io.out


def test_flash_yes_path_exit_5_on_race(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Re-probe race during execute_plan -> exit 5."""
    img = tmp_path / "x.img"
    img.write_bytes(b"\0" * 1024)

    monkeypatch.setattr(
        "bty.cli.flash.probe_target",
        lambda p: cli.flash.TargetInfo(
            path=p,
            exists=True,
            is_block_device=True,
            size_bytes=10**9,
            mountpoints=[],
        ),
    )
    monkeypatch.setattr("bty.cli.os.geteuid", lambda: 0)

    def boom(plan: cli.flash.FlashPlan, **_kw: object) -> None:
        raise cli.flash.FlashRaceError("target now has mounted partitions: /mnt/oops")

    monkeypatch.setattr("bty.cli.flash.execute_plan", boom)

    rc = cli.main(["flash", "--image", str(img), "--target", "/dev/loop9", "--yes"])
    assert rc == 5
    err = capsys.readouterr().err
    assert "mounted partitions" in err


def test_flash_progress_ndjson_emits_lifecycle_events(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--progress=ndjson`` emits one JSON object per line on stdout."""
    img = tmp_path / "x.img"
    img.write_bytes(b"\0" * 1024)

    monkeypatch.setattr(
        "bty.cli.flash.probe_target",
        lambda p: cli.flash.TargetInfo(
            path=p,
            exists=True,
            is_block_device=True,
            size_bytes=10**9,
            mountpoints=[],
        ),
    )
    monkeypatch.setattr("bty.cli.os.geteuid", lambda: 0)

    def fake_execute(plan: cli.flash.FlashPlan, *, progress: object = None) -> None:
        if callable(progress):
            for evt in (
                cli.flash.FlashProgress(event="started", total_bytes=1024),
                cli.flash.FlashProgress(event="writing", note="img"),
                cli.flash.FlashProgress(event="synced"),
                cli.flash.FlashProgress(event="partprobed"),
            ):
                progress(evt)

    monkeypatch.setattr("bty.cli.flash.execute_plan", fake_execute)

    rc = cli.main(
        [
            "flash",
            "--image",
            str(img),
            "--target",
            "/dev/loop9",
            "--yes",
            "--progress",
            "ndjson",
        ]
    )
    assert rc == 0
    out_lines = [line for line in capsys.readouterr().out.splitlines() if line.startswith("{")]
    events = [json.loads(line) for line in out_lines]
    names = [e["event"] for e in events]
    assert "started" in names
    assert "writing" in names
    assert "synced" in names
    assert "partprobed" in names
    assert names[-1] == "done"
    assert events[0]["total_bytes"] == 1024


def test_flash_progress_none_silences_lifecycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--progress=none`` passes a None callback to execute_plan."""
    img = tmp_path / "x.img"
    img.write_bytes(b"\0" * 1024)

    monkeypatch.setattr(
        "bty.cli.flash.probe_target",
        lambda p: cli.flash.TargetInfo(
            path=p,
            exists=True,
            is_block_device=True,
            size_bytes=10**9,
            mountpoints=[],
        ),
    )
    monkeypatch.setattr("bty.cli.os.geteuid", lambda: 0)

    received: list[object] = []

    def fake_execute(plan: cli.flash.FlashPlan, *, progress: object = None) -> None:
        received.append(progress)

    monkeypatch.setattr("bty.cli.flash.execute_plan", fake_execute)

    rc = cli.main(
        [
            "flash",
            "--image",
            str(img),
            "--target",
            "/dev/loop9",
            "--yes",
            "--progress",
            "none",
        ]
    )
    assert rc == 0
    assert received == [None]


def test_flash_yes_path_exit_4_on_missing_dependency(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A FlashDependencyError from execute_plan -> exit 4."""
    img = tmp_path / "x.img"
    img.write_bytes(b"\0" * 1024)

    monkeypatch.setattr(
        "bty.cli.flash.probe_target",
        lambda p: cli.flash.TargetInfo(
            path=p,
            exists=True,
            is_block_device=True,
            size_bytes=10**9,
            mountpoints=[],
        ),
    )
    monkeypatch.setattr("bty.cli.os.geteuid", lambda: 0)

    def boom(plan: cli.flash.FlashPlan, **_kw: object) -> None:
        raise cli.flash.FlashDependencyError("some-tool is not installed")

    monkeypatch.setattr("bty.cli.flash.execute_plan", boom)

    rc = cli.main(["flash", "--image", str(img), "--target", "/dev/loop9", "--yes"])
    assert rc == 4
    assert "some-tool is not installed" in capsys.readouterr().err


def test_flash_cijoe_passes_config_through(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    img = tmp_path / "x.img"
    img.write_bytes(b"\0" * 1024)
    workflow = tmp_path / "wf.yaml"
    workflow.write_text("steps: []\n")
    config = tmp_path / "cfg.toml"
    config.write_text("[bty]\n")

    monkeypatch.setattr(
        "bty.cli.flash.probe_target",
        lambda p: cli.flash.TargetInfo(
            path=p,
            exists=True,
            is_block_device=True,
            size_bytes=10**9,
            mountpoints=[],
        ),
    )
    monkeypatch.setattr("bty.cli.os.geteuid", lambda: 0)
    monkeypatch.setattr("bty.cli.flash.execute_plan", lambda plan, **kw: None)

    captured: list[tuple[Path, Path, Path | None]] = []
    monkeypatch.setattr(
        "bty.cli.flash.apply_cijoe",
        lambda target, wf, cfg=None: captured.append((target, wf, cfg)),
    )

    rc = cli.main(
        [
            "flash",
            "--image",
            str(img),
            "--target",
            "/dev/loop9",
            "--provision",
            "cijoe",
            "--cijoe-workflow",
            str(workflow),
            "--cijoe-config",
            str(config),
            "--yes",
        ]
    )
    assert rc == 0
    assert captured == [(Path("/dev/loop9"), workflow, config)]
