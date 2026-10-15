"""The root half of the online update (online-update §2.4, §3): `apply_update.sh` and
`install_updater.sh`, run with bash in temporary git repositories, a fake `systemctl`
and `uv`, and a local HTTP server standing in for the restarted console."""

import json
import shutil
import sqlite3
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
APPLY = BACKEND / "scripts" / "apply_update.sh"
INSTALL = BACKEND / "scripts" / "install_updater.sh"
UPDATE_ID = "a" * 32

pytestmark = pytest.mark.skipif(shutil.which("git") is None or shutil.which("bash") is None,
                                reason="needs git and bash")


def sh(*args: str, cwd: Path) -> str:
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def commit(repo: Path, message: str, files: dict[str, str]) -> str:
    for name, text in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    sh("git", "add", "-A", cwd=repo)
    sh("git", "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m",
       message, cwd=repo)
    return sh("git", "rev-parse", "HEAD", cwd=repo)


def fake_bin(path: Path, body: str) -> Path:
    path.write_text("#!/usr/bin/env bash\n" + body, encoding="utf-8")
    path.chmod(0o755)
    return path


class _Console(BaseHTTPRequestHandler):
    repo: Path
    refuse: set[str]

    def do_GET(self):  # noqa: N802
        head = sh("git", "rev-parse", "HEAD", cwd=self.repo)
        if head in self.refuse:
            self.send_response(503)
            self.end_headers()
            return
        body = json.dumps({"version": {"sha": head}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


@pytest.fixture
def host(tmp_path):
    """origin (c1 → c2), the live checkout at c1 with a built dist, the update cache,
    and a prepared stage of c2 — what the web process leaves before the hand-over."""
    origin = tmp_path / "origin"
    origin.mkdir()
    sh("git", "init", "-q", "-b", "main", cwd=origin)
    base = {
        ".gitignore": "data/\nfrontend/dist*\nfrontend/node_modules*\n",
        "backend/scripts/ledger_backup.py":
            (BACKEND / "scripts" / "ledger_backup.py").read_text(encoding="utf-8"),
        "frontend/package-lock.json": "{}\n",
        "README.md": "one\n",
    }
    c1 = commit(origin, "c1", base)
    live = tmp_path / "live"
    sh("git", "clone", "-q", str(origin), str(live), cwd=tmp_path)
    c2 = commit(origin, "c2", {"README.md": "two\n"})
    (live / "frontend" / "dist").mkdir(parents=True)
    (live / "frontend" / "dist" / "index.html").write_text("c1", encoding="utf-8")
    updates = live / "data" / "updates"
    updates.mkdir(parents=True)
    sh("git", "clone", "-q", "--bare", str(origin), str(updates / "repo.git"), cwd=tmp_path)
    stage = updates / f"stage-{c2}"
    sh("git", "-C", str(updates / "repo.git"), "worktree", "add", "-q", "--detach", str(stage),
       c2, cwd=tmp_path)
    (stage / "frontend" / "dist").mkdir(parents=True)
    (stage / "frontend" / "dist" / "index.html").write_text("c2", encoding="utf-8")
    (stage / ".launchpad-stage-ok").write_text(c2 + "\n", encoding="utf-8")
    ledger = live / "data" / "launchpad.db"
    with sqlite3.connect(ledger) as conn:
        conn.execute("CREATE TABLE agents (id TEXT)")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "calls.log"
    systemctl = fake_bin(bin_dir / "systemctl", f"""
echo "systemctl $*" >>{calls}
case "$1" in
  cat) [ "$2" = launchpad-backend ] ;;
  *) exit 0 ;;
esac
""")
    uv = fake_bin(bin_dir / "uv", f'echo "uv $* in $PWD" >>{calls}\n')

    handler = type("Console", (_Console,), {"repo": live, "refuse": set()})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    env = {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin:/usr/local/bin:/opt/homebrew/bin",
        "HOME": str(tmp_path),
        "LAUNCHPAD_REPO_DIR": str(live),
        "LAUNCHPAD_OWNER": sh("id", "-un", cwd=tmp_path),
        "LAUNCHPAD_SERVICES": "launchpad-backend launchpad-frontend",
        "LAUNCHPAD_HEALTH_URL": f"http://127.0.0.1:{server.server_port}/api/auth/status",
        "LAUNCHPAD_HEALTH_TIMEOUT": "3",
        "LAUNCHPAD_SYSTEMCTL": str(systemctl),
        "LAUNCHPAD_UV": str(uv),
        "LAUNCHPAD_PYTHON": shutil.which("python3") or "python3",
    }
    yield {"live": live, "updates": updates, "c1": c1, "c2": c2, "env": env, "calls": calls,
           "refuse": handler.refuse, "origin": origin, "tmp": tmp_path}
    server.shutdown()


def apply(host, payload) -> tuple[int, dict | None]:
    (host["updates"] / "apply.request").write_text(json.dumps(payload), encoding="utf-8")
    done = subprocess.run(["bash", str(APPLY)], env=host["env"], capture_output=True,
                          text=True, timeout=120, check=False)
    result = host["updates"] / "apply.result"
    return done.returncode, (json.loads(result.read_text()) if result.exists() else None)


def head(host) -> str:
    return sh("git", "rev-parse", "HEAD", cwd=host["live"])


def dist(host) -> str:
    return (host["live"] / "frontend" / "dist" / "index.html").read_text()


def test_applies_a_fast_forward_and_passes_the_health_check(host):
    code, result = apply(host, {"update_id": UPDATE_ID, "sha": host["c2"], "action": "update"})
    assert code == 0, result
    assert result["status"] == "done" and result["update_id"] == UPDATE_ID
    assert result["previous_sha"] == host["c1"] and result["error"] is None
    assert head(host) == host["c2"] and dist(host) == "c2"
    calls = host["calls"].read_text()
    # only the unit that exists is restarted; the backend is synced frozen
    assert "systemctl restart launchpad-backend\n" in calls
    assert "uv sync --frozen" in calls
    assert list((host["live"] / "data").glob("launchpad.db.bak-*")), "the ledger is backed up first"
    assert not (host["updates"] / "apply.request").exists()
    assert not (host["updates"] / "apply.processing").exists()


def test_health_failure_rolls_back_to_the_previous_commit(host):
    host["refuse"].add(host["c2"])  # the new code never reports healthy
    code, result = apply(host, {"update_id": UPDATE_ID, "sha": host["c2"], "action": "update"})
    assert code == 1
    assert result["status"] == "rolled_back"
    assert "health check" in result["error"] and host["c1"] in result["error"]
    assert head(host) == host["c1"] and dist(host) == "c1"
    assert host["calls"].read_text().count("systemctl restart") == 2


def test_a_failed_rollback_is_reported_as_failed(host):
    host["refuse"].update({host["c1"], host["c2"]})
    code, result = apply(host, {"update_id": UPDATE_ID, "sha": host["c2"], "action": "update"})
    assert code == 1 and result["status"] == "failed"
    assert result["error"].startswith("update.rollback_failed")


def test_refuses_a_commit_that_is_not_a_fast_forward(host):
    # a rewritten branch: c3 does not descend from the running c1
    origin = host["origin"]
    sh("git", "checkout", "-q", "--orphan", "rewritten", cwd=origin)
    c3 = commit(origin, "c3", {"README.md": "three\n"})
    cache = host["updates"] / "repo.git"
    sh("git", "fetch", "-q", str(origin), "rewritten", cwd=cache)
    stage = host["updates"] / f"stage-{c3}"
    (stage / "frontend" / "dist").mkdir(parents=True)
    (stage / ".launchpad-stage-ok").write_text(c3, encoding="utf-8")
    code, result = apply(host, {"update_id": UPDATE_ID, "sha": c3, "action": "update"})
    assert code == 1 and result["status"] == "failed"
    assert result["error"].startswith("update.not_fast_forward")
    assert head(host) == host["c1"] and dist(host) == "c1"
    assert "restart" not in (host["calls"].read_text() if host["calls"].exists() else "")


def test_refuses_an_unprepared_stage(host):
    (host["updates"] / f"stage-{host['c2']}" / ".launchpad-stage-ok").unlink()
    code, result = apply(host, {"update_id": UPDATE_ID, "sha": host["c2"]})
    assert code == 1 and result["error"].startswith("update.stage_missing")
    assert head(host) == host["c1"]


@pytest.mark.parametrize("payload", [
    {"update_id": UPDATE_ID, "sha": "HEAD; rm -rf /"},
    {"update_id": UPDATE_ID, "sha": "b" * 40, "action": "shell"},
])
def test_rejects_a_malformed_request(host, payload):
    code, result = apply(host, payload)
    assert code == 1 and result["status"] == "failed"
    assert result["error"].startswith("update.request_invalid")
    assert head(host) == host["c1"]


def test_a_request_without_an_id_writes_no_result(host):
    code, result = apply(host, {"sha": host["c2"]})
    assert code == 1 and result is None
    assert not (host["updates"] / "apply.processing").exists()


def test_rollback_returns_to_an_ancestor(host):
    assert apply(host, {"update_id": UPDATE_ID, "sha": host["c2"]})[1]["status"] == "done"
    (host["updates"] / "apply.result").unlink()
    stage = host["updates"] / f"stage-{host['c1']}"
    (stage / "frontend" / "dist").mkdir(parents=True)
    (stage / "frontend" / "dist" / "index.html").write_text("c1-rebuilt", encoding="utf-8")
    (stage / ".launchpad-stage-ok").write_text(host["c1"], encoding="utf-8")
    code, result = apply(host, {"update_id": "b" * 32, "sha": host["c1"], "action": "rollback"})
    assert code == 0, result
    assert result["status"] == "done" and result["previous_sha"] == host["c2"]
    assert head(host) == host["c1"] and dist(host) == "c1-rebuilt"


def test_no_request_is_a_no_op(host):
    done = subprocess.run(["bash", str(APPLY)], env=host["env"], capture_output=True,
                          text=True, timeout=30, check=False)
    assert done.returncode == 0
    assert not (host["updates"] / "apply.result").exists()


def test_installer_writes_the_units_and_a_copy_of_the_script(host):
    root = host["tmp"] / "root"
    env = {**host["env"], "LAUNCHPAD_UPDATER_ROOT": str(root)}
    done = subprocess.run(
        ["bash", str(INSTALL), "--repo", str(host["live"]), "--user", host["env"]["LAUNCHPAD_OWNER"]],
        env=env, capture_output=True, text=True, timeout=30, check=False)
    assert done.returncode == 0, done.stderr
    copy = root / "usr/local/lib/launchpad/apply_update.sh"
    assert copy.read_text() == APPLY.read_text()
    path_unit = (root / "etc/systemd/system/launchpad-updater.path").read_text()
    assert f"PathExists={host['live']}/data/updates/apply.request" in path_unit
    service = (root / "etc/systemd/system/launchpad-updater.service").read_text()
    assert "Type=oneshot" in service
    assert "ExecStart=/usr/local/lib/launchpad/apply_update.sh" in service
    conf = (root / "etc/launchpad-updater.env").read_text()
    assert f"LAUNCHPAD_REPO_DIR={host['live']}" in conf
    # only the units this host has
    assert "LAUNCHPAD_SERVICES=launchpad-backend\n" in conf
    calls = host["calls"].read_text()
    assert "systemctl daemon-reload" in calls
    assert "systemctl enable --now launchpad-updater.path" in calls


def test_installer_refuses_to_run_unprivileged_without_a_test_root(host):
    if subprocess.run(["id", "-u"], capture_output=True, text=True).stdout.strip() == "0":
        pytest.skip("running as root")
    done = subprocess.run(["bash", str(INSTALL)], env=host["env"], capture_output=True,
                          text=True, timeout=30, check=False)
    assert done.returncode == 1 and "root" in done.stderr
