#!/usr/bin/env bash
# Local ARM64 smoke test for the Claude SDK container template.
# Builds the image natively (host is aarch64), runs it with the host's AWS
# credentials, and asserts a Bedrock-backed answer — the pre-CodeBuild gate.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# docker, else finch (macOS without Docker Desktop); LAUNCHPAD_CONTAINER_CLI overrides.
CLI="${LAUNCHPAD_CONTAINER_CLI:-$(command -v docker || command -v finch || echo docker)}"
# finch builds inside a VM that only shares $HOME, so a /tmp context is invisible to it.
if [[ "$(basename "$CLI")" == finch ]]; then
  CTX="${HOME}/.cache/launchpad/claude_sdk_smoke_ctx"
else
  CTX=/tmp/claude_sdk_smoke_ctx
fi
NAME=launchpad-claude-smoke
PORT="${SMOKE_PORT:-18080}"

echo "── rendering build context"
(cd "$ROOT/backend" && uv run python - <<PY
from pathlib import Path
from app.schemas.agent import AgentSpec
from app.templates.claude_sdk_agent import assemble_build_context

spec = AgentSpec(
    name="local-smoke",
    method="container",
    system_prompt="You are a terse math assistant. Answer with just the number.",
)
assemble_build_context(spec, Path("$CTX"))
print("context:", sorted(p.name for p in Path("$CTX").iterdir()))
PY
)

echo "── $(basename "$CLI") build (linux/arm64)"
"$CLI" build --platform linux/arm64 -t "$NAME" "$CTX"

echo "── $(basename "$CLI") run"
"$CLI" rm -f "$NAME" >/dev/null 2>&1 || true
ENVFILE=
cleanup() {
  [[ -z "$ENVFILE" ]] || rm -f "$ENVFILE"
  "$CLI" rm -f "$NAME" >/dev/null 2>&1 || true
}
trap cleanup EXIT

ENVFILE=$(mktemp)
aws configure export-credentials --format env-no-export > "$ENVFILE"
echo "AWS_REGION=${AWS_REGION:-us-west-2}" >> "$ENVFILE"
# bedrock_agentcore binds 0.0.0.0 only when it detects a container via
# /.dockerenv, which docker creates but finch/containerd does not; without this
# the server listens on 127.0.0.1 inside the container and -p gets empty replies.
echo "DOCKER_CONTAINER=1" >> "$ENVFILE"
"$CLI" run -d --name "$NAME" --env-file "$ENVFILE" -p "$PORT:8080" "$NAME" >/dev/null
rm -f "$ENVFILE"
ENVFILE=

echo "── waiting for /ping"
for _ in $(seq 1 30); do
  curl -sf "localhost:$PORT/ping" >/dev/null 2>&1 && break
  sleep 2
done
curl -sf "localhost:$PORT/ping" >/dev/null || { echo "container never became healthy"; "$CLI" logs "$NAME" | tail -20; exit 1; }

echo "── checking optional subagent scaffold in image"
"$CLI" exec "$NAME" sh -c \
  'test ! -d /app/.claude/agents || ls /app/.claude/agents/'

echo "── invoking: what is 2+2?"
ANSWER=$(curl -sf -X POST "localhost:$PORT/invocations" \
  -H 'Content-Type: application/json' \
  -d '{"prompt": "What is 2+2? Reply with just the number."}')
echo "response: $ANSWER"
echo "$ANSWER" | grep -q 4 || { echo "SMOKE FAIL: expected '4' in answer"; exit 1; }
echo "LOCAL CONTAINER SMOKE: PASS"
