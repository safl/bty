"""Online cijoe provisioning runner.

When a machine has ``provisioning_mode == 'cijoe-online'``, a
successful flash (signalled by ``POST /pxe/{mac}/done``) kicks off a
cijoe workflow run from bty-web against the freshly-booted target.
This module owns that orchestration:

1. Update the machine record's ``last_workflow_status`` to
   ``running`` and publish a machines-update SSE event.
2. Synthesise a per-run cijoe transport config pointing at
   ``last_seen_ip`` over SSH using the operator-supplied key at
   ``/var/lib/bty/keys/id_ed25519``.
3. ``cijoe <workflow.yaml> --config <transport.toml> --monitor``
   runs in a daemon worker thread. cijoe's own transport-retry
   handles waiting for SSH to come up - bty-web doesn't poll. A
   long timeout (default 30 min) keeps the thread from hanging
   forever if the target never appears.
4. On exit (or exception), update the record to ``success`` /
   ``failed`` and publish another SSE event so the UI reflects the
   outcome.

The thread does its own DB writes via :mod:`sqlite3` (which is
thread-safe per-connection). It calls the ``publish_machines_changed``
callable from the worker thread; :class:`MachineEventBus` makes that
safe via the loop captured at app startup.

Phase 1 deliberately keeps history to "last run only" - the older
output dirs accumulate under ``BTY_WORKFLOWS_DIR`` for inspection but
the machine record only points at the most recent. A history table +
auth-protected ``/workflows/{run_id}`` endpoint is left for phase 2.
"""

from __future__ import annotations

import logging
import subprocess
import sys
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from bty.web import _db

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 30 * 60  # 30 min - covers a leisurely first boot.
DEFAULT_SSH_KEY = Path("/var/lib/bty/keys/id_ed25519")
DEFAULT_WORKFLOWS_DIR = Path("/var/lib/bty/workflows")
DEFAULT_CIJOE_BIN = str(Path(sys.executable).parent / "cijoe")


class WorkflowRunner:
    """Runs a cijoe workflow against a target machine in a worker thread."""

    def __init__(
        self,
        *,
        state_path: Path,
        publish_machines_changed: Callable[[], None],
        workflows_dir: Path = DEFAULT_WORKFLOWS_DIR,
        ssh_key_path: Path = DEFAULT_SSH_KEY,
        ssh_username: str = "root",
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        cijoe_bin: str = DEFAULT_CIJOE_BIN,
    ) -> None:
        self._state_path = state_path
        self._publish = publish_machines_changed
        self._workflows_dir = workflows_dir
        self._ssh_key_path = ssh_key_path
        self._ssh_username = ssh_username
        self._timeout_seconds = timeout_seconds
        self._cijoe_bin = cijoe_bin

    def kick_off(self, mac: str, workflow_ref: str, target_ip: str) -> None:
        """Start a worker thread for this run and return immediately."""
        thread = threading.Thread(
            target=self._run,
            args=(mac, workflow_ref, target_ip),
            daemon=True,
            name=f"bty-workflow-{mac}",
        )
        thread.start()

    # ----- worker -----------------------------------------------------------

    def _run(self, mac: str, workflow_ref: str, target_ip: str) -> None:
        run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        run_dir = self._workflows_dir / mac / run_id
        try:
            run_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            log.error("workflow %s: cannot create %s: %s", mac, run_dir, exc)
            self._record_status(mac, status="failed", run_dir=run_dir)
            return

        self._record_status(mac, status="running", run_dir=run_dir)

        config_path = run_dir / "transport.toml"
        config_path.write_text(self._render_config(target_ip))

        status = "failed"
        try:
            result = subprocess.run(
                [
                    self._cijoe_bin,
                    str(workflow_ref),
                    "--config",
                    str(config_path),
                    "--monitor",
                ],
                capture_output=True,
                text=True,
                timeout=self._timeout_seconds,
                cwd=run_dir,
                check=False,
            )
            (run_dir / "cijoe.stdout").write_text(result.stdout)
            (run_dir / "cijoe.stderr").write_text(result.stderr)
            status = "success" if result.returncode == 0 else "failed"
        except subprocess.TimeoutExpired:
            (run_dir / "error.txt").write_text(f"cijoe timed out after {self._timeout_seconds}s\n")
            log.error("workflow %s: timed out after %ds", mac, self._timeout_seconds)
        except FileNotFoundError as exc:
            (run_dir / "error.txt").write_text(f"cijoe binary not found: {exc}\n")
            log.error("workflow %s: cijoe binary not found: %s", mac, exc)
        except OSError as exc:
            (run_dir / "error.txt").write_text(f"OSError: {exc}\n")
            log.exception("workflow %s: subprocess failed", mac)

        self._record_status(mac, status=status, run_dir=run_dir)

    # ----- helpers ----------------------------------------------------------

    def _render_config(self, target_ip: str) -> str:
        """Synthesise a cijoe SSH transport config for this run."""
        return (
            "# Generated by bty-web for a single online-cijoe run.\n"
            "[cijoe.workflow]\n"
            "fail_fast = true\n"
            "\n"
            "[cijoe.transport.ssh]\n"
            f'hostname = "{target_ip}"\n'
            f'username = "{self._ssh_username}"\n'
            "port = 22\n"
            f'key = "{self._ssh_key_path}"\n'
        )

    def _record_status(self, mac: str, *, status: str, run_dir: Path) -> None:
        now = datetime.now(UTC).isoformat()
        with _db.open_db(self._state_path) as conn:
            conn.execute(
                """
                UPDATE machines
                SET last_workflow_run_at      = COALESCE(last_workflow_run_at, ?),
                    last_workflow_status      = ?,
                    last_workflow_output_path = ?,
                    updated_at                = ?
                WHERE mac = ?
                """,
                (now, status, str(run_dir), now, mac),
            )
            # On 'running', also stamp the start time afresh - COALESCE
            # above keeps the earlier value if a previous row left one
            # behind, but for a fresh kick-off we want the new start.
            if status == "running":
                conn.execute(
                    "UPDATE machines SET last_workflow_run_at = ? WHERE mac = ?",
                    (now, mac),
                )
            conn.commit()
        try:
            self._publish()
        except Exception:
            log.exception("workflow %s: SSE publish failed", mac)
