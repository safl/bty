"""Online cijoe provisioning runner (cijoe "task" runs).

When a machine has ``provisioning_mode == 'cijoe-online'``, a
successful flash (signalled by ``POST /pxe/{mac}/done``) kicks off a
cijoe task run from bty-web against the freshly-booted target. This
module owns that orchestration.

The shape mirrors :class:`bty.web._hash.HashManager` /
:class:`bty.web._catalog.DownloadManager` /
:class:`bty.web._release_mgr.ReleaseFetchManager`: an in-memory
:class:`TaskState` dict keyed by MAC, ``kick_off`` to enqueue
(idempotent on already-running), ``cancel`` to abort an in-flight
task via :class:`threading.Event` + ``subprocess.terminate``,
``list`` to snapshot for the API.

Why threading rather than asyncio supervision: the actual cijoe
invocation is a blocking subprocess that takes minutes. The
existing managers run that in :func:`asyncio.to_thread` from an
asyncio worker; here we use a plain :class:`threading.Thread`
because ``cancel`` needs to terminate the subprocess from outside
the worker, and storing the :class:`subprocess.Popen` handle on
the state is simpler than wiring a thread-to-loop signal. The web
UI's request handlers stay non-blocking either way: state-dict
mutations are guarded by a :class:`threading.Lock` and held only
for microseconds.

Lifecycle:

1. ``kick_off(mac, task_ref, target_ip)`` validates the IP, creates
   a :class:`TaskState`, spawns a daemon thread, returns
   immediately.
2. The worker thread updates ``last_task_status='running'`` in
   state.db, synthesises a per-run cijoe transport config, then
   ``Popen``s ``cijoe <task.yaml> --config <transport.toml>
   --monitor`` and waits for it.
3. ``cancel(mac)`` flips the per-task :class:`threading.Event` and
   calls ``Popen.terminate()`` on the stored handle. The worker
   thread's ``Popen.wait`` returns; the worker writes the
   ``cancelled`` status (rather than ``failed``).
4. On exit (success / cancellation / timeout / exception), the
   worker writes the final status to state.db and publishes an
   SSE event so the UI reflects the outcome.

Phase-1 history is "last run only" - the older output dirs
accumulate under ``DEFAULT_TASKS_DIR`` (``/var/lib/bty/tasks``,
overridable per :class:`TaskManager` constructor arg) for
inspection but the machine record only points at the most
recent.

Naming: CIJOE renamed their "workflow" concept to "task" in 2026;
bty mirrors that vocabulary. The CIJOE CLI accepts the same
positional argument shape as before, so ``cijoe <task.yaml>`` is
the same invocation as ``cijoe <workflow.yaml>`` was -- the rename
is purely vocabulary.
"""

from __future__ import annotations

import contextlib
import ipaddress
import logging
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from bty.web import _db
from bty.web._events_log import record as _log_event

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 30 * 60  # 30 min - covers a leisurely first boot.
DEFAULT_SSH_KEY = Path("/var/lib/bty/keys/id_ed25519")
DEFAULT_TASKS_DIR = Path("/var/lib/bty/tasks")
DEFAULT_CIJOE_BIN = str(Path(sys.executable).parent / "cijoe")

# Grace period after ``terminate()`` before falling back to ``kill``.
# cijoe's --monitor mode forwards the signal to its own children; a
# few seconds is plenty for the SSH transport to wind down cleanly.
_TERMINATE_GRACE_SECONDS = 5


@dataclass
class TaskState:
    """Live state of one cijoe task run.

    Mutable on purpose -- the worker thread updates ``status`` /
    ``run_dir`` / ``started_at`` / ``finished_at`` / ``returncode``
    / ``error`` as the run progresses, and the API serialises the
    current snapshot via :meth:`to_dict`.
    """

    mac: str
    task_ref: str
    target_ip: str
    status: str = "queued"  # queued | running | completed | cancelled | failed
    started_at: float | None = None
    finished_at: float | None = None
    error: str | None = None
    returncode: int | None = None
    # Absolute path; populated when status flips to ``running``.
    run_dir: str | None = None
    # Worker-internal handles. Not serialised by ``to_dict``; the
    # threading.Event isn't picklable and the Popen is a process
    # handle.
    _cancel: threading.Event = field(default_factory=threading.Event, repr=False)
    _proc: subprocess.Popen[str] | None = field(default=None, repr=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "mac": self.mac,
            "task_ref": self.task_ref,
            "target_ip": self.target_ip,
            "status": self.status,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "error": self.error,
            "returncode": self.returncode,
            "run_dir": self.run_dir,
        }


class TaskManager:
    """Per-MAC manager for cijoe-online task runs.

    Lifecycle: ``kick_off(mac, task_ref, target_ip)`` enqueues a
    new task (idempotent on already-running for the same mac),
    ``cancel(mac)`` aborts an in-flight task, ``list()`` snapshots
    every known state for the API, ``start()`` sweeps stale
    ``running`` rows in state.db (left by an in-flight task at
    server-restart time) to ``failed``.

    Concurrency: per-MAC parallelism is 1 (a second ``kick_off``
    while a task is in flight returns the existing state). Across
    MACs there's no cap -- ten machines can run their post-flash
    tasks simultaneously without contention.
    """

    def __init__(
        self,
        *,
        state_path: Path,
        publish_machines_changed: Callable[[], None],
        tasks_dir: Path = DEFAULT_TASKS_DIR,
        ssh_key_path: Path = DEFAULT_SSH_KEY,
        ssh_username: str = "root",
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        cijoe_bin: str = DEFAULT_CIJOE_BIN,
    ) -> None:
        self._state_path = state_path
        self._publish = publish_machines_changed
        self._tasks_dir = tasks_dir
        self._ssh_key_path = ssh_key_path
        self._ssh_username = ssh_username
        self._timeout_seconds = timeout_seconds
        self._cijoe_bin = cijoe_bin
        self._states: dict[str, TaskState] = {}
        # ``threading.Lock`` rather than ``asyncio.Lock`` because the
        # consumers are worker threads; FastAPI handlers also touch
        # ``_states`` but only briefly (snapshot-then-release) so
        # holding a thread lock from the event loop is fine.
        self._lock = threading.Lock()

    def start(self) -> None:
        """Sweep stale ``running`` rows in state.db on bty-web start.

        An in-flight task is killed when bty-web stops (the daemon
        thread + cijoe subprocess die with the parent). The machine
        record's ``last_task_status='running'`` survives that
        crash; without this sweep, the operator sees a perma-
        running badge that never resolves until the next task run
        rewrites it.

        Idempotent. Called once from the FastAPI lifespan startup.
        """
        now = datetime.now(UTC).isoformat()
        with _db.open_db(self._state_path) as conn:
            conn.execute(
                """
                UPDATE machines
                SET last_task_status = 'failed',
                    updated_at       = ?
                WHERE last_task_status = 'running'
                """,
                (now,),
            )
            conn.commit()

    def kick_off(self, mac: str, task_ref: str, target_ip: str) -> TaskState | None:
        """Validate, create state, spawn a worker thread.

        Idempotent: returns the existing state if a task is already
        ``queued`` / ``running`` for this mac. ``cancelled`` /
        ``failed`` / ``completed`` allow a fresh attempt.

        Validates ``target_ip`` is a real IPv4 / IPv6 address before
        spawning the thread (TOML-injection defence -- see
        :func:`_render_config`). Returns ``None`` if the validation
        fails so the caller can log + skip without try/except.
        """
        try:
            ipaddress.ip_address(target_ip)
        except ValueError:
            log.error(
                "task %s: refusing to kick off with non-IP target_ip %r",
                mac,
                target_ip,
            )
            return None
        with self._lock:
            existing = self._states.get(mac)
            if existing is not None and existing.status in ("queued", "running"):
                return existing
            state = TaskState(mac=mac, task_ref=task_ref, target_ip=target_ip)
            self._states[mac] = state
        thread = threading.Thread(
            target=self._run,
            args=(state,),
            daemon=True,
            name=f"bty-task-{mac}",
        )
        thread.start()
        return state

    def cancel(self, mac: str) -> TaskState | None:
        """Abort an in-flight task. Returns the state (whatever its
        current status) or ``None`` if no task is known for ``mac``.

        Sets the per-task :class:`threading.Event` and, if a
        subprocess is in flight, sends ``SIGTERM`` (then ``SIGKILL``
        after :data:`_TERMINATE_GRACE_SECONDS`). The worker thread
        sees the event when ``Popen.wait`` returns and writes
        ``cancelled`` (rather than ``failed``) into state.db.

        Permissive on already-finished tasks: returns the state with
        no mutation so the API can be a no-op DELETE.
        """
        with self._lock:
            state = self._states.get(mac)
            if state is None:
                return None
            if state.status not in ("queued", "running"):
                return state
            state._cancel.set()
            proc = state._proc
        # Terminate outside the lock: terminate() can briefly block
        # on signal delivery, and we don't want to hold the lock
        # against ``list()`` while it does.
        if proc is not None:
            with contextlib.suppress(ProcessLookupError, OSError):
                proc.terminate()
        return state

    def list(self) -> list[TaskState]:
        with self._lock:
            return list(self._states.values())

    # ----- worker -----------------------------------------------------------

    def _run(self, state: TaskState) -> None:
        """Worker thread entry point.

        Identical to the pre-v0.7.37 ``TaskRunner._run`` except:
        - subprocess invocation switched to :class:`subprocess.Popen`
          + ``Popen.wait(timeout=...)`` so :meth:`cancel` can call
          ``Popen.terminate()`` on the stored handle.
        - exit-status branching distinguishes operator cancellation
          (cancel flag set) from failure (cancel flag clear).
        """
        run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        run_dir = self._tasks_dir / state.mac / run_id
        try:
            run_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            log.error("task %s: cannot create %s: %s", state.mac, run_dir, exc)
            self._record_status(
                state, status="failed", run_dir=run_dir, error=f"mkdir failed: {exc}"
            )
            return

        # Mark running. Do this BEFORE the subprocess starts so the UI
        # sees the run_dir + start time even if cijoe takes a moment
        # to launch.
        with self._lock:
            state.run_dir = str(run_dir)
            state.started_at = time.time()
        self._record_status(state, status="running", run_dir=run_dir)

        config_path = run_dir / "transport.toml"
        config_path.write_text(self._render_config(state.target_ip))

        cmd = [
            self._cijoe_bin,
            str(state.task_ref),
            "--config",
            str(config_path),
            "--monitor",
        ]
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd=run_dir,
            )
        except FileNotFoundError as exc:
            (run_dir / "error.txt").write_text(f"cijoe binary not found: {exc}\n")
            log.error("task %s: cijoe binary not found: %s", state.mac, exc)
            self._record_status(
                state, status="failed", run_dir=run_dir, error="cijoe binary not found"
            )
            return
        except OSError as exc:
            (run_dir / "error.txt").write_text(f"OSError: {exc}\n")
            log.exception("task %s: subprocess failed", state.mac)
            self._record_status(
                state,
                status="failed",
                run_dir=run_dir,
                error=f"OSError: {exc}",
            )
            return

        with self._lock:
            state._proc = proc

        try:
            stdout, stderr = proc.communicate(timeout=self._timeout_seconds)
        except subprocess.TimeoutExpired:
            # Hard timeout: terminate, then fall through to read
            # whatever buffered output we got.
            with contextlib.suppress(ProcessLookupError, OSError):
                proc.terminate()
            try:
                stdout, stderr = proc.communicate(timeout=_TERMINATE_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(ProcessLookupError, OSError):
                    proc.kill()
                stdout, stderr = proc.communicate()
            (run_dir / "cijoe.stdout").write_text(stdout or "")
            (run_dir / "cijoe.stderr").write_text(stderr or "")
            (run_dir / "error.txt").write_text(f"cijoe timed out after {self._timeout_seconds}s\n")
            log.error("task %s: timed out after %ds", state.mac, self._timeout_seconds)
            self._record_status(
                state,
                status="failed",
                run_dir=run_dir,
                error=f"timed out after {self._timeout_seconds}s",
                returncode=proc.returncode,
            )
            return
        finally:
            with self._lock:
                state._proc = None

        (run_dir / "cijoe.stdout").write_text(stdout or "")
        (run_dir / "cijoe.stderr").write_text(stderr or "")
        rc = proc.returncode
        # ``rc < 0`` on POSIX means the process was killed by signal
        # (-15 = SIGTERM, -9 = SIGKILL). Combined with the cancel
        # event being set, that's an operator-cancel; without the
        # event, treat as a generic failure (e.g. the OS killed it).
        if state._cancel.is_set():
            self._record_status(state, status="cancelled", run_dir=run_dir, returncode=rc)
        elif rc == 0:
            self._record_status(state, status="completed", run_dir=run_dir, returncode=rc)
        else:
            self._record_status(
                state,
                status="failed",
                run_dir=run_dir,
                error=f"cijoe exited {rc}",
                returncode=rc,
            )

    # ----- helpers ----------------------------------------------------------

    def _render_config(self, target_ip: str) -> str:
        """Synthesise a cijoe SSH transport config for this run.

        The ``[cijoe.workflow]`` section name is kept as-is: CIJOE's
        config schema still recognises that section in the
        backwards-compatible CLI; bty doesn't get a say in the upstream
        TOML key. Only bty's own vocabulary (file/class/db) renamed.
        """
        return (
            "# Generated by bty-web for a single online-cijoe task run.\n"
            "[cijoe.workflow]\n"
            "fail_fast = true\n"
            "\n"
            "[cijoe.transport.ssh]\n"
            f'hostname = "{target_ip}"\n'
            f'username = "{self._ssh_username}"\n'
            "port = 22\n"
            f'key = "{self._ssh_key_path}"\n'
        )

    def _record_status(
        self,
        state: TaskState,
        *,
        status: str,
        run_dir: Path,
        error: str | None = None,
        returncode: int | None = None,
    ) -> None:
        now_iso = datetime.now(UTC).isoformat()
        with self._lock:
            state.status = status
            if status in ("running",):
                # Started_at was already set in _run; keep it.
                pass
            elif status in ("completed", "cancelled", "failed"):
                state.finished_at = time.time()
            if error is not None:
                state.error = error
            if returncode is not None:
                state.returncode = returncode
        with _db.open_db(self._state_path) as conn:
            conn.execute(
                """
                UPDATE machines
                SET last_task_run_at      = COALESCE(last_task_run_at, ?),
                    last_task_status      = ?,
                    last_task_output_path = ?,
                    updated_at            = ?
                WHERE mac = ?
                """,
                (now_iso, status, str(run_dir), now_iso, state.mac),
            )
            # On 'running', stamp the start time afresh - COALESCE
            # above keeps the earlier value if a previous row left
            # one behind, but for a fresh kick-off we want the new
            # start.
            if status == "running":
                conn.execute(
                    "UPDATE machines SET last_task_run_at = ? WHERE mac = ?",
                    (now_iso, state.mac),
                )
            # Audit log: capture every status transition so the
            # /ui/events timeline shows ``task started``, ``task
            # completed`` (or failed/cancelled). Skip ``running``
            # if the operator already saw a kick_off event from the
            # PXE-done handler -- but the kick_off itself is logged
            # by the API layer, so the running record here is the
            # informative one.
            kind = f"machine.task.{status}"
            summary = f"task on {state.mac} {status}" + (
                f" (rc={state.returncode})" if state.returncode is not None else ""
            )
            _log_event(
                conn,
                kind=kind,
                summary=summary,
                subject_kind="machine",
                subject_id=state.mac,
                actor="system",
                details={
                    "task_ref": state.task_ref,
                    "target_ip": state.target_ip,
                    "returncode": state.returncode,
                    "error": state.error,
                    "run_dir": str(run_dir),
                },
            )
            conn.commit()
        try:
            self._publish()
        except Exception:
            log.exception("task %s: SSE publish failed", state.mac)


# Backwards-compatibility shim. The pre-v0.7.37 module exposed
# ``TaskRunner`` (no manager methods, just ``kick_off``); a few
# tests + an internal call site reference it. The new
# :class:`TaskManager` is a strict superset, so the alias keeps
# the import path working while we migrate.
TaskRunner = TaskManager
