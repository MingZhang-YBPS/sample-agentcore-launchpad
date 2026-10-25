#!/usr/bin/env bash
# Run the live-capable e2e_*.py scripts against a deployed console, in order.
#
# REAL AWS: every script creates (and cleans up) its own resources in the target
# workspace. Each script logs to <log-dir>/<name>.log; a PASS/FAIL/SKIP table is
# printed at the end and the exit code is non-zero when any script failed.
#
#   export LAUNCHPAD_E2E_USERNAME=admin LAUNCHPAD_E2E_PASSWORD=...
#   export LAUNCHPAD_E2E_MEMBER_USERNAME=developer LAUNCHPAD_E2E_MEMBER_PASSWORD=...  # 403 checks
#   export AWS_REGION=us-west-2            # the workspace region (AWS readbacks)
#   backend/scripts/run_live_e2e.sh --base https://<console> [--log-dir DIR]
#       [--only name,name] [--skip name,name] [--list]
#
# Names are the script names without `e2e_` / `.py` (e.g. experiment, traces).
#
# Order: small deploy smokes → the HR gateway agent (`gateway_tool --keep
# --long-term-memory`) feeding observability / traces / chat_memory, then that
# agent is deleted → evaluation + experiment (on `eval-target`, deleted after) →
# runtime canary → journeys and
# roadmap phases → Skill Lab assets → scripts whose preconditions are probed
# from the live env (a second / third workspace, a disposable policy gateway).
#
# The browser scripts run last, headless Chromium against the console's own URL.
#
# Never run here (each is reported as SKIP with its reason): scripts that read or
# write the LOCAL ledger (assistant_clear_canary, runtime_canary, skill_lab,
# knowledge_base, kb_gateway, docker_exec, identity_*).
#
# A run that dies on a network drop (ConnectError / ENOTFOUND) is retried after
# a pause, up to E2E_NETWORK_RETRIES times (default 2).
set -u

BASE=""
LOG_DIR=""
ONLY=""
SKIP=""
LIST=0
NETWORK_RETRIES="${E2E_NETWORK_RETRIES:-2}"
HR_AGENT="e2e-gw-hr-assistant"
EVAL_AGENT="eval-target"

usage() { sed -n '2,28p' "$0" | sed 's/^# \{0,1\}//'; }

while [ $# -gt 0 ]; do
  case "$1" in
    --base) BASE="$2"; shift 2 ;;
    --log-dir) LOG_DIR="$2"; shift 2 ;;
    --only) ONLY=",$2,"; shift 2 ;;
    --skip) SKIP=",$2,"; shift 2 ;;
    --list) LIST=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

SCRIPTS_DIR="$(cd "$(dirname "$0")" && pwd)"
BACKEND_DIR="$(dirname "$SCRIPTS_DIR")"

# name|args — `@BASE@` / `@RECORD@` / `@TARGET@` are filled in at run time
PLAN="
harness|--base @BASE@
zip_runtime|--base @BASE@
registry|--base @BASE@
policy|--base @BASE@
platform_structure|--base @BASE@
platform_ops|--base @BASE@
dlc_records_and_tools|--base @BASE@
gateway_tool|--base @BASE@ --keep --long-term-memory
observability|--base @BASE@ --agent $HR_AGENT
traces|--base @BASE@ --agent $HR_AGENT
chat_memory|--base @BASE@ --agent $HR_AGENT
@DELETE_HR_AGENT@|
eval_run|--base @BASE@
online_eval|--base @BASE@
eval_extended|--base @BASE@
experiment|--base @BASE@
@DELETE_EVAL_TARGET@|
golden_path|--base @BASE@
projects|--base @BASE@
claude_sdk|--base @BASE@ --skip-local
agent_dlc|--base @BASE@
roadmap_p0|--base @BASE@
roadmap_p1|--base @BASE@
roadmap_p2|--base @BASE@
roadmap_p3p4|--base @BASE@
skill_lab_assets|--base @BASE@ --record-id @RECORD@
skill_lab_taskgen_assets|--base @BASE@ --record-id @RECORD@
roadmap_promotion|--base @BASE@ --target @TARGET@
multi_workspace|--base @BASE@
gateway_policy_management|--base @BASE@ --gateway-id @POLICY_GW@ --statement-file @POLICY_FILE@ --confirm-real-aws
v3_console_browser|--ui @BASE@ --out @LOGDIR@/v3-shots
agent_dlc_browser|--ui @BASE@ --out @LOGDIR@/dlc-shots
skill_lab_assets_browser|--base-url @BASE@ --evidence-dir @LOGDIR@/skill-lab-assets
skill_lab_taskgen_assets_browser|--base-url @BASE@ --evidence-dir @LOGDIR@/skill-lab-taskgen
policy_deny_browser|--ui @BASE@ --agent @DENY_AGENT@ --project @DENY_PROJECT@
costs_dept_admin_browser|--ui @BASE@ --out @LOGDIR@/costs-shots
"

# never run against a deployed console: name|reason
NEVER="
assistant_clear_canary|seeds the conversation into the LOCAL ledger (refuses a remote --base)
runtime_canary|service-layer script on the LOCAL ledger (the HTTP canary is covered by experiment)
skill_lab|service-layer script on the LOCAL ledger
knowledge_base|service-layer script on the LOCAL ledger
kb_gateway|service-layer script on the LOCAL ledger
docker_exec|service-layer script on the LOCAL ledger
identity_connections|needs a local backend bound to its throwaway lpid-e2e-gw gateway + --discovery-url
identity_inbound|reads the LOCAL ledger: needs a local backend + --gateway-role-arn
identity_3lo|needs a local backend + UI and a browser
"

selected() {
  local name="$1"
  if [ -n "$ONLY" ] && [ "${ONLY#*,$name,}" = "$ONLY" ]; then return 1; fi
  if [ -n "$SKIP" ] && [ "${SKIP#*,$name,}" != "$SKIP" ]; then return 1; fi
  return 0
}

if [ "$LIST" = 1 ]; then
  echo "run order:"
  echo "$PLAN" | sed '/^$/d' | while IFS='|' read -r name args; do
    [ "$name" = "@DELETE_HR_AGENT@" ] && name="(delete $HR_AGENT)"
    [ "$name" = "@DELETE_EVAL_TARGET@" ] && name="(delete $EVAL_AGENT)"
    printf '  %-28s %s\n' "$name" "$args"
  done
  echo "never run here:"
  echo "$NEVER" | sed '/^$/d' | while IFS='|' read -r name reason; do
    printf '  %-28s %s\n' "$name" "$reason"
  done
  exit 0
fi

[ -n "$BASE" ] || { echo "--base is required" >&2; usage >&2; exit 2; }
if [ -z "${LAUNCHPAD_E2E_PASSWORD:-}${LAUNCHPAD_AUTH_PASSWORD:-}" ]; then
  echo "note: no LAUNCHPAD_E2E_PASSWORD set — fine only for a console without the login gate" >&2
fi
LOG_DIR="${LOG_DIR:-/tmp/launchpad-e2e-$(date +%Y%m%d-%H%M%S)}"
mkdir -p "$LOG_DIR"
RESULTS="$LOG_DIR/results.tsv"
: > "$RESULTS"

cd "$BACKEND_DIR" || exit 2
PY="uv run python -u"

# ── preconditions probed from the live env ────────────────────────────────────
PROBE="$($PY - "$BASE" <<'PYEOF' 2>"$LOG_DIR/preflight.log"
import sys
sys.path.insert(0, "scripts")
from _e2e_client import e2e_client

client = e2e_client(sys.argv[1], timeout=300)  # the registry list can be slow
workspaces = client.get("/api/workspaces").json().get("workspaces", [])
ready = [w for w in workspaces if w.get("bootstrap_status") == "ready"]
target = next((w["id"] for w in ready if not w.get("is_default")
               and w.get("tier") != "prod"), "")
records = client.get("/api/registry/records", params={"type": "AGENT_SKILLS"})
record = next((r["record_id"] for r in records.json().get("records", [])), "")
print(f"WORKSPACES={len(ready)}")
print(f"TARGET={target}")
print(f"RECORD={record}")
PYEOF
)"
if [ $? -ne 0 ]; then
  echo "preflight against $BASE failed:" >&2
  tail -5 "$LOG_DIR/preflight.log" >&2
  exit 2
fi
WORKSPACES="$(echo "$PROBE" | sed -n 's/^WORKSPACES=//p')"
TARGET="$(echo "$PROBE" | sed -n 's/^TARGET=//p')"
RECORD="$(echo "$PROBE" | sed -n 's/^RECORD=//p')"
POLICY_GW="${E2E_POLICY_GATEWAY_ID:-}"
POLICY_FILE="${E2E_POLICY_STATEMENT_FILE:-}"
DENY_AGENT="${E2E_POLICY_DENY_AGENT:-}"
DENY_PROJECT="${E2E_POLICY_DENY_PROJECT:-default}"
echo "── $BASE · ready workspaces=$WORKSPACES · promotion target=${TARGET:-none}" \
     "· skill record=${RECORD:-none} · logs in $LOG_DIR"

skip_reason() {
  case "$1" in
    skill_lab_assets|skill_lab_taskgen_assets)
      [ -n "$RECORD" ] || echo "no AGENT_SKILLS registry record on this env" ;;
    roadmap_promotion)
      [ -n "$TARGET" ] || echo "needs a second ready, non-prod workspace (found $WORKSPACES ready)" ;;
    multi_workspace)
      [ "${WORKSPACES:-0}" -ge 3 ] || echo "needs 3 ready workspaces (found $WORKSPACES)" ;;
    gateway_policy_management)
      [ -n "$POLICY_GW" ] && [ -n "$POLICY_FILE" ] \
        || echo "set E2E_POLICY_GATEWAY_ID (a disposable gateway) + E2E_POLICY_STATEMENT_FILE" ;;
    policy_deny_browser)
      [ -n "$DENY_AGENT" ] || echo "set E2E_POLICY_DENY_AGENT (an active agent id; E2E_POLICY_DENY_PROJECT if not in default)" ;;
    observability|traces|chat_memory)
      grep -q "^gateway_tool	PASS" "$RESULTS" \
        || echo "gateway_tool did not leave $HR_AGENT deployed" ;;
  esac
}

record() { printf '%s\t%s\t%s\t%s\n' "$1" "$2" "$3" "$4" >> "$RESULTS"; }

network_drop() {
  grep -Eq "ConnectError|ENOTFOUND|nodename nor servname|Temporary failure in name resolution|Network is unreachable" "$1"
}

run_one() {
  local name="$1" args="$2" log="$LOG_DIR/$1.log" started rc attempt=0
  args="${args//@BASE@/$BASE}"
  args="${args//@RECORD@/$RECORD}"
  args="${args//@TARGET@/$TARGET}"
  args="${args//@POLICY_GW@/$POLICY_GW}"
  args="${args//@POLICY_FILE@/$POLICY_FILE}"
  args="${args//@LOGDIR@/$LOG_DIR}"
  args="${args//@DENY_AGENT@/$DENY_AGENT}"
  args="${args//@DENY_PROJECT@/$DENY_PROJECT}"
  started=$(date +%s)
  while :; do
    printf '── %-28s ' "$name"
    # shellcheck disable=SC2086  # args is a word list by construction
    $PY "scripts/e2e_$name.py" $args < /dev/null > "$log" 2>&1
    rc=$?
    if [ $rc -ne 0 ] && network_drop "$log" && [ $attempt -lt "$NETWORK_RETRIES" ]; then
      attempt=$((attempt + 1))
      echo "network drop — retrying in 60s ($attempt/$NETWORK_RETRIES)"
      mv "$log" "$log.drop$attempt"
      sleep 60
      continue
    fi
    break
  done
  local secs=$(( $(date +%s) - started ))
  if [ $rc -eq 0 ]; then
    echo "PASS (${secs}s)"
    local detail
    detail="$(grep -E ': PASS' "$log" | tail -1)"
    [ -n "$detail" ] || detail="$(grep -v '^[[:space:]]*$' "$log" | tail -1)"
    record "$name" PASS "${secs}s" "$(echo "$detail" | cut -c1-80)"
  else
    echo "FAIL rc=$rc (${secs}s) — $log"
    record "$name" FAIL "${secs}s" "$(grep -v '^[[:space:]]*$' "$log" | tail -1 | cut -c1-80)"
  fi
}

delete_agent() {
  local agent="$1" log="$LOG_DIR/delete_$1.log"
  $PY - "$BASE" "$agent" <<'PYEOF' > "$log" 2>&1
import sys
sys.path.insert(0, "scripts")
from _e2e_client import e2e_client

client = e2e_client(sys.argv[1], timeout=120)
for agent in client.get("/api/agents").json()["agents"]:
    if agent["name"] == sys.argv[2] and agent["status"] != "deleted":
        client.delete(f"/api/agents/{agent['id']}").raise_for_status()
        print(f"deleted {agent['name']} ({agent['id']})")
PYEOF
  local rc=$?
  printf '── %-28s %s\n' "(delete $agent)" "$([ $rc -eq 0 ] && echo ok || echo "FAILED — $log")"
}

PLAN_FILE="$LOG_DIR/.plan"
echo "$PLAN" | sed '/^$/d' > "$PLAN_FILE"
while IFS='|' read -r name args; do
  if [ "$name" = "@DELETE_HR_AGENT@" ]; then
    grep -q "^gateway_tool	PASS" "$RESULTS" && delete_agent "$HR_AGENT"
    continue
  fi
  if [ "$name" = "@DELETE_EVAL_TARGET@" ]; then
    # eval_run deploys it; online_eval / eval_extended / experiment (which needs the
    # traces the eval runs left) reuse it; nothing after needs it
    grep -Eq "^(eval_run|online_eval|eval_extended|experiment)	" "$RESULTS" \
      && delete_agent "$EVAL_AGENT"
    continue
  fi
  selected "$name" || continue
  reason="$(skip_reason "$name")"
  if [ -n "$reason" ]; then
    printf '── %-28s SKIP — %s\n' "$name" "$reason"
    record "$name" SKIP - "$reason"
    continue
  fi
  run_one "$name" "$args"
done < "$PLAN_FILE"
rm -f "$PLAN_FILE"

echo "$NEVER" | sed '/^$/d' | while IFS='|' read -r name reason; do
  selected "$name" && record "$name" SKIP - "$reason"
done

echo
printf '%-28s %-5s %-7s %s\n' SCRIPT RESULT TIME DETAIL
printf '%-28s %-5s %-7s %s\n' ------ ------ ---- ------
while IFS="$(printf '\t')" read -r name result secs detail; do
  printf '%-28s %-6s %-7s %s\n' "$name" "$result" "$secs" "$detail"
done < "$RESULTS"
echo "logs: $LOG_DIR"
! grep -q "	FAIL	" "$RESULTS"
