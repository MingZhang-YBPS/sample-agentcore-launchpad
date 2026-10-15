"""Bounded-concurrency queue for batch evaluations / insights analyses.

AgentCore allows 5 active batch evaluations per account (hard quota), so runs
beyond the configured cap (`eval_max_concurrent_runs`, default 5) QUEUE instead
of failing. Lower the cap to leave headroom for other consumers in the account.
Worker threads are persistent daemons created lazily up to the cap; positions
are exposed for the UI ("QUEUED · waiting for a slot").

A project may hold fewer slots (`quotas.max_concurrent_eval_runs`, projects §11.3):
a run whose project already has that many running is held back — still queued, not
refused — and released into the queue when one of its project's runs finishes, so
one project's batch cannot take every slot. The limit is read when the run is
submitted; the project is the caller's (`project_context`) unless named.
"""

import queue
import threading
from collections.abc import Callable
from typing import Any

from app.core.config import get_settings
from app.core.project_scope import current_project

_AMBIENT: Any = object()


def _project_limit(project_id: str | None) -> int | None:
    from app.services.project_quotas import concurrent_eval_limit

    return concurrent_eval_limit(project_id)


class EvalRunQueue:
    def __init__(
        self,
        max_concurrency: int = 1,
        project_limit: Callable[[str | None], int | None] | None = _project_limit,
    ) -> None:
        self._max_concurrency = max(1, max_concurrency)
        self._project_limit = project_limit
        self._queue: queue.Queue[tuple[str, Callable[[], None]]] = queue.Queue()
        self._lock = threading.Lock()
        self._pending: list[str] = []
        self._running: set[str] = set()
        # Cancelled while still pending: the worker drops these on dequeue
        # instead of calling the run's callable.
        self._cancelled: set[str] = set()
        self._workers: list[threading.Thread] = []
        # run id -> (project, that project's slot limit or None)
        self._projects: dict[str, tuple[str | None, int | None]] = {}
        self._running_by_project: dict[str, int] = {}
        # dequeued while their project was full: re-queued as its runs finish
        self._held: list[tuple[str, Callable[[], None]]] = []

    @property
    def max_concurrency(self) -> int:
        return self._max_concurrency

    def submit(
        self, run_id: str, fn: Callable[[], None], *, project_id: Any = _AMBIENT,
    ) -> int:
        """Enqueue a run; returns its queue position (0 = will run next/now)."""
        project = current_project() if project_id is _AMBIENT else project_id
        limit = None
        if project and self._project_limit is not None:
            try:
                limit = self._project_limit(project)
            except Exception:  # noqa: BLE001 - an unreadable quota is no quota
                limit = None
        with self._lock:
            self._projects[run_id] = (project, limit)
            self._pending.append(run_id)
            # Runs that must finish before a slot frees up for this one.
            position = max(
                0, len(self._pending) - 1 + len(self._running) - (self._max_concurrency - 1)
            )
        self._queue.put((run_id, fn))
        self._ensure_workers()
        return position

    def cancel(self, run_id: str) -> bool:
        """Drop a run that has not been dequeued yet. Returns True when the run
        was still pending (its callable will never execute — the worker skips
        it on dequeue); False when it is already running or unknown, in which
        case the caller has to stop the in-flight work itself."""
        with self._lock:
            if run_id not in self._pending:
                return False
            self._pending.remove(run_id)
            self._cancelled.add(run_id)
            return True

    def _ensure_workers(self) -> None:
        with self._lock:
            if len(self._workers) >= self._max_concurrency:
                return
            worker = threading.Thread(target=self._drain, daemon=True)
            self._workers.append(worker)
            worker.start()

    def _drain(self) -> None:
        # Persistent worker: blocking get, lives for the process. Dying on an
        # idle timeout would race submit() and strand a just-queued run.
        while True:
            run_id, fn = self._queue.get()
            with self._lock:
                cancelled = run_id in self._cancelled
                self._cancelled.discard(run_id)
                if cancelled:
                    self._projects.pop(run_id, None)
                    self._queue.task_done()
                    continue
                project, limit = self._projects.get(run_id, (None, None))
                if (project is not None and limit is not None
                        and self._running_by_project.get(project, 0) >= limit):
                    # its project is full: hold it (still pending) until one of the
                    # project's runs finishes — that run's task is still open, so
                    # `join()` cannot return while this one waits
                    self._held.append((run_id, fn))
                    self._queue.task_done()
                    continue
                if run_id in self._pending:
                    self._pending.remove(run_id)
                self._running.add(run_id)
                if project is not None:
                    self._running_by_project[project] = (
                        self._running_by_project.get(project, 0) + 1)
            try:
                fn()
            except Exception:
                pass  # run status carries the failure; the queue must survive
            finally:
                with self._lock:
                    self._running.discard(run_id)
                    self._projects.pop(run_id, None)
                    released: list[tuple[str, Callable[[], None]]] = []
                    if project is not None:
                        left = self._running_by_project.get(project, 1) - 1
                        if left > 0:
                            self._running_by_project[project] = left
                        else:
                            self._running_by_project.pop(project, None)
                        released = [h for h in self._held
                                    if self._projects.get(h[0], (None,))[0] == project]
                        self._held = [h for h in self._held if h not in released]
                for item in released:  # back in line, before this run's task closes
                    self._queue.put(item)
                self._queue.task_done()

    def state(self) -> dict[str, Any]:
        with self._lock:
            return {
                "running": sorted(self._running),
                "queued": list(self._pending),
                # "locked" = at capacity: one more run would have to wait.
                "locked": len(self._running) >= self._max_concurrency,
                "max_concurrency": self._max_concurrency,
                # queued because their project's slots are taken ("project full")
                "project_waiting": [run_id for run_id, _ in self._held
                                    if run_id not in self._cancelled],
            }

    def position(self, run_id: str) -> int | None:
        with self._lock:
            if run_id in self._running:
                return 0
            if run_id in self._pending:
                return self._pending.index(run_id) + 1
        return None

    def project_full(self, run_id: str) -> bool:
        """True while the run waits only because its project's slots are taken."""
        with self._lock:
            return any(held == run_id for held, _ in self._held)


run_queue = EvalRunQueue(max_concurrency=get_settings().eval_max_concurrent_runs)
