"""Integration tests for the bty-lab deploy lifecycle.

These tests exercise the full ``init -> up -> healthz -> down -v ->
up -> healthz -> down -v`` cycle against real container images. They
catch the kind of breakage a unit test misses by design: a compose
file that's syntactically valid but won't actually come up
(missing data dirs, missing env vars, wrong port shape, image-pull
failures, container-startup races).

The bug that motivated this test: v0.62-v0.65.0 shipped with a
compose.yml that bind-mounted ``./data/nbdmux`` and
``./data/nbdmux/images`` into the nbdmux sidecar without
pre-creating those dirs in ``_prepare_data_dirs``. Every fresh
``bty-lab init && podman compose up -d`` failed with
``Error: statfs /opt/bty/data/nbdmux: no such file or directory``.
The existing unit tests checked file generation; they couldn't
catch a bug that only manifests when podman actually tries to
mount the volumes. v0.65.1 fixes the helper; this test prevents
regressions.

Marked ``@pytest.mark.integration`` so it's opt-in. Skipped when:

* podman or a compose backend is missing on PATH.
* Any of the four sidecar ports (8080 / 8081 / 8082 / 10809) is
  already bound (the existing bty-web on the host, or any other
  conflicting process). The test refuses to fight an in-use port
  rather than silently failing.
"""

from __future__ import annotations

import shutil
import socket
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

# Ports the generated compose binds. Test skips if any are in use
# at start-of-run.
COMPOSE_PORTS = (8080, 8081, 8082, 10809)
HEALTHZ_TIMEOUT = 60.0  # seconds to wait for each service's healthz


def _compose_backend() -> str | None:
    """Return ``podman-compose`` if it's on PATH, else ``None``.
    The test invokes podman-compose directly (see _compose_cmd
    for why), so docker-compose-plugin doesn't count even though
    ``podman compose`` would find it."""
    return "podman-compose" if shutil.which("podman-compose") else None


def _port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


def _wait_healthz(
    port: int,
    path: str = "/healthz",
    *,
    timeout: float | None = None,
    extra_diagnostics: str | None = None,
) -> None:
    """Poll the given port + path for HTTP 200; raises after
    ``timeout`` (defaults to HEALTHZ_TIMEOUT) seconds. The port is
    the host-side bind from the compose / Quadlet entry, hit on
    127.0.0.1. On timeout, dump every running container's name +
    status + tail of logs, plus any caller-provided
    ``extra_diagnostics`` string (for quadlet: journalctl per
    service, systemctl status per service) so the CI failure
    surfaces WHY the service didn't come up, not just that it
    didn't answer."""
    import urllib.error
    import urllib.request

    deadline = time.monotonic() + (timeout if timeout is not None else HEALTHZ_TIMEOUT)
    url = f"http://127.0.0.1:{port}{path}"
    last_err: str = ""
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2.0) as resp:
                if resp.status == 200:
                    return
                last_err = f"HTTP {resp.status}"
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            last_err = str(exc)
        time.sleep(1.0)
    diag = _container_diagnostics()
    if extra_diagnostics:
        diag = f"{diag}\n\n{extra_diagnostics}"
    raise AssertionError(f"healthz timeout: {url}: {last_err}\n\n{diag}")


def _container_diagnostics() -> str:
    """Snapshot of every podman container + its log tail for the
    failure message."""
    lines = ["--- podman ps -a ---"]
    ps = subprocess.run(
        ["podman", "ps", "-a", "--format", "{{.Names}}\t{{.Status}}\t{{.Image}}"],
        check=False,
        capture_output=True,
        text=True,
    )
    lines.append(ps.stdout.strip() or "(empty)")
    for name in [ln.split("\t", 1)[0] for ln in ps.stdout.strip().splitlines() if ln]:
        log = subprocess.run(
            ["podman", "logs", "--tail", "30", name],
            check=False,
            capture_output=True,
            text=True,
        )
        body = log.stdout.strip() or log.stderr.strip()
        lines.append(f"\n--- logs {name} (tail 30) ---\n{body}")
    return "\n".join(lines)


def _compose_cmd() -> list[str]:
    """The compose invocation the test uses. Prefer ``podman-compose``
    directly: ``podman compose`` is a wrapper that picks the first
    available provider in (docker-compose-plugin, podman-compose),
    and the docker-compose plugin fails on a runner without dockerd
    (GitHub Actions runners ship both but only podman is available).
    Invoking ``podman-compose`` directly sidesteps that selection."""
    return ["podman-compose"]


def _compose_up(dest: Path) -> None:
    """Bring up the stack with the envvars file. Raises on non-zero
    with stderr in the exception for diagnostics."""
    try:
        subprocess.run(
            [*_compose_cmd(), "--env-file", "envvars", "up", "-d"],
            cwd=dest,
            check=True,
            capture_output=True,
        )
    except subprocess.CalledProcessError as exc:
        out = exc.stdout.decode("utf-8", errors="replace") if exc.stdout else ""
        err = exc.stderr.decode("utf-8", errors="replace") if exc.stderr else ""
        raise AssertionError(
            f"compose up failed (rc={exc.returncode})\n--- stdout ---\n{out}\n--- stderr ---\n{err}"
        ) from exc


def _compose_down(dest: Path) -> None:
    """Tear the stack down INCLUDING volumes so the next ``up`` is
    truly fresh. Best-effort: don't raise on failure (the test's
    teardown stage shouldn't itself fail noisily if a container
    is already gone)."""
    subprocess.run(
        [*_compose_cmd(), "--env-file", "envvars", "down", "-v"],
        cwd=dest,
        check=False,
        capture_output=True,
    )


def _running_services(dest: Path) -> set[str]:
    """Names of the running services under this compose project.
    Reads from ``podman ps`` since ``podman compose ps`` parses
    differently across podman-compose vs docker-compose."""
    result = subprocess.run(
        ["podman", "ps", "--format", "{{.Names}}"],
        check=True,
        capture_output=True,
        text=True,
    )
    project = dest.name
    prefixes = (f"{project}_", f"{project}-")
    return {line for line in result.stdout.split() if line.startswith(prefixes)}


@pytest.fixture
def deploy_dest(tmp_path: Path) -> Iterator[Path]:
    """A clean tmp dir for the deploy, with safety teardown that
    runs ``compose down -v`` even on test failure so the next test
    isn't poisoned by lingering containers."""
    dest = tmp_path / "bty-host"
    yield dest
    if (dest / "compose.yml").exists():
        _compose_down(dest)


@pytest.mark.integration
def test_deploy_purge_redeploy_lifecycle(deploy_dest: Path) -> None:
    """The shipped contract: ``bty-lab init`` writes a compose
    stack; ``podman compose up -d`` brings it up; healthz returns
    200 on each service; ``compose down -v`` cleans up; running the
    same sequence again produces the same outcome (no stale-state
    surprises)."""
    if _compose_backend() is None:
        pytest.skip("no compose backend on PATH")
    if shutil.which("podman") is None:
        pytest.skip("podman not on PATH")
    busy = [p for p in COMPOSE_PORTS if _port_in_use(p)]
    if busy:
        pytest.skip(f"ports already in use: {busy}")

    # Use the in-process init_main rather than `uvx bty-lab init`
    # so the test exercises THIS checkout's deploy.py instead of
    # a stale wheel cached by uvx.
    import bty.deploy as deploy_mod

    def _provision(dest: Path) -> None:
        """init + copy envvars.example -> envvars; rewrite the bty-*
        image tags to ``:latest``.

        The generated compose pins to the running ``bty.__version__``
        (e.g. ``ghcr.io/safl/bty-web:0.65.2``), but THIS PR is the one
        that publishes that tag; on a PR-build CI run, ghcr.io only
        has tags up to the previous release. ``:latest`` always
        resolves to the most recently published release of each
        image, so the test exercises the deploy contract against
        real bytes without depending on its own unreleased tag.
        """
        import bty as _bty

        deploy_mod.init_main([str(dest)])
        compose_yml = dest / "compose.yml"
        body = compose_yml.read_text(encoding="utf-8")
        for img in ("bty-web", "bty-tftp"):
            body = body.replace(
                f"ghcr.io/safl/{img}:{_bty.__version__}",
                f"ghcr.io/safl/{img}:latest",
            )
        compose_yml.write_text(body, encoding="utf-8")
        envvars_example = dest / "envvars.example"
        envvars = dest / "envvars"
        shutil.copy(envvars_example, envvars)
        # The integration host's IP isn't 10.0.0.5; pin to localhost
        # so any service that consults HOST_ADDR doesn't unicast to
        # the placeholder.
        body = envvars.read_text(encoding="utf-8")
        envvars.write_text(
            body.replace("HOST_ADDR=10.0.0.5", "HOST_ADDR=127.0.0.1"),
            encoding="utf-8",
        )

    # Round 1: fresh deploy.
    _provision(deploy_dest)
    assert (deploy_dest / "compose.yml").is_file()
    # The bug we're guarding against: the data dirs must exist
    # BEFORE compose-up, else podman fails with statfs ENOENT.
    for sub in ("withcache", "bty", "nbdmux", "nbdmux/images"):
        assert (deploy_dest / "data" / sub).is_dir(), (
            f"_prepare_data_dirs should have created data/{sub}/ before "
            f"compose-up; podman won't bind-mount a non-existent host path"
        )

    _compose_up(deploy_dest)
    try:
        # All three sidecars expose /healthz. nbdmux's is the one
        # the original outage was about; the others are belt-and-
        # braces.
        _wait_healthz(8080)  # bty-web
        _wait_healthz(8081)  # withcache
        _wait_healthz(8082)  # nbdmux
        services = _running_services(deploy_dest)
        assert any("bty-web" in s for s in services), services
        assert any("withcache" in s for s in services), services
        assert any("nbdmux" in s for s in services), services
    finally:
        _compose_down(deploy_dest)

    # Verify the down cleaned up containers (no lingering instances
    # of this project's names).
    assert _running_services(deploy_dest) == set()

    # Round 2: re-deploy on the same path. The init call is
    # idempotent (force=False; existing files are kept), so this
    # is the bare ``compose up`` case after a purge -v.
    #
    # Note we DON'T call init again here: the contract is "purge
    # the running state; the files on disk are still good; up
    # should work". Catches "second up needs a regenerate" bugs.
    _compose_up(deploy_dest)
    try:
        _wait_healthz(8080)
        _wait_healthz(8081)
        _wait_healthz(8082)
        services = _running_services(deploy_dest)
        assert any("bty-web" in s for s in services)
        assert any("withcache" in s for s in services)
        assert any("nbdmux" in s for s in services)
    finally:
        _compose_down(deploy_dest)


QUADLET_SYSTEM_DIR = Path("/etc/containers/systemd")
QUADLET_UNIT_NAMES = (
    "bty-web.container",
    "withcache.container",
    "nbdmux.container",
    "bty-tftp.container",
)
QUADLET_SERVICE_NAMES = tuple(n.replace(".container", ".service") for n in QUADLET_UNIT_NAMES)


def _quadlet_prereqs_missing() -> str | None:
    """Return a skip reason if the quadlet lifecycle test can't run
    here, or None if it can. The recommended install path
    (``bty-lab deploy`` as root on Debian 13) needs root + systemctl
    + podman + a compose backend (deploy_main warms the registry
    via ``podman compose pull`` before handing lifecycle off to
    Quadlet) + free ports + a clean /etc/containers/systemd/."""
    import os

    if os.geteuid() != 0:
        return "root required (writes to /etc/containers/systemd/)"
    if shutil.which("podman") is None:
        return "podman not on PATH"
    if shutil.which("systemctl") is None:
        return "systemctl not on PATH"
    if _compose_backend() is None:
        return "no compose backend on PATH (deploy_main needs one for `podman compose pull`)"
    if not QUADLET_SYSTEM_DIR.exists():
        return f"{QUADLET_SYSTEM_DIR} doesn't exist (no systemd-quadlet on this host)"
    existing = [n for n in QUADLET_UNIT_NAMES if (QUADLET_SYSTEM_DIR / n).exists()]
    if existing:
        return (
            f"refusing to clobber existing operator install: "
            f"{QUADLET_SYSTEM_DIR}/ already has {existing}"
        )
    busy = [p for p in COMPOSE_PORTS if _port_in_use(p)]
    if busy:
        return f"ports already in use: {busy}"
    return None


def _systemctl(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["systemctl", *args],
        check=check,
        capture_output=True,
        text=True,
    )


def _quadlet_diagnostics() -> str:
    """Snapshot of every bty service's systemctl status + journalctl
    tail. Emitted on healthz-timeout so the CI log surfaces WHY the
    service crashed (quadlet-managed containers use ``--rm`` by
    default; a container that crashes on start disappears from
    ``podman ps -a`` between Restart=always cycles, so journalctl
    is the only source of the actual error)."""
    lines: list[str] = []
    for svc in QUADLET_SERVICE_NAMES:
        status = _systemctl("status", "--no-pager", "-l", svc, check=False)
        lines.append(f"\n--- systemctl status {svc} ---\n{status.stdout}")
        jc = subprocess.run(
            ["journalctl", "-u", svc, "--no-pager", "-n", "60"],
            check=False,
            capture_output=True,
            text=True,
        )
        lines.append(f"\n--- journalctl -u {svc} (tail 60) ---\n{jc.stdout}")
    return "\n".join(lines)


def _quadlet_teardown() -> None:
    """Best-effort: stop every bty quadlet service, remove every unit,
    daemon-reload. Runs on both test success + failure so the runner
    is left clean for other tests."""
    _systemctl("stop", *QUADLET_SERVICE_NAMES, check=False)
    _systemctl("reset-failed", *QUADLET_SERVICE_NAMES, check=False)
    for name in QUADLET_UNIT_NAMES:
        target = QUADLET_SYSTEM_DIR / name
        if target.exists():
            target.unlink()
    _systemctl("daemon-reload", check=False)


@pytest.fixture
def quadlet_deploy_dest(tmp_path: Path) -> Iterator[Path]:
    """Fixture pair to deploy_dest but with a guaranteed quadlet
    teardown on the way out: even if the test asserts mid-way, the
    system's /etc/containers/systemd/ state gets cleaned up so the
    next pytest invocation isn't poisoned."""
    dest = tmp_path / "bty-host-quadlet"
    yield dest
    _quadlet_teardown()


@pytest.mark.integration
def test_deploy_purge_redeploy_quadlet_lifecycle(
    quadlet_deploy_dest: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The recommended install path: ``bty-lab deploy`` as root ->
    Podman Quadlet units under /etc/containers/systemd/ +
    systemctl-managed lifecycle + all 4 sidecars responding on
    healthz. The v0.62-v0.65.2 outage this guards against: the
    Quadlet emit path silently omitted the nbdmux sidecar because
    four separate hardcoded tuples never grew a fourth entry when
    nbdmux was added in v0.62. The compose integration test above
    can't catch this class of bug -- it exercises `podman compose`
    directly, not the recommended ``bty-lab deploy`` entrypoint that
    Debian 13 operators actually run.

    Skipped unless running as root with podman + systemd-quadlet
    available. On CI this runs under ``sudo pytest -m integration``
    in a dedicated job.
    """
    skip = _quadlet_prereqs_missing()
    if skip is not None:
        pytest.skip(skip)

    import bty
    import bty.deploy as deploy_mod

    # Pin the emitted image tag to the previously-published release
    # so `podman compose pull` + Quadlet service start don't
    # chicken-and-egg on this PR's own unreleased tag. THIS test is
    # about the emit + lifecycle glue in deploy_main; version-string
    # threading is a static test.
    monkeypatch.setattr(bty, "__version__", "0.65.2")
    monkeypatch.setattr(deploy_mod, "__version__", "0.65.2", raising=False)

    def _assert_all_services_active() -> None:
        """Poll each service until active or timeout; fail with
        systemctl status + journalctl tail so the CI log surfaces
        why any service didn't come up."""
        deadline = time.monotonic() + HEALTHZ_TIMEOUT
        states: dict[str, str] = {}
        while time.monotonic() < deadline:
            states = {
                svc: _systemctl("is-active", svc, check=False).stdout.strip()
                for svc in QUADLET_SERVICE_NAMES
            }
            if all(s == "active" for s in states.values()):
                return
            time.sleep(1.0)
        # Failed: gather diagnostics.
        diag_lines = [f"service states: {states}"]
        for svc in QUADLET_SERVICE_NAMES:
            status = _systemctl("status", "--no-pager", "-l", svc, check=False)
            diag_lines.append(f"\n--- systemctl status {svc} ---\n{status.stdout}")
            jc = subprocess.run(
                ["journalctl", "-u", svc, "--no-pager", "-n", "40"],
                check=False,
                capture_output=True,
                text=True,
            )
            diag_lines.append(f"\n--- journalctl -u {svc} (tail 40) ---\n{jc.stdout}")
        raise AssertionError(
            "quadlet services didn't reach active state:\n" + "\n".join(diag_lines)
        )

    def _run_deploy() -> None:
        # Invoke deploy_main directly (in-process) rather than via
        # the bty-lab CLI so THIS checkout's deploy.py runs, not a
        # stale wheel cached by uvx.
        deploy_mod.deploy_main([str(quadlet_deploy_dest), "--force"])

    # Round 1: fresh deploy.
    _run_deploy()

    # All four units landed in the system dir (this is what
    # v0.65.2 got wrong: only three).
    for name in QUADLET_UNIT_NAMES:
        assert (QUADLET_SYSTEM_DIR / name).exists(), (
            f"{QUADLET_SYSTEM_DIR}/{name} missing after `bty-lab deploy` -- "
            f"a hardcoded tuple in deploy.py forgot to include it"
        )
    _assert_all_services_active()
    # Healthz on every sidecar. nbdmux is the one v0.65.2 got wrong.
    # Longer timeout + quadlet-specific diagnostics on failure:
    # quadlet-managed containers default to --rm so a crash-loop
    # produces no visible container between Restart cycles; only
    # journalctl carries the crash reason.
    _wait_healthz(8080, timeout=120.0, extra_diagnostics=_quadlet_diagnostics())  # bty-web
    _wait_healthz(8081, timeout=120.0, extra_diagnostics=_quadlet_diagnostics())  # withcache
    _wait_healthz(8082, timeout=120.0, extra_diagnostics=_quadlet_diagnostics())  # nbdmux

    # Round 2: purge, then verify the deploy path is undone.
    deploy_mod.purge_main([str(quadlet_deploy_dest), "--all", "--yes"])
    for name in QUADLET_UNIT_NAMES:
        assert not (QUADLET_SYSTEM_DIR / name).exists(), (
            f"{QUADLET_SYSTEM_DIR}/{name} still present after `bty-lab purge --all`"
        )
    # Services are inactive (or gone).
    for svc in QUADLET_SERVICE_NAMES:
        state = _systemctl("is-active", svc, check=False).stdout.strip()
        assert state in {"inactive", "failed", "unknown"}, f"{svc} still {state!r} after purge"

    # Round 3: redeploy on the same path. Same contract as round 1.
    _run_deploy()
    for name in QUADLET_UNIT_NAMES:
        assert (QUADLET_SYSTEM_DIR / name).exists()
    _assert_all_services_active()
    _wait_healthz(8080)
    _wait_healthz(8081)
    _wait_healthz(8082)


if __name__ == "__main__":  # pragma: no cover
    # Convenience: ``python -m pytest tests/test_deploy_integration.py -m integration -s``
    raise SystemExit(pytest.main([__file__, "-m", "integration", "-s"]))
