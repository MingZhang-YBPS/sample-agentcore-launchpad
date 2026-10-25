#!/usr/bin/env bash
# Launchpad online updater (docs/design online-update §2.4) — the root half.
#
# Run by launchpad-updater.service when launchpad-updater.path sees
# $DATA_DIR/updates/apply.request. install_updater.sh copies THIS file to
# /usr/local/lib/launchpad/apply_update.sh: an update never replaces the script that
# applies it, so an update package cannot rewrite its own privilege boundary.
#
# The request carries only {update_id, sha, action}; everything else is fixed here:
#   1. backup the ledger online (backend/scripts/ledger_backup.py, verified)
#   2. update: git fetch the stage cache + git merge --ff-only <sha>
#      rollback: <sha> must be an ancestor of the running commit; git reset --hard <sha>
#      then swap the stage's built frontend/dist in by rename (node_modules too when
#      the lockfile changed), uv sync --frozen
#   3. systemctl restart the console's units
#   4. health check: GET $HEALTH_URL until version.sha == <sha> (HEALTH_TIMEOUT s)
#   5. on any failure after 2 started: reset to the previous commit, restore dist,
#      uv sync, restart, health check on the previous commit -> rolled_back
#   6. $DATA_DIR/updates/apply.result for the console to read back
# Only systemctl runs as root; git, uv, the copy and the backup run as the checkout's
# owner, so a crafted stage cannot reach anything that owner could not.
#
# Configuration (environment, /etc/launchpad-updater.env):
#   LAUNCHPAD_REPO_DIR (required), LAUNCHPAD_DATA_DIR (default $REPO_DIR/data),
#   LAUNCHPAD_OWNER (default: the checkout's owner), LAUNCHPAD_SERVICES
#   (default "launchpad-backend launchpad-frontend"; units that do not exist are
#   skipped), LAUNCHPAD_HEALTH_URL (default http://127.0.0.1:8000/api/auth/status),
#   LAUNCHPAD_HEALTH_TIMEOUT (120), LAUNCHPAD_LEDGER_DB (default $DATA_DIR/launchpad.db),
#   LAUNCHPAD_SYSTEMCTL / LAUNCHPAD_PYTHON / LAUNCHPAD_UV (tests point these at fakes).
set -uo pipefail

REPO_DIR=${LAUNCHPAD_REPO_DIR:?LAUNCHPAD_REPO_DIR is required}
DATA_DIR=${LAUNCHPAD_DATA_DIR:-$REPO_DIR/data}
UPDATES=$DATA_DIR/updates
SERVICES=${LAUNCHPAD_SERVICES:-launchpad-backend launchpad-frontend}
HEALTH_URL=${LAUNCHPAD_HEALTH_URL:-http://127.0.0.1:8000/api/auth/status}
HEALTH_TIMEOUT=${LAUNCHPAD_HEALTH_TIMEOUT:-120}
LEDGER_DB=${LAUNCHPAD_LEDGER_DB:-$DATA_DIR/launchpad.db}
SYSTEMCTL=${LAUNCHPAD_SYSTEMCTL:-systemctl}
PYTHON=${LAUNCHPAD_PYTHON:-python3}
UV=${LAUNCHPAD_UV:-uv}
OWNER=${LAUNCHPAD_OWNER:-$(stat -c %U "$REPO_DIR" 2>/dev/null || stat -f %Su "$REPO_DIR")}

REQUEST=$UPDATES/apply.request
PROCESSING=$UPDATES/apply.processing
RESULT=$UPDATES/apply.result
LOG=$UPDATES/apply.log

UPDATE_ID="" SHA="" ACTION="" PREV="" DIST_SWAPPED=no MODULES_SWAPPED=no FAIL=""

log() {
  local line
  line="$(date -u +%Y-%m-%dT%H:%M:%SZ) $*"
  FAIL="$*"
  echo "$line"
  echo "$line" >>"$LOG"
  STEPS+=("$*")
}
STEPS=()

as_owner() {
  if [ "$(id -u)" = 0 ] && [ "$OWNER" != root ]; then
    local home
    home=$(getent passwd "$OWNER" | cut -d: -f6)
    runuser -u "$OWNER" -- env HOME="$home" PATH="$PATH" "$@"
  else
    "$@"
  fi
}

finish() {  # finish <status> <error>
  local status=$1 error=${2:-}
  printf '%s\n' "${STEPS[@]}" | "$PYTHON" -c '
import json, sys
uid, sha, prev, status, error, out = sys.argv[1:7]
log = [line for line in sys.stdin.read().splitlines() if line]
tmp = out + ".tmp"
with open(tmp, "w", encoding="utf-8") as fh:
    json.dump({"update_id": uid, "sha": sha, "previous_sha": prev or None,
               "status": status, "error": error or None, "log": log[-200:]}, fh)
import os
os.replace(tmp, out)
' "$UPDATE_ID" "$SHA" "$PREV" "$status" "$error" "$RESULT"
  [ "$(id -u)" = 0 ] && chown "$OWNER" "$RESULT" 2>/dev/null
  rm -f "$PROCESSING"
  log "result: $status${error:+ ($error)}"
  [ "$status" = done ] && exit 0
  exit 1
}

# ---- the request: id + sha + action, validated before anything runs ----
[ -f "$REQUEST" ] || exit 0
mkdir -p "$UPDATES"
mv -f "$REQUEST" "$PROCESSING"  # a PathExists unit must not re-trigger on it
read -r UPDATE_ID SHA ACTION < <("$PYTHON" -c '
import json, re, sys
try:
    data = json.load(open(sys.argv[1], encoding="utf-8"))
except Exception:
    data = {}
uid, sha, action = str(data.get("update_id", "")), str(data.get("sha", "")), str(data.get("action", "update"))
ok = re.fullmatch(r"[0-9a-f]{32}", uid) and re.fullmatch(r"[0-9a-f]{40}", sha) and action in ("update", "rollback")
print(uid if re.fullmatch(r"[0-9a-f]{32}", uid) else "-", sha if ok else "-", action if ok else "-")
' "$PROCESSING")
if [ "$UPDATE_ID" = "-" ]; then
  UPDATE_ID="" SHA=""
  log "malformed request: no valid update_id"
  rm -f "$PROCESSING"
  exit 1
fi
if [ "$SHA" = "-" ]; then
  SHA=""
  finish failed "update.request_invalid: malformed sha or action"
fi
STAGE=$UPDATES/stage-$SHA
CACHE=$UPDATES/repo.git
log "request $UPDATE_ID: $ACTION to $SHA"

if [ "$(cat "$STAGE/.launchpad-stage-ok" 2>/dev/null)" != "$SHA" ] || [ ! -d "$STAGE/frontend/dist" ]; then
  finish failed "update.stage_missing: $STAGE is not a prepared stage of $SHA"
fi

PREV=$(as_owner git -C "$REPO_DIR" rev-parse HEAD) || finish failed "update.no_git: cannot read HEAD"
log "running commit $PREV"
if [ "$ACTION" = update ]; then
  as_owner git -C "$CACHE" cat-file -e "$SHA^{commit}" 2>/dev/null \
    || finish failed "update.not_cached: $SHA is not in the update cache"
  as_owner git -C "$REPO_DIR" fetch --no-tags --quiet "$CACHE" "$SHA" \
    || finish failed "update.fetch_failed: git fetch from the cache failed"
  as_owner git -C "$REPO_DIR" merge-base --is-ancestor "$PREV" "$SHA" \
    || finish failed "update.not_fast_forward: $SHA is not a descendant of $PREV"
else
  as_owner git -C "$REPO_DIR" merge-base --is-ancestor "$SHA" "$PREV" \
    || finish failed "update.not_fast_forward: $SHA is not an ancestor of $PREV"
fi

# ---- 1. backup ----
if [ -f "$LEDGER_DB" ]; then
  as_owner "$PYTHON" "$REPO_DIR/backend/scripts/ledger_backup.py" --db "$LEDGER_DB" --keep 10 >>"$LOG" 2>&1 \
    || finish failed "update.backup_failed: ledger_backup.py failed (nothing changed)"
  log "ledger backed up"
else
  log "no ledger at $LEDGER_DB; backup skipped"
fi

units() {
  local found="" unit
  for unit in $SERVICES; do
    if "$SYSTEMCTL" cat "$unit" >/dev/null 2>&1; then found="$found $unit"; fi
  done
  echo "${found# }"
}

healthy() {  # healthy <sha>: the restarted console reports <sha> within HEALTH_TIMEOUT
  "$PYTHON" -c '
import json, sys, time, urllib.request
url, want, budget = sys.argv[1], sys.argv[2], float(sys.argv[3])
deadline = time.monotonic() + budget
while time.monotonic() < deadline:
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            if resp.status == 200 and (json.load(resp).get("version") or {}).get("sha") == want:
                sys.exit(0)
    except Exception:
        pass
    time.sleep(1)
sys.exit(1)
' "$HEALTH_URL" "$1" "$HEALTH_TIMEOUT"
}

restart() {
  local list
  list=$(units)
  if [ -z "$list" ]; then
    log "no console unit among: $SERVICES"
    return 1
  fi
  log "systemctl restart $list"
  # shellcheck disable=SC2086
  "$SYSTEMCTL" restart $list
}

lock_changed() {
  ! as_owner git -C "$REPO_DIR" diff --quiet "$PREV" "$SHA" -- frontend/package-lock.json
}

apply() {
  if [ "$ACTION" = update ]; then
    as_owner git -C "$REPO_DIR" merge --ff-only --quiet "$SHA" || { log "git merge --ff-only failed"; return 1; }
  else
    as_owner git -C "$REPO_DIR" reset --hard --quiet "$SHA" || { log "git reset failed"; return 1; }
  fi
  log "checkout at $SHA"
  local fe=$REPO_DIR/frontend
  as_owner rm -rf "$fe/dist.next" "$fe/dist.prev"
  as_owner cp -a "$STAGE/frontend/dist" "$fe/dist.next" || { log "copying dist failed"; return 1; }
  if [ -d "$fe/dist" ]; then as_owner mv "$fe/dist" "$fe/dist.prev"; fi
  DIST_SWAPPED=yes
  as_owner mv "$fe/dist.next" "$fe/dist" || { log "swapping dist failed"; return 1; }
  log "frontend/dist swapped in"
  if lock_changed && [ -d "$STAGE/frontend/node_modules" ]; then
    as_owner rm -rf "$fe/node_modules.prev"
    if [ -d "$fe/node_modules" ]; then as_owner mv "$fe/node_modules" "$fe/node_modules.prev"; fi
    MODULES_SWAPPED=yes
    as_owner mv "$STAGE/frontend/node_modules" "$fe/node_modules" || { log "swapping node_modules failed"; return 1; }
    log "frontend/node_modules swapped in (lockfile changed)"
  fi
  (cd "$REPO_DIR/backend" && as_owner "$UV" sync --frozen) >>"$LOG" 2>&1 || { log "uv sync --frozen failed"; return 1; }
  log "backend synced"
  restart || return 1
  if ! healthy "$SHA"; then
    log "health check: $HEALTH_URL did not report $SHA within ${HEALTH_TIMEOUT}s"
    return 1
  fi
  log "health check passed"
}

revert() {
  local fe=$REPO_DIR/frontend
  as_owner git -C "$REPO_DIR" reset --hard --quiet "$PREV" || log "git reset to $PREV failed"
  if [ "$DIST_SWAPPED" = yes ] && [ -d "$fe/dist.prev" ]; then
    as_owner rm -rf "$fe/dist"
    as_owner mv "$fe/dist.prev" "$fe/dist"
  fi
  if [ "$MODULES_SWAPPED" = yes ] && [ -d "$fe/node_modules.prev" ]; then
    as_owner rm -rf "$fe/node_modules"
    as_owner mv "$fe/node_modules.prev" "$fe/node_modules"
  fi
  (cd "$REPO_DIR/backend" && as_owner "$UV" sync --frozen) >>"$LOG" 2>&1 || log "uv sync on rollback failed"
  restart || return 1
  healthy "$PREV"
}

if apply; then
  finish done ""
fi
REASON=$FAIL
log "rolling back to $PREV"
if revert; then
  finish rolled_back "update.apply_failed: $REASON — rolled back to $PREV"
fi
finish failed "update.rollback_failed: the console did not come back on $PREV — see $LOG"
