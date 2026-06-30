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
* Any of the four sidecar ports (8080 / 3000 / 4040 / 10809) is
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
COMPOSE_PORTS = (8080, 3000, 4040, 10809)
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


def _wait_healthz(port: int, path: str = "/healthz") -> None:
    """Poll the given port + path for HTTP 200; raises after
    HEALTHZ_TIMEOUT. The port is the host-side bind from the
    compose entry, hit on 127.0.0.1."""
    import urllib.error
    import urllib.request

    url = f"http://127.0.0.1:{port}{path}"
    deadline = time.monotonic() + HEALTHZ_TIMEOUT
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
    raise AssertionError(f"healthz timeout: {url}: {last_err}")


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
        _wait_healthz(3000)  # withcache
        _wait_healthz(4040)  # nbdmux
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
        _wait_healthz(3000)
        _wait_healthz(4040)
        services = _running_services(deploy_dest)
        assert any("bty-web" in s for s in services)
        assert any("withcache" in s for s in services)
        assert any("nbdmux" in s for s in services)
    finally:
        _compose_down(deploy_dest)


if __name__ == "__main__":  # pragma: no cover
    # Convenience: ``python -m pytest tests/test_deploy_integration.py -m integration -s``
    raise SystemExit(pytest.main([__file__, "-m", "integration", "-s"]))
