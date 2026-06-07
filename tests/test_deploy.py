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
    assert (dest / "envvars.example").is_file()
    assert (dest / "README.md").is_file()
    # Bind-mount roots are pre-created so the operator can see where state
    # will land before starting the stack.
    assert (dest / "data" / "bty").is_dir()
    assert (dest / "data" / "withcache").is_dir()
    captured = capsys.readouterr()
    assert "wrote 3 files" in captured.err
    assert "podman compose --profile tftp up -d" in captured.err


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
    body = (dest / "envvars.example").read_text(encoding="utf-8")
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
        assert f"# {var}=" in body, f"{var} not documented in envvars.example"


def test_compose_plumbs_optional_env_vars_through(tmp_path: Path) -> None:
    """The compose env block must reference every optional knob that
    appears in envvars.example so uncommenting in envvars immediately
    propagates -- without a corresponding ``VAR: ${{VAR:-}}`` entry
    the operator's envvars change is silently ignored."""
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
    assert (dest / "envvars.example").is_file()


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


# ---- deploy + upgrade subcommands -------------------------------------------


@pytest.fixture
def _patched_runtime(monkeypatch: pytest.MonkeyPatch) -> dict[str, list]:
    """Stub the runtime side-effects of `deploy` / `upgrade`:

    - prereqs always pass (podman + podman-compose simulated as on PATH).
    - host-addr auto-detection returns a stable address (so test output
      doesn't drift with the developer's NIC layout).
    - ``_run`` accumulates invocations into a list rather than spawning
      podman / systemctl.
    - ``_install_quadlets`` is a no-op (system path / root not exercised
      here; covered separately).

    Returns the calls dict the test can inspect."""
    calls: dict[str, list] = {"run": [], "quadlets": []}

    def fake_run(cmd, *, cwd=None, env=None):  # type: ignore[no-untyped-def]
        calls["run"].append((list(cmd), cwd))

    def fake_install_quadlets(dest, *, force):  # type: ignore[no-untyped-def]
        # Mimic the real return shape; record for assertions.
        calls["quadlets"].append((Path(dest), force))
        return [deploy_mod.QUADLET_SYSTEM_DIR / "bty-web.container"]

    monkeypatch.setattr(deploy_mod, "_run", fake_run)
    monkeypatch.setattr(deploy_mod, "_install_quadlets", fake_install_quadlets)
    monkeypatch.setattr(deploy_mod.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(deploy_mod, "_detect_host_addr", lambda: "10.20.30.200")
    monkeypatch.setattr(deploy_mod.os, "geteuid", lambda: 0)
    return calls


def test_deploy_emits_envvars_and_runs_compose(
    tmp_path: Path, _patched_runtime: dict[str, list]
) -> None:
    """``deploy`` writes a real ``envvars`` (not just .example) with the
    detected HOST_ADDR + the historic-PAM "bty" admin password default
    (session secret stays random crypto material), and runs ``podman
    compose pull`` + ``up -d``."""
    dest = tmp_path / "bty-host"
    deploy_mod.deploy_main([str(dest)])

    envvars = (dest / "envvars").read_text(encoding="utf-8")
    assert "\nHOST_ADDR=10.20.30.200\n" in envvars
    # Admin passwords default to "bty" (memorable, matches PAM convention).
    assert "\nBTY_ADMIN_PASSWORD=bty\n" in envvars
    assert "\nWITHCACHE_ADMIN_PASSWORD=bty\n" in envvars
    # Session secret is random crypto material -- just assert it's filled.
    session_line = next(
        line for line in envvars.splitlines() if line.startswith("BTY_SESSION_SECRET=")
    )
    assert len(session_line.split("=", 1)[1]) >= 32

    # podman compose pull + up -d both ran, with --profile tftp baked in.
    run_cmds = [cmd for cmd, _ in _patched_runtime["run"]]
    assert ["podman", "compose", "--env-file", "envvars", "--profile", "tftp", "pull"] in run_cmds
    assert [
        "podman",
        "compose",
        "--env-file",
        "envvars",
        "--profile",
        "tftp",
        "up",
        "-d",
    ] in run_cmds


def test_deploy_as_root_does_system_install(
    tmp_path: Path, _patched_runtime: dict[str, list]
) -> None:
    """Run as root, ``deploy`` does the full system install: TFTP
    sidecar in the compose call + Quadlet units installed + systemctl
    daemon-reload + service start."""
    dest = tmp_path / "bty-host"
    deploy_mod.deploy_main([str(dest)])  # _patched_runtime fakes geteuid==0

    # TFTP profile is included on the compose calls.
    run_cmds = [cmd for cmd, _ in _patched_runtime["run"]]
    assert ["podman", "compose", "--env-file", "envvars", "--profile", "tftp", "pull"] in run_cmds
    assert [
        "podman",
        "compose",
        "--env-file",
        "envvars",
        "--profile",
        "tftp",
        "up",
        "-d",
    ] in run_cmds
    # Quadlet units installed + systemctl invocations.
    assert len(_patched_runtime["quadlets"]) == 1
    assert ["systemctl", "daemon-reload"] in run_cmds
    starts = [cmd for cmd in run_cmds if cmd[:2] == ["systemctl", "start"]]
    assert len(starts) == 1
    assert set(starts[0][2:]) == set(deploy_mod._SYSTEMD_SERVICES)


def test_deploy_as_non_root_does_user_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _patched_runtime: dict[str, list]
) -> None:
    """Run as non-root, ``deploy`` does the compose-only user install:
    no TFTP profile, no Quadlet install, no systemctl, plus a loud
    "limitations" warning naming exactly what's missing + the re-run
    command to promote to a system install."""
    monkeypatch.setattr(deploy_mod.os, "geteuid", lambda: 1000)
    dest = tmp_path / "bty-host"
    deploy_mod.deploy_main([str(dest)])

    run_cmds = [cmd for cmd, _ in _patched_runtime["run"]]
    # Compose runs without the --profile tftp flag (TFTP needs root for UDP/69).
    assert ["podman", "compose", "--env-file", "envvars", "pull"] in run_cmds
    assert ["podman", "compose", "--env-file", "envvars", "up", "-d"] in run_cmds
    # No --profile tftp in any compose call.
    assert not any("--profile" in cmd for cmd in run_cmds if cmd[:2] == ["podman", "compose"])
    # No Quadlets installed, no systemctl.
    assert _patched_runtime["quadlets"] == []
    assert not any(cmd[0] == "systemctl" for cmd in run_cmds)


def test_deploy_user_install_warns_about_limitations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    _patched_runtime: dict[str, list],
) -> None:
    """The user-install path must surface exactly what's missing and
    how to promote -- without this, operators see no autostart on
    reboot and don't realise why."""
    monkeypatch.setattr(deploy_mod.os, "geteuid", lambda: 1000)
    dest = tmp_path / "bty-host"
    deploy_mod.deploy_main([str(dest)])
    err = capsys.readouterr().err
    assert "user install [non-root]" in err
    assert "No autostart" in err
    assert "No TFTP sidecar" in err
    # The re-run command must be present so the operator can copy-paste.
    assert f"sudo bty-lab deploy {dest} --force" in err


def test_deploy_host_addr_override(tmp_path: Path, _patched_runtime: dict[str, list]) -> None:
    """``--host-addr`` overrides auto-detection and lands in envvars."""
    dest = tmp_path / "bty-host"
    deploy_mod.deploy_main([str(dest), "--host-addr", "192.168.50.10"])
    assert "\nHOST_ADDR=192.168.50.10\n" in (dest / "envvars").read_text(encoding="utf-8")


def test_deploy_refuses_existing_envvars_without_force(
    tmp_path: Path, _patched_runtime: dict[str, list]
) -> None:
    """Pre-existing ``envvars`` is preserved unless ``--force`` -- a
    silent overwrite would replace operator-set passwords."""
    dest = tmp_path / "bty-host"
    deploy_mod.deploy_main([str(dest)])  # first run lands envvars
    with pytest.raises(SystemExit) as excinfo:
        deploy_mod.deploy_main([str(dest)])  # second run, no --force
    assert excinfo.value.code == 1


def test_deploy_force_overwrites_envvars(tmp_path: Path, _patched_runtime: dict[str, list]) -> None:
    """``--force`` regenerates everything, including envvars (admin
    passwords reset to the "bty" default, session secret rotates to a
    fresh random value)."""
    dest = tmp_path / "bty-host"
    deploy_mod.deploy_main([str(dest)])
    sess1 = next(
        line
        for line in (dest / "envvars").read_text().splitlines()
        if line.startswith("BTY_SESSION_SECRET=")
    )
    deploy_mod.deploy_main([str(dest), "--force"])
    sess2 = next(
        line
        for line in (dest / "envvars").read_text().splitlines()
        if line.startswith("BTY_SESSION_SECRET=")
    )
    # Session secret rotates on --force (it's fresh random each time).
    assert sess1 != sess2


def test_deploy_missing_prereq_aborts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``-f`` does NOT bypass missing prereqs -- the deploy genuinely
    can't proceed without podman / a compose backend."""
    monkeypatch.setattr(deploy_mod.shutil, "which", lambda name: None)
    with pytest.raises(SystemExit) as excinfo:
        deploy_mod.deploy_main([str(tmp_path / "bty-host"), "--force"])
    assert excinfo.value.code == 1
    err = capsys.readouterr().err
    assert "podman" in err


def test_upgrade_refuses_quadlet_managed_without_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    _patched_runtime: dict[str, list],
) -> None:
    """Upgrading a Quadlet-managed stack as non-root would race the
    running systemd-managed containers via `podman compose up -d`. The
    new auto-detect refuses cleanly with a re-run hint."""
    dest = tmp_path / "bty-host"
    deploy_mod.deploy_main([str(dest)])  # full system install (root)
    _patched_runtime["run"].clear()

    # Pretend the operator now invokes upgrade as a normal user, but
    # the Quadlet units are still installed system-wide.
    monkeypatch.setattr(deploy_mod.os, "geteuid", lambda: 1000)
    real_exists = Path.exists

    def fake_exists(self):  # type: ignore[no-untyped-def]
        if str(self).startswith("/etc/containers/systemd"):
            return True
        return real_exists(self)

    monkeypatch.setattr(Path, "exists", fake_exists)

    with pytest.raises(SystemExit) as excinfo:
        deploy_mod.upgrade_main([str(dest)])
    assert excinfo.value.code == 1
    err = capsys.readouterr().err
    assert "Quadlet-managed" in err
    assert f"sudo bty-lab upgrade {dest}" in err


def test_upgrade_refuses_without_existing_compose(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`upgrade` is for an existing deploy -- refuse if compose.yml /
    envvars are missing so the operator doesn't accidentally regenerate
    over a stale dir."""
    with pytest.raises(SystemExit) as excinfo:
        deploy_mod.upgrade_main([str(tmp_path / "bty-host")])
    assert excinfo.value.code == 1
    assert "deploy" in capsys.readouterr().err


def test_upgrade_pulls_and_restarts_compose_managed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _patched_runtime: dict[str, list]
) -> None:
    """`upgrade` on a compose-managed stack pulls + re-`up -d`s."""
    dest = tmp_path / "bty-host"
    deploy_mod.deploy_main([str(dest)])  # set up the deploy
    _patched_runtime["run"].clear()

    # Force the Quadlet-detect check to see no installed units.
    def _no_quadlets(self):  # type: ignore[no-untyped-def]
        return not str(self).startswith("/etc/containers/systemd")

    monkeypatch.setattr(Path, "exists", _no_quadlets)

    # Pre-create compose + envvars existence checks pass (they were written
    # by the deploy call above).
    deploy_mod.upgrade_main([str(dest)])
    run_cmds = [cmd for cmd, _ in _patched_runtime["run"]]
    assert any(c[-1] == "pull" for c in run_cmds)
    assert any(c[-2:] == ["up", "-d"] for c in run_cmds)
    # No systemctl on a compose-managed upgrade.
    assert not any(c[0] == "systemctl" for c in run_cmds)


def test_upgrade_quadlet_managed_as_root_uses_systemctl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _patched_runtime: dict[str, list]
) -> None:
    """When Quadlet units exist under /etc/containers/systemd and
    upgrade runs as root, it refreshes the units + daemon-reload +
    systemctl restart (instead of `podman compose up -d` which would
    race the running systemd-managed containers)."""
    dest = tmp_path / "bty-host"
    deploy_mod.deploy_main([str(dest)])  # system install (root via fixture)
    _patched_runtime["run"].clear()
    _patched_runtime["quadlets"].clear()
    # Stub Path.exists so the QUADLET_SYSTEM_DIR check fires True.
    real_exists = Path.exists

    def fake_exists(self):  # type: ignore[no-untyped-def]
        if str(self).startswith("/etc/containers/systemd"):
            return True
        return real_exists(self)

    monkeypatch.setattr(Path, "exists", fake_exists)

    deploy_mod.upgrade_main([str(dest)])
    run_cmds = [cmd for cmd, _ in _patched_runtime["run"]]
    assert ["systemctl", "daemon-reload"] in run_cmds
    restarts = [cmd for cmd in run_cmds if cmd[:2] == ["systemctl", "restart"]]
    assert len(restarts) == 1
    assert set(restarts[0][2:]) == set(deploy_mod._SYSTEMD_SERVICES)


def test_main_dispatches_deploy_and_upgrade(
    tmp_path: Path, _patched_runtime: dict[str, list]
) -> None:
    """The top-level dispatcher routes `deploy` and `upgrade` to their
    handlers (regression for the subcommand-sniff list)."""
    dest = tmp_path / "bty-host"
    deploy_mod.main(["deploy", str(dest)])
    assert (dest / "envvars").is_file()
    deploy_mod.main(["upgrade", str(dest)])  # would crash if dispatcher missed it


def test_main_help_lists_all_three_subcommands(capsys: pytest.CaptureFixture[str]) -> None:
    """No-arg help mentions init / deploy / upgrade, so an operator who
    runs `pipx run bty-lab` blind discovers all three."""
    with pytest.raises(SystemExit):
        deploy_mod.main([])
    err = capsys.readouterr().err
    for subcommand in ("init", "deploy", "upgrade"):
        assert subcommand in err
