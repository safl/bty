"""Tests for ``bty-lab init`` (`bty.deploy`).

Covers the rendered-file contract (version pinning, env-var refs,
bind-mount layout), the --print stdout-only mode, --force overwrite
behaviour, --systemd Quadlet emission, the top-level dispatcher, and
that ``bty-lab`` stays standalone -- it must NOT import anything from
the [tui] / [web] extras, so a bare ``uvx bty-lab init`` cold-starts
without touching Rich or FastAPI.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

import bty
import bty.deploy as deploy_mod

# ---- Rendered-file contract --------------------------------------------------


def test_default_dest_writes_three_files(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    dest = tmp_path / "bty-host"
    deploy_mod.init_main([str(dest)])
    assert (dest / "compose.yml").is_file()
    assert (dest / ".env.example").is_file()
    assert (dest / "README.md").is_file()
    # Bind-mount roots are pre-created so the operator can see where state
    # will land before starting the stack.
    assert (dest / "data" / "bty").is_dir()
    assert (dest / "data" / "withcache").is_dir()
    captured = capsys.readouterr()
    assert "wrote 3 files" in captured.err
    assert "podman compose up -d" in captured.err


def test_compose_pins_to_current_bty_version(tmp_path: Path) -> None:
    dest = tmp_path / "bty-host"
    deploy_mod.init_main([str(dest)])
    body = (dest / "compose.yml").read_text(encoding="utf-8")
    assert f"ghcr.io/safl/bty-web:v{bty.__version__}" in body
    assert f"ghcr.io/safl/bty-tftp:v{bty.__version__}" in body
    # withcache is an external project and stays on :latest.
    assert "ghcr.io/safl/withcache:latest" in body


def test_compose_wires_first_boot_withcache_env(tmp_path: Path) -> None:
    """bty-web auto-discovers withcache via $BTY_WITHCACHE_URL on every
    request -- the compose file is responsible for setting it. If this
    assertion ever fails, first-boot becomes a UI-configuration step."""
    dest = tmp_path / "bty-host"
    deploy_mod.init_main([str(dest)])
    body = (dest / "compose.yml").read_text(encoding="utf-8")
    assert "BTY_WITHCACHE_URL: http://${HOST_ADDR" in body


def test_compose_uses_bind_mount_data_dirs(tmp_path: Path) -> None:
    dest = tmp_path / "bty-host"
    deploy_mod.init_main([str(dest)])
    body = (dest / "compose.yml").read_text(encoding="utf-8")
    assert "${BTY_HOST_DATA_DIR:-./data}/bty:/var/lib/bty" in body
    assert "${BTY_HOST_DATA_DIR:-./data}/withcache:/data" in body
    # No named volumes -- if anyone reintroduces them, this assertion catches it.
    assert "volumes:\n  withcache-data:" not in body


def test_env_example_has_required_keys(tmp_path: Path) -> None:
    dest = tmp_path / "bty-host"
    deploy_mod.init_main([str(dest)])
    body = (dest / ".env.example").read_text(encoding="utf-8")
    # Required (uncommented):
    assert "\nHOST_ADDR=" in body
    assert "\nWITHCACHE_ADMIN_PASSWORD=" in body
    # Strongly recommended (commented; operator opts in):
    assert "# BTY_ADMIN_PASSWORD=" in body
    # Common (commented):
    assert "# BTY_HOST_DATA_DIR=" in body
    # Advanced knobs are documented so operators don't have to chase
    # them down in the docs. If any of these disappear, an operator
    # will have to grep the bty-web source to discover the var name.
    for var in (
        "BTY_BOOT_RELEASE_REPO",
        "BTY_TRUSTED_PROXY",
        "BTY_SESSION_SECRET",
        "BTY_MAX_UPLOAD_BYTES",
        "BTY_CATALOG_MAX_PARALLEL",
        "BTY_HASH_MAX_PARALLEL",
        "BTY_BACKUP_MAX_PARALLEL",
    ):
        assert f"# {var}=" in body, f"{var} not documented in .env.example"


def test_compose_plumbs_optional_env_vars_through(tmp_path: Path) -> None:
    """The compose env block must reference every optional knob that
    appears in .env.example so uncommenting in .env immediately
    propagates -- without a corresponding ``VAR: ${{VAR:-}}`` entry
    the operator's .env change is silently ignored."""
    dest = tmp_path / "bty-host"
    deploy_mod.init_main([str(dest)])
    body = (dest / "compose.yml").read_text(encoding="utf-8")
    for var in (
        "BTY_ADMIN_PASSWORD",
        "BTY_BOOT_RELEASE_REPO",
        "BTY_TRUSTED_PROXY",
        "BTY_SESSION_SECRET",
        "BTY_MAX_UPLOAD_BYTES",
        "BTY_CATALOG_MAX_PARALLEL",
        "BTY_HASH_MAX_PARALLEL",
        "BTY_BACKUP_MAX_PARALLEL",
    ):
        assert f"{var}: ${{{var}:-}}" in body, f"{var} not plumbed through compose"


# ---- Mode flags --------------------------------------------------------------


def test_print_emits_compose_to_stdout(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    deploy_mod.init_main(["--print"])
    captured = capsys.readouterr()
    assert "services:" in captured.out
    assert f"ghcr.io/safl/bty-web:v{bty.__version__}" in captured.out
    # No files written and no progress text on stderr in --print mode.
    assert not (tmp_path / "compose.yml").exists()
    assert "wrote" not in captured.err


def test_refuses_to_overwrite_without_force(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    dest = tmp_path / "bty-host"
    deploy_mod.init_main([str(dest)])
    capsys.readouterr()  # discard first-run output
    with pytest.raises(SystemExit) as excinfo:
        deploy_mod.init_main([str(dest)])
    assert excinfo.value.code == 1
    err = capsys.readouterr().err
    assert "already exists" in err
    assert "--force" in err


def test_force_overwrites_changed_content(tmp_path: Path) -> None:
    dest = tmp_path / "bty-host"
    deploy_mod.init_main([str(dest)])
    (dest / "compose.yml").write_text("# stale\n", encoding="utf-8")
    deploy_mod.init_main([str(dest), "--force"])
    body = (dest / "compose.yml").read_text(encoding="utf-8")
    assert "stale" not in body
    assert "services:" in body


def test_systemd_emits_quadlet_units_with_absolute_paths(tmp_path: Path) -> None:
    dest = tmp_path / "bty-host"
    deploy_mod.init_main([str(dest), "--systemd"])
    quadlet = dest / "quadlet"
    assert (quadlet / "bty-web.container").is_file()
    assert (quadlet / "withcache.container").is_file()
    assert (quadlet / "bty-tftp.container").is_file()
    web = (quadlet / "bty-web.container").read_text(encoding="utf-8")
    # Quadlet runs from systemd's cwd, not the operator's -- bind-mount
    # paths MUST be absolute.
    expected = (dest / "data" / "bty").resolve()
    assert f"Volume={expected}:/var/lib/bty:Z" in web
    assert f"Image=ghcr.io/safl/bty-web:v{bty.__version__}" in web


def test_data_dir_override_baked_into_quadlets(tmp_path: Path) -> None:
    dest = tmp_path / "bty-host"
    custom = tmp_path / "elsewhere" / "state"
    deploy_mod.init_main([str(dest), "--systemd", "--data-dir", str(custom)])
    web = (dest / "quadlet" / "bty-web.container").read_text(encoding="utf-8")
    withcache = (dest / "quadlet" / "withcache.container").read_text(encoding="utf-8")
    assert f"Volume={custom.resolve()}/bty:/var/lib/bty:Z" in web
    assert f"Volume={custom.resolve()}/withcache:/data:Z" in withcache


def test_readme_links_quadlet_section_only_with_systemd(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    deploy_mod.init_main([str(plain)])
    body_plain = (plain / "README.md").read_text(encoding="utf-8")
    assert "## Auto-start on boot" not in body_plain

    sd = tmp_path / "sd"
    deploy_mod.init_main([str(sd), "--systemd"])
    body_sd = (sd / "README.md").read_text(encoding="utf-8")
    assert "## Auto-start on boot" in body_sd
    assert "quadlet/*.container" in body_sd


# ---- Top-level dispatcher ----------------------------------------------------


def test_main_routes_init_to_init_main(tmp_path: Path) -> None:
    """``bty-lab init <dest>`` dispatched through :func:`main` writes
    the same files as a direct :func:`init_main` call."""
    dest = tmp_path / "bty-host"
    deploy_mod.main(["init", str(dest)])
    assert (dest / "compose.yml").is_file()
    assert (dest / ".env.example").is_file()


def test_main_stays_standalone(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """``bty-lab`` must NOT import anything from the [tui] / [web]
    extras. If anyone slips a top-level ``bty.tui`` or ``bty.web``
    import into :mod:`bty.deploy`, this test fails fast: we poison
    those modules' deferred imports and the dispatch path must still
    succeed."""
    monkeypatch.setitem(sys.modules, "bty.tui._app", None)
    monkeypatch.setitem(sys.modules, "bty.web._app", None)
    dest = tmp_path / "bty-host"
    deploy_mod.main(["init", str(dest)])
    assert (dest / "compose.yml").is_file()


def test_main_no_args_prints_help_and_exits_2(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A bare ``bty-lab`` (no subcommand) prints help and exits 2 --
    argparse convention for "you gave me nothing to do". The help must
    mention the sibling ``bty`` script so somebody running
    ``pipx run bty-lab`` blind learns about the wizard."""
    with pytest.raises(SystemExit) as excinfo:
        deploy_mod.main([])
    assert excinfo.value.code == 2
    err = capsys.readouterr().err
    assert "Subcommands:" in err
    assert "init" in err
    # The "looking for the wizard?" hint is the whole reason we have a
    # bare-help path -- assert it's actually there.
    assert "bty " in err  # mentions the sibling ``bty`` script


def test_main_unknown_subcommand_errors(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An unknown first arg falls through to argparse, which exits 2
    with a usage error."""
    with pytest.raises(SystemExit) as excinfo:
        deploy_mod.main(["bogus-subcommand"])
    assert excinfo.value.code == 2
    err = capsys.readouterr().err
    assert "bogus-subcommand" in err or "unrecognized" in err


def test_main_version_flag(capsys: pytest.CaptureFixture[str]) -> None:
    """``bty-lab --version`` prints the version and exits 0."""
    with pytest.raises(SystemExit) as excinfo:
        deploy_mod.main(["--version"])
    assert excinfo.value.code == 0
    out = capsys.readouterr().out
    assert bty.__version__ in out
