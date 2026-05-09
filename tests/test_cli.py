"""End-to-end-ish tests for bty.cli.

The flash tests call ``cli.cmd_flash`` directly with explicit
dependency-injected fakes - no monkeypatching of module-level
references. Argparse-routing tests still go through ``cli.main`` to
verify the wiring.
"""

from __future__ import annotations

import argparse
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


def test_list_images_includes_bri_descriptors(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``.bri`` files should appear in ``bty list images`` so the
    catalog reflects local + remote pointers in one view."""
    (tmp_path / "local.qcow2").write_bytes(b"")
    (tmp_path / "remote.bri").write_text('url = "https://example.invalid/server.img.gz"\n')
    rc = cli.main(["list", "images", "--json", "--image-root", str(tmp_path)])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    sources = sorted(img["source"] for img in payload["images"])
    assert sources == ["local", "remote"]
    remote = next(i for i in payload["images"] if i["source"] == "remote")
    assert remote["url"] == "https://example.invalid/server.img.gz"
    assert remote["format"] == "img.gz"


def test_inspect_image_malformed_bri_returns_two(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``bty inspect image bad.bri`` surfaces a friendly stderr
    message + exit 2 instead of dumping a BriError traceback."""
    bri = tmp_path / "bad.bri"
    bri.write_text('not_url = "missing-the-url-key"\n')
    rc = cli.main(["inspect", "image", str(bri)])
    assert rc == 2
    err = capsys.readouterr().err
    assert "malformed .bri" in err
    assert "url" in err


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


# ---------- helpers for direct cmd_flash invocation --------------------------


def _flash_args(
    *,
    image: Path | str,
    target: Path = Path("/dev/loop9"),
    provision: str = "none",
    dry_run: bool = False,
    yes: bool = False,
    user_data: Path | None = None,
    meta_data: Path | None = None,
    cijoe_workflow: Path | None = None,
    cijoe_config: Path | None = None,
    progress: str = "text",
    json_out: bool = False,
) -> argparse.Namespace:
    """Build the Namespace ``cmd_flash`` expects, without going through argparse."""
    return argparse.Namespace(
        image=image,
        target=target,
        provision=provision,
        dry_run=dry_run,
        yes=yes,
        user_data=user_data,
        meta_data=meta_data,
        cijoe_workflow=cijoe_workflow,
        cijoe_config=cijoe_config,
        progress=progress,
        json=json_out,
    )


def _block_target(path: Path) -> cli.flash.TargetInfo:
    return cli.flash.TargetInfo(
        path=path,
        exists=True,
        is_block_device=True,
        size_bytes=10**9,
        mountpoints=[],
    )


# Shared fake of probe_target that returns a healthy block device for any path.
def _fake_probe_block_target(p: Path) -> cli.flash.TargetInfo:
    return _block_target(p)


def _no_op_execute(plan: cli.flash.FlashPlan, **_kw: object) -> None:
    return None


# ---------- cmd_flash tests (no monkeypatching) ------------------------------


def test_flash_yes_path_refuses_when_not_root(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The --yes path refuses without root with exit 3 (distinct from 2 = misuse)."""
    img = tmp_path / "x.img"
    img.write_bytes(b"\0" * 1024)

    rc = cli.cmd_flash(
        _flash_args(image=img, yes=True),
        probe_target=_fake_probe_block_target,
        geteuid=lambda: 1000,
    )
    assert rc == 3
    assert "requires root" in capsys.readouterr().err


def test_flash_yes_path_invokes_execute_plan_when_root(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    img = tmp_path / "x.img"
    img.write_bytes(b"\0" * 1024)
    seen_targets: list[Path] = []

    def fake_execute(plan: cli.flash.FlashPlan, **_kw: object) -> None:
        seen_targets.append(plan.target.path)

    rc = cli.cmd_flash(
        _flash_args(image=img, yes=True),
        probe_target=_fake_probe_block_target,
        execute_plan=fake_execute,
        geteuid=lambda: 0,
    )
    assert rc == 0
    assert seen_targets == [Path("/dev/loop9")]
    assert "Done" in capsys.readouterr().out


def test_flash_dispatches_to_url_probe_for_http_image() -> None:
    """``--image http://...`` routes through ``probe_image_url``; the
    local-file ``probe_image`` is not called."""
    seen: dict[str, str] = {}

    def fake_probe_url(url: str) -> cli.flash.ImageInfo:
        seen["url"] = url
        return cli.flash.ImageInfo(
            path=None,
            url=url,
            format="img.zst",
            size_bytes=4096,
            virtual_size_bytes=8192,
        )

    def fake_probe_local(_p: Path) -> cli.flash.ImageInfo:
        seen["local"] = "should-not-be-called"
        raise AssertionError("probe_image was called for a URL --image arg")

    rc = cli.cmd_flash(
        _flash_args(image="http://server/foo.img.zst", dry_run=True),
        probe_image=fake_probe_local,
        probe_image_url=fake_probe_url,
        probe_target=_fake_probe_block_target,
    )
    assert rc == 0
    assert seen == {"url": "http://server/foo.img.zst"}


def test_flash_dispatches_to_url_probe_for_bri_descriptor(tmp_path: Path) -> None:
    """``--image foo.bri`` resolves the descriptor's ``url`` and
    routes through ``probe_image_url`` like a direct URL flash."""
    bri = tmp_path / "bty-server.bri"
    bri.write_text('url = "https://example.invalid/bty-server-x86_64.img.gz"\n')
    seen: dict[str, str] = {}

    def fake_probe_url(url: str) -> cli.flash.ImageInfo:
        seen["url"] = url
        return cli.flash.ImageInfo(
            path=None,
            url=url,
            format="img.gz",
            size_bytes=0,
            virtual_size_bytes=0,
        )

    def fake_probe_local(_p: Path) -> cli.flash.ImageInfo:
        raise AssertionError("probe_image was called for a .bri --image arg")

    rc = cli.cmd_flash(
        _flash_args(image=str(bri), dry_run=True),
        probe_image=fake_probe_local,
        probe_image_url=fake_probe_url,
        probe_target=_fake_probe_block_target,
    )
    assert rc == 0
    assert seen == {"url": "https://example.invalid/bty-server-x86_64.img.gz"}


def test_flash_yes_path_propagates_validation_failure(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Validation errors stop the write path before the root check.

    /dev/null is not a block device, so make_plan rejects it. We skip
    the deps because they are never reached.
    """
    img = tmp_path / "x.img"
    img.write_bytes(b"\0" * 1024)

    rc = cli.cmd_flash(_flash_args(image=img, target=Path("/dev/null"), yes=True))
    assert rc == 1
    assert "Validation: FAILED" in capsys.readouterr().out


def test_flash_cloud_init_requires_user_data(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    img = tmp_path / "x.img"
    img.write_bytes(b"\0")
    rc = cli.cmd_flash(
        _flash_args(
            image=img,
            target=Path("/dev/null"),
            provision="cloud-init",
            dry_run=True,
        )
    )
    assert rc == 2
    assert "--user-data is required" in capsys.readouterr().err


def test_flash_cloud_init_invokes_apply_cloud_init(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    img = tmp_path / "x.img"
    img.write_bytes(b"\0" * 1024)
    user_data = tmp_path / "user-data"
    user_data.write_text("#cloud-config\n")
    captured: list[tuple[Path, Path, Path | None]] = []

    def fake_apply(target: Path, ud: Path, md: Path | None = None) -> None:
        captured.append((target, ud, md))

    rc = cli.cmd_flash(
        _flash_args(
            image=img,
            provision="cloud-init",
            user_data=user_data,
            yes=True,
        ),
        probe_target=_fake_probe_block_target,
        execute_plan=_no_op_execute,
        apply_cloud_init=fake_apply,
        geteuid=lambda: 0,
    )
    assert rc == 0
    assert captured == [(Path("/dev/loop9"), user_data, None)]
    captured_io = capsys.readouterr()
    assert "[provisioning] cloud-init" in captured_io.err
    assert "Done" in captured_io.out


def test_flash_cijoe_requires_workflow(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    img = tmp_path / "x.img"
    img.write_bytes(b"\0")
    rc = cli.cmd_flash(
        _flash_args(
            image=img,
            target=Path("/dev/null"),
            provision="cijoe",
            dry_run=True,
        )
    )
    assert rc == 2
    assert "--cijoe-workflow is required" in capsys.readouterr().err


def test_flash_cijoe_invokes_apply_cijoe(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    img = tmp_path / "x.img"
    img.write_bytes(b"\0" * 1024)
    workflow = tmp_path / "wf.yaml"
    workflow.write_text("steps: []\n")
    captured: list[tuple[Path, Path, Path | None]] = []

    def fake_apply(target: Path, wf: Path, cfg: Path | None = None) -> None:
        captured.append((target, wf, cfg))

    rc = cli.cmd_flash(
        _flash_args(
            image=img,
            provision="cijoe",
            cijoe_workflow=workflow,
            yes=True,
        ),
        probe_target=_fake_probe_block_target,
        execute_plan=_no_op_execute,
        apply_cijoe=fake_apply,
        geteuid=lambda: 0,
    )
    assert rc == 0
    assert captured == [(Path("/dev/loop9"), workflow, None)]
    captured_io = capsys.readouterr()
    assert "[provisioning] cijoe" in captured_io.err
    assert "Done" in captured_io.out


def test_flash_yes_path_exit_5_on_race(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Re-probe race during execute_plan -> exit 5."""
    img = tmp_path / "x.img"
    img.write_bytes(b"\0" * 1024)

    def boom(plan: cli.flash.FlashPlan, **_kw: object) -> None:
        raise cli.flash.FlashRaceError("target now has mounted partitions: /mnt/oops")

    rc = cli.cmd_flash(
        _flash_args(image=img, yes=True),
        probe_target=_fake_probe_block_target,
        execute_plan=boom,
        geteuid=lambda: 0,
    )
    assert rc == 5
    assert "mounted partitions" in capsys.readouterr().err


def test_flash_progress_ndjson_emits_lifecycle_events(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--progress=ndjson`` emits one JSON object per line on stdout."""
    img = tmp_path / "x.img"
    img.write_bytes(b"\0" * 1024)

    def fake_execute(plan: cli.flash.FlashPlan, *, progress: object = None) -> None:
        if callable(progress):
            for evt in (
                cli.flash.FlashProgress(event="started", total_bytes=1024),
                cli.flash.FlashProgress(event="writing", note="img"),
                cli.flash.FlashProgress(event="synced"),
                cli.flash.FlashProgress(event="partprobed"),
            ):
                progress(evt)

    rc = cli.cmd_flash(
        _flash_args(image=img, yes=True, progress="ndjson"),
        probe_target=_fake_probe_block_target,
        execute_plan=fake_execute,
        geteuid=lambda: 0,
    )
    assert rc == 0
    out_lines = [line for line in capsys.readouterr().out.splitlines() if line.startswith("{")]
    events = [json.loads(line) for line in out_lines]
    names = [e["event"] for e in events]
    assert names[:4] == ["started", "writing", "synced", "partprobed"]
    assert names[-1] == "done"
    assert events[0]["total_bytes"] == 1024


def test_flash_progress_none_silences_lifecycle(tmp_path: Path) -> None:
    """``--progress=none`` passes a None callback to execute_plan."""
    img = tmp_path / "x.img"
    img.write_bytes(b"\0" * 1024)
    received: list[object] = []

    def fake_execute(plan: cli.flash.FlashPlan, *, progress: object = None) -> None:
        received.append(progress)

    rc = cli.cmd_flash(
        _flash_args(image=img, yes=True, progress="none"),
        probe_target=_fake_probe_block_target,
        execute_plan=fake_execute,
        geteuid=lambda: 0,
    )
    assert rc == 0
    assert received == [None]


def test_flash_yes_path_exit_4_on_missing_dependency(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A FlashDependencyError from execute_plan -> exit 4."""
    img = tmp_path / "x.img"
    img.write_bytes(b"\0" * 1024)

    def boom(plan: cli.flash.FlashPlan, **_kw: object) -> None:
        raise cli.flash.FlashDependencyError("some-tool is not installed")

    rc = cli.cmd_flash(
        _flash_args(image=img, yes=True),
        probe_target=_fake_probe_block_target,
        execute_plan=boom,
        geteuid=lambda: 0,
    )
    assert rc == 4
    assert "some-tool is not installed" in capsys.readouterr().err


def test_flash_cijoe_passes_config_through(tmp_path: Path) -> None:
    img = tmp_path / "x.img"
    img.write_bytes(b"\0" * 1024)
    workflow = tmp_path / "wf.yaml"
    workflow.write_text("steps: []\n")
    config = tmp_path / "cfg.toml"
    config.write_text("[bty]\n")
    captured: list[tuple[Path, Path, Path | None]] = []

    def fake_apply(target: Path, wf: Path, cfg: Path | None = None) -> None:
        captured.append((target, wf, cfg))

    rc = cli.cmd_flash(
        _flash_args(
            image=img,
            provision="cijoe",
            cijoe_workflow=workflow,
            cijoe_config=config,
            yes=True,
        ),
        probe_target=_fake_probe_block_target,
        execute_plan=_no_op_execute,
        apply_cijoe=fake_apply,
        geteuid=lambda: 0,
    )
    assert rc == 0
    assert captured == [(Path("/dev/loop9"), workflow, config)]


def test_bty_tui_help_exits_cleanly() -> None:
    """Smoke test: ``bty-tui --help`` must import + run without
    raising. Regression catch for any future textual / pamela /
    bty.tui import-chain breakage.
    """
    from bty import tui as tui_mod

    with pytest.raises(SystemExit) as excinfo:
        tui_mod.main(["--help"])
    assert excinfo.value.code == 0


def test_bty_web_help_exits_cleanly() -> None:
    """Smoke test: ``bty-web --help`` must import + run without
    raising. Regression catch for any future fastapi / starlette /
    pamela / bty.web import-chain breakage. The ``[web]`` extra is
    pulled in by the dev group in ``pyproject.toml``, so this runs
    in CI.
    """
    from bty import web as web_mod

    with pytest.raises(SystemExit) as excinfo:
        web_mod.main(["--help"])
    assert excinfo.value.code == 0
