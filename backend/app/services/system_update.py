"""Online console update (docs/design online-update §2): check, prepare, schedule, apply.

The web process never restarts itself and never touches the running checkout:

* **check** fetches the fixed source (`settings.update_repo` / `update_branch`, never a
  request parameter) into a bare cache `DATA_DIR/updates/repo.git` and accepts only a
  fast-forward of the running commit (`git merge-base --is-ancestor`);
* **prepare** (a background job, resumable from `system_updates.progress`) checks the
  target out into its own stage worktree and runs fixed commands only — `uv sync
  --frozen`, `npm ci`, `npm run build`, an import and a port self-test — at `nice 10`;
* **schedule** publishes an announcement; at the time the scheduler task
  `system.update` writes `DATA_DIR/updates/apply.request`, which the root updater
  (`launchpad-updater.path` → `apply_update.sh`, installed by
  `backend/scripts/install_updater.sh`) applies: backup → fast-forward → swap the built
  `dist` → restart → health check on `version.sha` → roll back on failure;
* the updater's `apply.result` is read back at startup and on every tick.

Every git / build / network call goes through `run_command` / `port_probe`, which the
tests replace; nothing here reaches GitHub under test.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import socket
import subprocess
import threading
import time
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.core.config import DATA_DIR, REPO_ROOT, get_settings
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.core.errors import AppError, NotFoundError
from app.core.project_scope import project_context
from app.models.system_update import (
    APPLYING,
    BUSY_STATUSES,
    DONE,
    FAILED,
    KIND_ROLLBACK,
    KIND_UPDATE,
    PREPARE_FAILED,
    PREPARING,
    READY,
    ROLLED_BACK,
    SCHEDULED,
    SUPERSEDED,
    SystemUpdate,
)
from app.services.audit import OUTCOME_FAILED, OUTCOME_SUCCESS, record_audit_event

logger = logging.getLogger(__name__)

# Module attributes on purpose: tests repoint them.
REPO_DIR: Path = REPO_ROOT
UPDATES_DIR: Path = DATA_DIR / "updates"
# what install_updater.sh writes; both present = the updater is installed
UPDATER_FILES: tuple[Path, ...] = (
    Path("/etc/systemd/system/launchpad-updater.path"),
    Path("/usr/local/lib/launchpad/apply_update.sh"),
)

CHECK_TTL = timedelta(minutes=10)
MAX_COMMITS = 50
KEEP_STAGES = 2
IMMEDIATE_DELAY = timedelta(seconds=60)  # "now" still counts down a minute (§2.5)
MAX_SCHEDULE_AHEAD = timedelta(days=7)
# an apply.request nobody picked up within this long: the updater is not running
REQUEST_STALE_AFTER = timedelta(minutes=10)
STAGE_MARKER = ".launchpad-stage-ok"
REQUEST_FILE, RESULT_FILE = "apply.request", "apply.result"
PREPARE_STEPS = ("fetch", "worktree", "backend", "frontend", "import", "port", "size")
_SHA = re.compile(r"^[0-9a-f]{40}$")
_ID = re.compile(r"^[0-9a-f]{32}$")
_LOG_MAX_CHARS = 200_000
_OUTPUT_TAIL = 2000

_LOCK = threading.Lock()
_RUNNING: set[str] = set()
_CHECK: dict[str, Any] = {}


# ── commands (the injection point) ────────────────────────────────────────────


@dataclass(frozen=True)
class CommandResult:
    code: int
    out: str
    err: str


def run_command(
    argv: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None,
    timeout: float = 600,
) -> CommandResult:
    """Run one fixed command. Replaced in tests."""
    try:
        done = subprocess.run(  # noqa: S603 — argv is built here, never from a request
            argv, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        return CommandResult(127, "", str(exc))
    except subprocess.TimeoutExpired:
        return CommandResult(124, "", f"timed out after {timeout:.0f} s")
    return CommandResult(done.returncode, done.stdout or "", done.stderr or "")


def _git(*args: str, cwd: Path, timeout: float = 600) -> CommandResult:
    return run_command(["git", *args], cwd=cwd, timeout=timeout)


def _git_ok(*args: str, cwd: Path, code: str = "update.check_failed",
            timeout: float = 600) -> str:
    result = _git(*args, cwd=cwd, timeout=timeout)
    if result.code != 0:
        raise AppError(code, f"git {args[0]} failed: {_tail(result.err or result.out)}",
                       status_code=502)
    return result.out.strip()


def _tail(text: str, limit: int = _OUTPUT_TAIL) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else "…" + text[-limit:]


def updates_dir() -> Path:
    return UPDATES_DIR


def cache_dir() -> Path:
    return updates_dir() / "repo.git"


def stage_dir_for(sha: str) -> Path:
    return updates_dir() / f"stage-{sha}"


def _now() -> datetime:
    return datetime.now(UTC)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _iso(value: datetime | None) -> str | None:
    value = _aware(value)
    return value.isoformat() if value else None


# ── version and switches ──────────────────────────────────────────────────────


@lru_cache(maxsize=1)
def running_version() -> dict[str, Any]:
    """The commit THIS process runs, read once: the updater's health check compares it
    with the target, so it must describe the loaded code, not a later checkout."""
    head = _git("rev-parse", "HEAD", cwd=REPO_DIR, timeout=30)
    sha = head.out.strip() if head.code == 0 and _SHA.match(head.out.strip()) else None
    committed_at = None
    if sha:
        shown = _git("show", "-s", "--format=%cI", sha, cwd=REPO_DIR, timeout=30)
        committed_at = shown.out.strip() or None if shown.code == 0 else None
    index = REPO_DIR / "frontend" / "dist" / "index.html"
    built_at = (datetime.fromtimestamp(index.stat().st_mtime, UTC).isoformat()
                if index.is_file() else None)
    return {"sha": sha, "short": sha[:7] if sha else None, "committed_at": committed_at,
            "built_at": built_at}


def version_sha() -> str | None:
    """`version.sha` of `GET /api/auth/status` (never raises: the route is public)."""
    try:
        return running_version()["sha"]
    except Exception:  # noqa: BLE001
        return None


def updater_installed() -> bool:
    return all(path.exists() for path in UPDATER_FILES)


def feature_enabled() -> bool:
    """`LAUNCHPAD_UPDATE_ENABLED=false` turns everything off; otherwise check and
    prepare are open (without the updater the page offers the manual commands)."""
    return get_settings().update_enabled is not False


def can_schedule() -> bool:
    """D-4: scheduling defaults on only where the updater is installed."""
    setting = get_settings().update_enabled
    return setting is not False and updater_installed()


def _require_enabled() -> None:
    if not feature_enabled():
        raise AppError("update.disabled", "Online update is turned off "
                       "(LAUNCHPAD_UPDATE_ENABLED=false)", status_code=409)


def version_payload() -> dict[str, Any]:
    return {**running_version(), "updater_installed": updater_installed(),
            "update_enabled": feature_enabled(), "can_schedule": can_schedule()}


def disk_free_bytes(path: Path) -> int:
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    return shutil.disk_usage(probe).free


# ── check: the fixed source, fast-forward only (§2.1) ─────────────────────────


def _ensure_cache() -> Path:
    cache = cache_dir()
    if not (cache / "HEAD").is_file():
        cache.parent.mkdir(parents=True, exist_ok=True)
        _git_ok("init", "--bare", "--quiet", str(cache), cwd=cache.parent)
    return cache


def _sync_running_into_cache(cache: Path) -> str:
    """The running commit's history into the cache, from the local checkout (no
    network). With it there, the remote fetch below only transfers the delta — which
    is why it needs no `--depth` — and the ancestry check sees both sides."""
    running = running_version()["sha"]
    if not running:
        raise AppError("update.no_git", "The console does not run from a git checkout, "
                       "so it cannot be updated online", status_code=409)
    _git_ok("fetch", "--no-tags", "--quiet", str(REPO_DIR),
            "+HEAD:refs/launchpad/running", cwd=cache)
    return running


def _is_ancestor(cache: Path, older: str, newer: str) -> bool:
    result = _git("merge-base", "--is-ancestor", older, newer, cwd=cache)
    if result.code not in (0, 1):
        raise AppError("update.check_failed",
                       f"git merge-base failed: {_tail(result.err)}", status_code=502)
    return result.code == 0


def _commit_list(cache: Path, since: str, until: str) -> list[dict[str, Any]]:
    out = _git_ok("log", f"--max-count={MAX_COMMITS}", "--format=%H%x1f%s%x1f%an%x1f%cI",
                  f"{since}..{until}", cwd=cache)
    commits = []
    for line in out.splitlines():
        parts = line.split("\x1f")
        if len(parts) == 4:
            commits.append({"sha": parts[0], "title": parts[1][:200], "author": parts[2][:80],
                            "time": parts[3]})
    return commits


def check_remote(*, force: bool = False) -> dict[str, Any]:
    """The latest commit of the fixed branch and how far the console is behind it.
    Cached for ten minutes; `force` re-fetches."""
    _require_enabled()
    with _LOCK:
        fresh = _CHECK.get("checked_at") and _now() - _CHECK["at"] < CHECK_TTL
        if fresh and not force:
            return dict(_CHECK["result"])
        settings = get_settings()
        result: dict[str, Any] = {"repo": settings.update_repo, "branch": settings.update_branch,
                                  "checked_at": _iso(_now())}
        cache = _ensure_cache()
        running = _sync_running_into_cache(cache)
        _git_ok("fetch", "--no-tags", "--quiet", settings.update_repo,
                f"+refs/heads/{settings.update_branch}:refs/launchpad/remote", cwd=cache,
                timeout=900)
        remote = _git_ok("rev-parse", "refs/launchpad/remote", cwd=cache)
        fast_forward = _is_ancestor(cache, running, remote)
        behind = int(_git_ok("rev-list", "--count", f"{running}..{remote}", cwd=cache) or 0)
        result.update({
            "sha": remote, "short": remote[:7],
            "committed_at": _git_ok("show", "-s", "--format=%cI", remote, cwd=cache) or None,
            "running_sha": running, "behind": behind, "fast_forward": fast_forward,
            "up_to_date": remote == running,
            "commits": _commit_list(cache, running, remote),
        })
        _CHECK.update({"checked_at": result["checked_at"], "at": _now(), "result": result,
                       "failed": None})
        return dict(result)


def cached_check() -> dict[str, Any] | None:
    """The last check (or its error), refreshed when older than ten minutes."""
    if not feature_enabled():
        return None
    failed = _CHECK.get("failed")
    if failed and _now() - _CHECK["failed_at"] < CHECK_TTL:
        return dict(failed)  # a failing source is not re-fetched on every page load
    try:
        return check_remote()
    except AppError as exc:
        failed = {"error": {"code": exc.code, "message": exc.message},
                  "checked_at": _iso(_now())}
        _CHECK.update({"failed": failed, "failed_at": _now()})
        return dict(failed)


def reset_check_cache_for_tests() -> None:
    _CHECK.clear()
    running_version.cache_clear()


# ── rows, log, journal ────────────────────────────────────────────────────────


def _event(row: SystemUpdate, step: str, message: str, level: str = "info") -> None:
    line = json.dumps({"t": _iso(_now()), "step": step, "level": level,
                       "message": message[:4000]}, ensure_ascii=False)
    log = f"{row.log or ''}{line}\n"
    if len(log) > _LOG_MAX_CHARS:  # keep the newest events
        log = log[-_LOG_MAX_CHARS:].split("\n", 1)[-1]
    row.log = log


def log_events(row: SystemUpdate) -> list[dict[str, Any]]:
    events = []
    for line in (row.log or "").splitlines():
        try:
            events.append(json.loads(line))
        except ValueError:
            continue
    return events


def _audit(action: str, target: str, *, actor: str, actor_user_id: str | None = None,
           outcome: str = OUTCOME_SUCCESS) -> None:
    """`system.update.*` — platform-level, filed like the structure journal: the hub's
    `default` workspace, no project."""
    try:
        record_audit_event(workspace_id=DEFAULT_WORKSPACE_ID, actor=actor, action=action,
                           target=target, actor_user_id=actor_user_id, project_id=None,
                           outcome=outcome)
    except Exception:  # noqa: BLE001 — the journal never blocks an update
        logger.warning("could not journal %s", action, exc_info=True)


def serialize(row: SystemUpdate) -> dict[str, Any]:
    return {
        "id": row.id,
        "kind": row.kind,
        "target_sha": row.target_sha,
        "short": row.target_sha[:7],
        "target_time": row.target_time,
        "base_sha": row.base_sha,
        "behind": row.behind,
        "commits": row.commits or [],
        "status": row.status,
        "staged_dir": row.staged_dir,
        "progress": row.progress or {},
        "steps": list(PREPARE_STEPS),
        "scheduled_at": _iso(row.scheduled_at),
        "requested_by": row.requested_by,
        "announcement_id": row.announcement_id,
        "error": row.error,
        "previous_sha": row.previous_sha,
        "finished_at": _iso(row.finished_at),
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
    }


def get_update(db: Session, update_id: str) -> SystemUpdate:
    row = db.get(SystemUpdate, update_id)
    if row is None:
        raise NotFoundError("update.not_found", f"update '{update_id}' not found",
                            {"update_id": update_id})
    return row


def _latest(db: Session) -> SystemUpdate | None:
    return (db.query(SystemUpdate)
            .order_by(SystemUpdate.created_at.desc(), SystemUpdate.id.desc()).first())


def _busy(db: Session) -> SystemUpdate | None:
    return db.query(SystemUpdate).filter(SystemUpdate.status.in_(BUSY_STATUSES)).first()


# ── prepare (§2.3) ────────────────────────────────────────────────────────────


def _new_row(db: Session, identity: Any, *, kind: str, target: str, base: str,
             behind: int, commits: list[dict[str, Any]], target_time: str | None) -> SystemUpdate:
    if _busy(db) is not None:
        raise AppError("update.in_progress", "Another update is being prepared, is "
                       "scheduled or is being applied", status_code=409)
    free = disk_free_bytes(updates_dir())
    floor = get_settings().update_min_free_gb * 1024**3
    if free < floor:
        raise AppError("update.disk_low", f"Only {free / 1024**3:.1f} GB free under "
                       f"{updates_dir()}; preparing needs {floor / 1024**3:.1f} GB",
                       {"free_bytes": free}, status_code=409)
    for ready in db.query(SystemUpdate).filter(SystemUpdate.status == READY).all():
        ready.status = SUPERSEDED  # its stage is pruned like any older one
        _event(ready, "prepare", f"superseded by a new {kind}")
    row = SystemUpdate(
        kind=kind, target_sha=target, base_sha=base, behind=behind, commits=commits,
        target_time=target_time, status=PREPARING, staged_dir=str(stage_dir_for(target)),
        progress={}, requested_by=identity.username[:64],
        requested_by_user_id=getattr(identity, "user_id", None),
    )
    _event(row, "prepare", f"{kind} to {target[:12]} requested by {identity.username}")
    db.add(row)
    db.commit()
    return row


def prepare(db: Session, identity: Any) -> SystemUpdate:
    """Stage the latest commit of the fixed branch (a fast-forward only)."""
    _require_enabled()
    info = check_remote()
    if not info["fast_forward"]:
        raise AppError("update.not_fast_forward",
                       f"{info['branch']} ({info['short']}) is not a descendant of the "
                       "running commit; only fast-forward updates are accepted",
                       {"remote_sha": info["sha"], "running_sha": info["running_sha"]},
                       status_code=409)
    if info["up_to_date"]:
        raise AppError("update.up_to_date", "The console already runs the latest commit",
                       status_code=409)
    with _LOCK:
        row = _new_row(db, identity, kind=KIND_UPDATE, target=info["sha"],
                       base=info["running_sha"], behind=info["behind"],
                       commits=info["commits"], target_time=info["committed_at"])
    _audit("system.update.prepare", f"update:{row.id} {row.target_sha[:12]}",
           actor=identity.username, actor_user_id=getattr(identity, "user_id", None))
    start_prepare(row.id)
    return row


def rollback(db: Session, identity: Any) -> SystemUpdate:
    """Stage the commit the last successful update replaced; it is applied through the
    updater and a schedule like any update."""
    _require_enabled()
    running = running_version()["sha"]
    last = (db.query(SystemUpdate)
            .filter(SystemUpdate.status == DONE, SystemUpdate.target_sha == running,
                    SystemUpdate.previous_sha.isnot(None))
            .order_by(SystemUpdate.finished_at.desc()).first())
    if running is None or last is None or not _SHA.match(last.previous_sha or ""):
        raise AppError("update.no_previous", "No earlier version to roll back to: the "
                       "running commit was not installed by an online update",
                       status_code=409)
    target = last.previous_sha
    with _LOCK:
        cache = _ensure_cache()
        _sync_running_into_cache(cache)
        if not _is_ancestor(cache, target, running):
            raise AppError("update.not_fast_forward", "The previous version is not an "
                           "ancestor of the running commit", status_code=409)
        reverted = _commit_list(cache, target, running)
        count = int(_git_ok("rev-list", "--count", f"{target}..{running}", cwd=cache) or 0)
        when = _git_ok("show", "-s", "--format=%cI", target, cwd=cache) or None
        row = _new_row(db, identity, kind=KIND_ROLLBACK, target=target, base=running,
                       behind=count, commits=reverted, target_time=when)
    _audit("system.update.rollback", f"update:{row.id} {running[:12]}->{target[:12]}",
           actor=identity.username, actor_user_id=getattr(identity, "user_id", None))
    start_prepare(row.id)
    return row


def start_prepare(update_id: str) -> threading.Thread | None:
    """The prepare job's thread (once per row per process)."""
    with _LOCK:
        if update_id in _RUNNING:
            return None
        _RUNNING.add(update_id)
    thread = threading.Thread(target=run_prepare, args=(update_id,), daemon=True,
                              name=f"system-update-{update_id[:8]}")
    thread.start()
    return thread


class PrepareError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def stage_env(stage: Path) -> dict[str, str]:
    """The stage's commands run against a throwaway ledger of their own: the self-test
    boots the NEW code, which must not migrate the live ledger before the backup."""
    env = {k: v for k, v in os.environ.items()
           if k not in ("VIRTUAL_ENV", "UV_PROJECT_ENVIRONMENT", "PYTHONPATH")}
    env["LAUNCHPAD_DATABASE_URL"] = f"sqlite:///{stage / 'data' / 'selftest.db'}"
    env["LAUNCHPAD_MODEL_PRICES_REFRESH_HOURS"] = "0"
    return env


def _nice(argv: list[str]) -> list[str]:
    return ["nice", "-n", "10", *argv]  # §4: a build must not starve the live console


def _run_step(row: SystemUpdate, step: str, argv: list[str], cwd: Path,
              env: dict[str, str] | None, timeout: float) -> None:
    result = run_command(argv, cwd=cwd, env=env, timeout=timeout)
    if result.code != 0:
        raise PrepareError("update.prepare_failed",
                           f"{' '.join(argv)} exited {result.code}: "
                           f"{_tail(result.err or result.out)}")
    _event(row, step, f"{' '.join(argv)}: ok")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def port_probe(stage: Path, sha: str, env: dict[str, str], timeout: float = 30) -> None:
    """Boot the staged backend on a temporary loopback port and wait for
    `/api/auth/status` (and, when it carries one, `version.sha == sha`)."""
    port = _free_port()
    proc = subprocess.Popen(  # noqa: S603 — fixed argv
        ["uv", "run", "--no-sync", "uvicorn", "app.main:app", "--host", "127.0.0.1",
         "--port", str(port)],
        cwd=stage / "backend", env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + timeout
        last = "no answer"
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                err = proc.stderr.read() if proc.stderr else ""
                raise PrepareError("update.selftest_failed",
                                   f"the staged backend exited {proc.returncode}: {_tail(err)}")
            try:
                with urllib.request.urlopen(  # noqa: S310 — loopback, fixed scheme
                        f"http://127.0.0.1:{port}/api/auth/status", timeout=3) as resp:
                    body = json.loads(resp.read() or b"{}")
                reported = (body.get("version") or {}).get("sha")
                if reported not in (None, sha):
                    raise PrepareError("update.selftest_failed",
                                       f"the staged backend reports {reported}, not {sha}")
                return
            except PrepareError:
                raise
            except Exception as exc:  # noqa: BLE001 — not up yet
                last = f"{type(exc).__name__}: {exc}"
                time.sleep(0.5)
        raise PrepareError("update.selftest_failed",
                           f"the staged backend did not answer within {timeout:.0f} s ({last})")
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()


def _tree_bytes(path: Path) -> int:
    total = 0
    stack = [path]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(Path(entry.path))
                    elif entry.is_file(follow_symlinks=False):
                        total += entry.stat(follow_symlinks=False).st_size
        except OSError:
            continue
    return total


def _step_fetch(row: SystemUpdate) -> None:
    cache = _ensure_cache()
    if _git("cat-file", "-e", f"{row.target_sha}^{{commit}}", cwd=cache).code != 0:
        settings = get_settings()
        if row.kind == KIND_ROLLBACK:
            _sync_running_into_cache(cache)
        else:
            _git_ok("fetch", "--no-tags", "--quiet", settings.update_repo,
                    f"+refs/heads/{settings.update_branch}:refs/launchpad/remote",
                    cwd=cache, code="update.prepare_failed", timeout=900)
        if _git("cat-file", "-e", f"{row.target_sha}^{{commit}}", cwd=cache).code != 0:
            raise PrepareError("update.prepare_failed",
                               f"commit {row.target_sha} is not in the update cache")
    _event(row, "fetch", f"{row.target_sha[:12]} is in the cache")


def _step_worktree(row: SystemUpdate) -> bool:
    """True when the stage was (re)created — the build steps after it start over."""
    cache, stage = cache_dir(), stage_dir_for(row.target_sha)
    if stage.exists():
        head = _git("rev-parse", "HEAD", cwd=stage)
        if head.code == 0 and head.out.strip() == row.target_sha:
            _event(row, "worktree", f"reusing {stage}")
            return False
        _git("worktree", "remove", "--force", str(stage), cwd=cache)
        shutil.rmtree(stage, ignore_errors=True)
    _git("worktree", "prune", cwd=cache)
    _git_ok("worktree", "add", "--detach", "--force", str(stage), row.target_sha, cwd=cache,
            code="update.prepare_failed")
    _event(row, "worktree", f"checked out {row.target_sha[:12]} into {stage}")
    return True


def _step_backend(row: SystemUpdate) -> None:
    stage = stage_dir_for(row.target_sha)
    _run_step(row, "backend", _nice(["uv", "sync", "--frozen"]), stage / "backend",
              stage_env(stage), 1800)


def _step_frontend(row: SystemUpdate) -> None:
    stage = stage_dir_for(row.target_sha)
    env = stage_env(stage)
    _run_step(row, "frontend", _nice(["npm", "ci"]), stage / "frontend", env, 1800)
    _run_step(row, "frontend", _nice(["npm", "run", "build"]), stage / "frontend", env, 1800)


def _step_import(row: SystemUpdate) -> None:
    stage = stage_dir_for(row.target_sha)
    (stage / "data").mkdir(exist_ok=True)
    _run_step(row, "import", ["uv", "run", "--no-sync", "python", "-c", "import app.main"],
              stage / "backend", stage_env(stage), 300)


def _step_port(row: SystemUpdate) -> None:
    stage = stage_dir_for(row.target_sha)
    port_probe(stage, row.target_sha, stage_env(stage))
    _event(row, "port", "the staged backend answered /api/auth/status")


def _step_size(row: SystemUpdate) -> None:
    size = _tree_bytes(stage_dir_for(row.target_sha))
    cap = get_settings().update_stage_max_gb * 1024**3
    if size > cap:
        raise PrepareError("update.disk_low",
                           f"the stage takes {size / 1024**3:.1f} GB, over the "
                           f"{cap / 1024**3:.1f} GB cap")
    _event(row, "size", f"stage size {size / 1024**2:.0f} MB")


_STEPS = {
    "fetch": _step_fetch, "worktree": _step_worktree, "backend": _step_backend,
    "frontend": _step_frontend, "import": _step_import, "port": _step_port,
    "size": _step_size,
}


def prune_stages(db: Session) -> list[str]:
    """Keep the newest KEEP_STAGES stage dirs and any an active row still needs."""
    root = updates_dir()
    if not root.is_dir():
        return []
    keep = {
        Path(r.staged_dir).name for r in db.query(SystemUpdate).filter(
            SystemUpdate.status.in_((PREPARING, READY, SCHEDULED, APPLYING))).all()
        if r.staged_dir
    }
    stages = sorted((p for p in root.glob("stage-*") if p.is_dir()),
                    key=lambda p: p.stat().st_mtime, reverse=True)
    keep |= {p.name for p in stages[:KEEP_STAGES]}
    removed = []
    for stage in stages:
        if stage.name in keep:
            continue
        _git("worktree", "remove", "--force", str(stage), cwd=cache_dir())
        shutil.rmtree(stage, ignore_errors=True)
        removed.append(stage.name)
    if removed:
        _git("worktree", "prune", cwd=cache_dir())
    return removed


def run_prepare(update_id: str) -> None:
    """The prepare job: every step not yet `done`, then `ready`. Platform-level, so it
    runs with no project (`project_context(None)`). Never raises."""
    try:
        with project_context(None):
            db = SessionLocal()
            try:
                row = db.get(SystemUpdate, update_id)
                if row is None or row.status != PREPARING:
                    return
                try:
                    for step in PREPARE_STEPS:
                        progress = dict(row.progress or {})
                        # the worktree is re-verified even when done: a later step may
                        # have died after someone pruned it
                        if progress.get(step) == "done" and step != "worktree":
                            continue
                        progress[step] = "running"
                        row.progress = progress
                        db.commit()
                        fresh = _STEPS[step](row)
                        if step == "worktree" and fresh:
                            progress = {"fetch": "done"}
                        row.progress = {**progress, step: "done"}
                        db.commit()
                    stage = stage_dir_for(row.target_sha)
                    (stage / STAGE_MARKER).write_text(row.target_sha + "\n", encoding="utf-8")
                    row.status = READY
                    row.error = None
                    _event(row, "prepare", "ready")
                    db.commit()
                except (PrepareError, AppError) as exc:
                    db.rollback()
                    row = db.get(SystemUpdate, update_id)
                    progress = {k: v for k, v in (row.progress or {}).items() if v == "done"}
                    row.progress = progress
                    row.status = PREPARE_FAILED
                    row.error = f"{exc.code}: {exc.message}"[:4000]
                    row.finished_at = _now()
                    _event(row, "prepare", row.error, level="error")
                    db.commit()
                try:
                    prune_stages(db)
                except Exception:  # noqa: BLE001
                    logger.warning("stage prune failed", exc_info=True)
            finally:
                db.close()
    except Exception:  # noqa: BLE001 — a thread that raises only logs a traceback
        logger.exception("system update prepare %s failed unexpectedly", update_id)
        _fail_unexpectedly(update_id)
    finally:
        with _LOCK:
            _RUNNING.discard(update_id)


def _fail_unexpectedly(update_id: str) -> None:
    db = SessionLocal()
    try:
        row = db.get(SystemUpdate, update_id)
        if row is not None and row.status == PREPARING:
            row.status = PREPARE_FAILED
            row.error = "update.prepare_failed: unexpected error (see the backend log)"
            row.finished_at = _now()
            db.commit()
    finally:
        db.close()


# ── schedule, cancel, announcement (§2.5) ─────────────────────────────────────


def _inflight(db: Session) -> list[dict[str, Any]]:
    from app.services import inflight

    with project_context(None):
        items = inflight.collect_inflight(db)
    return [i.to_dict() for i in items if i.kind != inflight.SYSTEM_UPDATE_PREPARE]


def inflight_summary(db: Session) -> dict[str, Any]:
    from app.services.inflight import BENIGN_OUTCOMES, RESTART_SAFE

    items = _inflight(db)
    return {
        "items": items,
        "interrupted": sum(i["restart_outcome"] not in BENIGN_OUTCOMES for i in items),
        "resumes": sum(i["restart_outcome"] in BENIGN_OUTCOMES
                       and i["restart_outcome"] != RESTART_SAFE for i in items),
    }


def _stage_ready(row: SystemUpdate) -> bool:
    marker = stage_dir_for(row.target_sha) / STAGE_MARKER
    try:
        return marker.read_text(encoding="utf-8").strip() == row.target_sha
    except OSError:
        return False


def _announce(db: Session, row: SystemUpdate, author: str) -> None:
    from app.services import announcements

    when = _aware(row.scheduled_at).strftime("%Y-%m-%d %H:%M UTC")
    content = {
        "title": "System update / 系统更新",
        "body": (f"The console will update and restart at {when}; it will be unavailable "
                 f"for about 1–2 minutes.\n系统将于 {when} 更新重启，约 1–2 分钟不可用。"),
        "link_url": None, "link_label": None,
    }
    try:
        created = announcements.create(db, content, author)
        published = announcements.change(db, created["id"], created["revision"], author,
                                          action="publish")
        row.announcement_id = published["id"]
    except Exception:  # noqa: BLE001 — the schedule stands without its notice
        db.rollback()
        logger.warning("could not publish the update announcement", exc_info=True)


def _close_announcement(db: Session, row: SystemUpdate, *, delete: bool = False) -> None:
    from app.models.announcement import Announcement
    from app.services import announcements

    if not row.announcement_id:
        return
    try:
        notice = db.get(Announcement, row.announcement_id)
        if notice is not None:
            if delete:
                announcements.delete(db, notice.id, notice.revision)
            elif notice.published_content is not None:
                announcements.change(db, notice.id, notice.revision, "system",
                                     action="unpublish")
    except Exception:  # noqa: BLE001 — an edited / deleted notice is the admin's
        db.rollback()
        logger.warning("could not close announcement %s", row.announcement_id, exc_info=True)
    if delete:
        row.announcement_id = None


def schedule(db: Session, update_id: str, at: datetime | None, acknowledged: bool,
             identity: Any) -> SystemUpdate:
    _require_enabled()
    row = get_update(db, update_id)
    if not can_schedule():
        raise AppError("update.updater_missing", "The updater is not installed on this "
                       "host (sudo backend/scripts/install_updater.sh); apply the update "
                       "with the manual commands instead", status_code=409)
    if row.status != READY:
        raise AppError("update.not_ready", f"update '{row.id}' is {row.status}, not ready",
                       {"status": row.status}, status_code=409)
    if not _stage_ready(row):
        raise AppError("update.stage_missing", "The prepared stage is gone; prepare the "
                       "update again", status_code=409)
    now = _now()
    when = _aware(at) if at is not None else now + IMMEDIATE_DELAY
    if when < now - timedelta(minutes=1) or when > now + MAX_SCHEDULE_AHEAD:
        raise AppError("update.schedule_invalid", "The restart time must be between now "
                       "and 7 days from now", status_code=422)
    summary = inflight_summary(db)
    if summary["interrupted"] and not acknowledged:
        raise AppError("update.inflight_unacknowledged",
                       f"{summary['interrupted']} task(s) in progress would be interrupted "
                       "by the restart; confirm to schedule anyway",
                       {"interrupted": summary["interrupted"]}, status_code=409)
    row.scheduled_at = max(when, now)
    row.status = SCHEDULED
    row.error = None
    _event(row, "schedule", f"restart scheduled for {_iso(row.scheduled_at)} by "
           f"{identity.username} ({summary['interrupted']} task(s) acknowledged)")
    db.commit()
    _announce(db, row, identity.username)
    db.commit()
    _audit("system.update.schedule",
           f"update:{row.id} {row.target_sha[:12]} at {_iso(row.scheduled_at)}",
           actor=identity.username, actor_user_id=getattr(identity, "user_id", None))
    return row


def cancel(db: Session, update_id: str, identity: Any) -> SystemUpdate:
    row = get_update(db, update_id)
    if row.status != SCHEDULED:
        raise AppError("update.not_cancellable", f"update '{row.id}' is {row.status}; only "
                       "a scheduled update can be cancelled", {"status": row.status},
                       status_code=409)
    row.status = READY
    row.scheduled_at = None
    _event(row, "schedule", f"cancelled by {identity.username}")
    _close_announcement(db, row, delete=True)
    db.commit()
    _audit("system.update.cancel", f"update:{row.id} {row.target_sha[:12]}",
           actor=identity.username, actor_user_id=getattr(identity, "user_id", None))
    return row


# ── hand-over to the updater, and its result ──────────────────────────────────


def _write_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _hand_over(db: Session, row: SystemUpdate) -> None:
    request = updates_dir() / REQUEST_FILE
    if not can_schedule() or not _stage_ready(row):
        row.status = READY if _stage_ready(row) else PREPARE_FAILED
        row.scheduled_at = None
        row.error = ("update.updater_missing: the updater is not installed"
                     if not can_schedule() else "update.stage_missing: the stage is gone")
        _event(row, "apply", row.error, level="error")
        _close_announcement(db, row)
        db.commit()
        return
    _write_atomic(request, {"update_id": row.id, "sha": row.target_sha, "action": row.kind,
                            "stage_dir": str(stage_dir_for(row.target_sha))})
    row.status = APPLYING
    _event(row, "apply", f"handed {row.target_sha[:12]} to the updater ({request})")
    db.commit()
    _audit("system.update.apply", f"update:{row.id} {row.target_sha[:12]} handed over",
           actor="scheduler")


_RESULT_STATUS = {"done": DONE, "rolled_back": ROLLED_BACK}


def read_result(db: Session) -> str | None:
    """Fold `apply.result` into its row (the updater writes it after the health check
    of the restarted console). Returns the update id read, if any."""
    path = updates_dir() / RESULT_FILE
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        update_id = str(data.get("update_id") or "")
        if not _ID.match(update_id):
            raise ValueError("no valid update_id")
    except (OSError, ValueError) as exc:
        logger.warning("unreadable %s: %s", path, exc)
        os.replace(path, path.with_name(f"{RESULT_FILE}.bad"))
        return None
    row = db.get(SystemUpdate, update_id)
    if row is not None and row.status in (SCHEDULED, APPLYING):
        status = _RESULT_STATUS.get(str(data.get("status")), FAILED)
        row.status = status
        previous = str(data.get("previous_sha") or "")
        row.previous_sha = previous if _SHA.match(previous) else row.previous_sha
        row.error = (str(data.get("error"))[:4000] or None) if status != DONE else None
        row.finished_at = _now()
        for line in (data.get("log") or [])[-200:]:
            _event(row, "apply", str(line))
        _event(row, "apply", f"updater result: {data.get('status')}",
               level="info" if status == DONE else "error")
        _close_announcement(db, row)
        db.commit()
        _audit("system.update.apply", f"update:{row.id} {row.target_sha[:12]} "
               f"{data.get('status')}", actor="updater",
               outcome=OUTCOME_SUCCESS if status == DONE else OUTCOME_FAILED)
    os.replace(path, path.with_name(f"{RESULT_FILE}.{update_id}"))
    return update_id


def _expire_stale_requests(db: Session, now: datetime) -> None:
    request = updates_dir() / REQUEST_FILE
    for row in db.query(SystemUpdate).filter(SystemUpdate.status == APPLYING).all():
        try:
            age = now - datetime.fromtimestamp(request.stat().st_mtime, UTC)
        except OSError:
            continue  # picked up: the updater is on it
        if age > REQUEST_STALE_AFTER:
            request.unlink(missing_ok=True)
            row.status = FAILED
            row.error = ("update.updater_timeout: the updater did not pick up the request "
                         "(systemctl status launchpad-updater.path)")
            row.finished_at = now
            _event(row, "apply", row.error, level="error")
            _close_announcement(db, row)
            db.commit()


def tick(db: Session, now: datetime | None = None) -> dict[str, Any]:
    """The scheduler task `system.update`: read back a result, expire a request nobody
    picked up, and hand a due update to the updater."""
    now = _aware(now) or _now()
    with project_context(None):
        read = read_result(db)
        _expire_stale_requests(db, now)
        handed = []
        for row in (db.query(SystemUpdate).filter(SystemUpdate.status == SCHEDULED)
                    .order_by(SystemUpdate.scheduled_at).all()):
            if _aware(row.scheduled_at) <= now and not (updates_dir() / REQUEST_FILE).exists():
                _hand_over(db, row)
                handed.append(row.id)
    return {"result_read": read, "handed_over": handed}


def resume_system_updates() -> list[str]:
    """Startup hook: read the updater's result and resume interrupted prepare jobs."""
    resumed: list[str] = []
    try:
        running_version()
        with project_context(None):
            db = SessionLocal()
            try:
                read_result(db)
                ids = [r.id for r in db.query(SystemUpdate)
                       .filter(SystemUpdate.status == PREPARING).all()]
            finally:
                db.close()
        for update_id in ids:
            if start_prepare(update_id) is not None:
                resumed.append(update_id)
    except Exception:  # noqa: BLE001 — never block startup
        logger.warning("could not resume system updates", exc_info=True)
    return resumed


# ── the page's state (§2.6) ───────────────────────────────────────────────────


def manual_commands(row: SystemUpdate) -> list[str]:
    """Without the updater: the same steps by hand, copy-paste ready."""
    stage = stage_dir_for(row.target_sha)
    move = (f"git merge --ff-only {row.target_sha}" if row.kind == KIND_UPDATE
            else f"git reset --hard {row.target_sha}")
    return [
        f"cd {REPO_DIR}",
        "(cd backend && uv run python scripts/ledger_backup.py)",
        f"git fetch --no-tags {cache_dir()} {row.target_sha}",
        move,
        "rm -rf frontend/dist.prev && mv frontend/dist frontend/dist.prev",
        f"cp -a {stage}/frontend/dist frontend/dist",
        "(cd backend && uv sync --frozen)",
        "sudo systemctl restart launchpad-backend launchpad-frontend",
        "curl -s localhost:8000/api/auth/status",
    ]


def state(db: Session) -> dict[str, Any]:
    latest = _latest(db)
    history = (db.query(SystemUpdate)
               .order_by(SystemUpdate.created_at.desc(), SystemUpdate.id.desc())
               .limit(10).all())
    settings = get_settings()
    out: dict[str, Any] = {
        "version": version_payload(),
        "source": {"repo": settings.update_repo, "branch": settings.update_branch},
        "remote": cached_check(),
        "current": serialize(latest) if latest is not None else None,
        "history": [serialize(r) | {"commits": [], "progress": {}} for r in history],
        "inflight": None,
        "manual_commands": None,
    }
    if latest is not None and latest.status in (READY, SCHEDULED):
        out["inflight"] = inflight_summary(db)
        if not can_schedule() and latest.status == READY:
            out["manual_commands"] = manual_commands(latest)
    return out
