"""Console authorization: one declarative table for every `/api` route.

Why a table instead of per-route `Depends(require_admin)`:

* a reviewer can audit the whole authorization posture in one file rather than by
  grepping 19 routers;
* it is **default-deny** — a route with no entry raises instead of silently
  serving, so a newly added endpoint cannot ship unclassified. `tests/
  test_route_policy.py` enumerates the live routes and fails on drift in both
  directions.

Roles: `ADMIN` requires an administrator identity; `MEMBER` requires only a live
session (the `auth_middleware` 401 already covers that); `PUBLIC` is reachable
without one (health, docs, and the login surface itself). A `perm:<key>` value
requires that member permission (admins implicitly hold all; a member holds every
key unless an admin stored an explicit denial on their account — see
`auth.AGENT_PERMISSIONS` and the Users console).

The classification principle, signed off by river 2026-08-03: **admin for routes
that execute code, change deployed or cloud state, mint credentials, or change
governance posture; member for reads and for the member's own interaction with an
agent.** Amended by river 2026-08-07: the agent-lifecycle routes (deploy,
discovery import, delete, convert and the deploy-flow skill helpers) are
member-grantable via `perm:agents.*`, **default granted**, revocable per user.
Amended by river 2026-08-10: starting evaluation/insights runs
(`POST /api/eval/runs`) is member-grantable via `perm:eval.run` on the same
default-granted terms — it invokes agents (member parity with Chat) and creates
billable AWS eval jobs, which revocation can still shut off per user.
Amended by river 2026-08-11: **the whole console is member-reachable except user
management** — every route that used to demand `ADMIN` (registry writes, knowledge
bases, governance, evaluation datasets/evaluators, experiments, canaries, API keys,
studio local exec, tools/demos, prices refresh) is now `MEMBER`; only `/api/users*`
still requires an administrator. The `perm:*` entries keep their revocation
semantics unchanged.
Amended 2026-10-04 (issue #55): creating, editing and deleting AgentCore Memory
resources (`/api/memory/resources` writes) is member-grantable via
`perm:memory.manage` on the same default-granted, revocable terms — DeleteMemory
is irreversible — and those routes reach only memories the workspace manages.
Adopting an existing account memory into management is `ADMIN`.
Amended 2026-10-08: the architect assistant's evaluation-assets routes (materialize,
retry, Lambda revision review, cleanup) are member-grantable via `perm:agents.deploy`
— the same permission that approves a proposal into a deployed Agent — instead of
`ADMIN`; clearing a conversation with assets needs the matching `perm:` keys.
Since 2026-08-12 the table carries a **second dimension**: whether a route
operates inside a workspace (one account/region environment). `WORKSPACE_EXEMPT`
names the hub-global routes; every other entry is workspace-scoped, and
`enforce_route_policy` resolves + authorizes the caller's workspace for it
before the handler runs (`routers/workspaces.resolve_workspace`). Absence from
the exempt set *is* the classification, so a new route cannot ship without one.

Consequences worth knowing before editing this table:

* Data is partitioned per workspace, not per user: within a workspace every
  member still sees and can mutate the same shared agents, records, datasets and
  gateways, but a member only reaches the workspaces an admin granted them
  (`user_workspaces`), and a resource id belonging to another workspace answers
  404. `ADMIN` marks user/workspace management, announcement publication and
  the gateway's consent portal (it passes an operator-named IAM role);
  it does not generically mark "state changes".
* The studio local-exec surface (`/api/execute*`, conversations writes) stays safe
  in production through its own handler guard (`local_exec`, refused outright in
  prod unless explicitly opted in) — that guard, not this table, is the real
  boundary there.
* Invoking an agent is deliberately `MEMBER` (`/api/agents/{id}/invoke`,
  `/api/registry/a2a-demo`): it is the same capability the Chat console gives
  every member, so gating it while Chat stays open would protect nothing.

* The account-free `/share/*` routes (T13) sit outside `/api` — no console session,
  no `X-Workspace` — and are classified here as `PUBLIC` + hub-global so the drift
  test covers them; `enforce_route_policy` itself only runs for `/api`.

There is deliberately **no** setting that disables this table. A flag that turns
authorization off is the vulnerability; fixing a misclassification means editing
the entry.
"""

from typing import Any

from fastapi import Request

from app.core.errors import AppError
from app.routers.auth import require_admin, require_identity, require_permission
from app.routers.workspaces import TIER_PROD, resolve_workspace
from app.services.audit import record_audit_event

ADMIN = "admin"
MEMBER = "member"
PUBLIC = "public"

# Member-grantable permissions (auth.AGENT_PERMISSIONS keys), default granted,
# revocable per user in the Users console.
PERM_AGENT_DEPLOY = "perm:agents.deploy"
PERM_AGENT_IMPORT = "perm:agents.import"
PERM_AGENT_DELETE = "perm:agents.delete"
PERM_AGENT_CONVERT = "perm:agents.convert"
PERM_EVAL_RUN = "perm:eval.run"
# T19/T21 — the release surface. `request` is default-granted to members and operators
# (asking for a release is not privileged); `approve` is granted to operators and admins
# only, so nobody can wave their own change into production.
PERM_PROMOTION_REQUEST = "perm:promotion.request"
PERM_PROMOTION_APPROVE = "perm:promotion.approve"
PERM_IDENTITY_MANAGE = "perm:identity.manage"
PERM_IDENTITY_GRANT = "perm:identity.grant"
PERM_MEMORY_MANAGE = "perm:memory.manage"
# Agent-DLC: who may edit the ruler, sign it, admit samples, calibrate a judge, approve
# a waiver and sign a release (docs/agent-dlc-design.md §8).
PERM_CRITERIA_MANAGE = "perm:criteria.manage"
PERM_CRITERIA_SIGN = "perm:criteria.sign"
PERM_GOLDEN_ADMIT = "perm:golden.admit"
PERM_JUDGE_CALIBRATE = "perm:judge.calibrate"
PERM_WAIVER_APPROVE = "perm:waiver.approve"
PERM_RELEASE_SIGN = "perm:release.sign"
_PERM_PREFIX = "perm:"

API_PREFIX = "/api"

# (HTTP method, route.path_format) -> required role
ROUTE_POLICY: dict[tuple[str, str], str] = {
    # ---- health, docs, and the login surface (reachable without a session) ----
    ("GET", "/api/health"): PUBLIC,
    ("GET", "/api/docs"): PUBLIC,
    ("GET", "/api/openapi.json"): PUBLIC,
    ("GET", "/api/auth/status"): PUBLIC,
    ("POST", "/api/auth/login"): PUBLIC,
    ("POST", "/api/auth/register"): PUBLIC,
    ("POST", "/api/auth/logout"): MEMBER,
    # ---- agents: lifecycle changes are member-grantable permissions (default
    # granted, revocable per user); reads and invoke are plain member ----
    ("GET", "/api/agents"): MEMBER,
    ("POST", "/api/agents"): PERM_AGENT_DEPLOY,
    ("GET", "/api/agents/discovery"): MEMBER,
    ("POST", "/api/agents/discovery/import"): PERM_AGENT_IMPORT,
    # BYOC artifact staging is one half of a deploy, so it carries deploy perms
    ("POST", "/api/agents/uploads"): PERM_AGENT_DEPLOY,
    ("GET", "/api/agents/uploads/{upload_id}"): MEMBER,
    ("GET", "/api/agents/{agent_id}"): MEMBER,
    ("GET", "/api/agents/{agent_id}/suggested-questions"): MEMBER,  # cached Converse call
    ("GET", "/api/agents/{agent_id}/identity"): MEMBER,
    ("GET", "/api/agents/{agent_id}/versions"): MEMBER,  # read-only AWS view
    ("GET", "/api/agents/{agent_id}/conversions"): MEMBER,  # ledger read: runtime twins
    ("DELETE", "/api/agents/{agent_id}"): PERM_AGENT_DELETE,
    ("POST", "/api/agents/{agent_id}/convert"): PERM_AGENT_CONVERT,
    ("POST", "/api/agents/{agent_id}/redeploy"): PERM_AGENT_DEPLOY,
    ("GET", "/api/agents/{agent_id}/snapshots"): MEMBER,  # ledger read (T18)
    ("GET", "/api/agents/{agent_id}/snapshots/diff"): MEMBER,
    ("GET", "/api/agents/{agent_id}/snapshots/{seq}"): MEMBER,
    ("POST", "/api/agents/{agent_id}/snapshots/{seq}/rollback"): PERM_AGENT_DEPLOY,
    ("POST", "/api/agents/{agent_id}/inbound-auth"): PERM_AGENT_DEPLOY,
    ("POST", "/api/agents/{agent_id}/invoke"): MEMBER,  # parity with Chat
    ("GET", "/api/jobs/{job_id}"): MEMBER,
    # ---- system-managed presets: status is a ledger read; install/repair and
    # uninstall create or delete billable AWS resources on an operator's explicit
    # request and are the ONLY mutation paths for a preset (the agent routes above
    # refuse rows that carry Agent.system_key, whatever perm:* the caller holds) ----
    ("GET", "/api/system-agents"): MEMBER,
    ("POST", "/api/system-agents/{preset_key}/install"): ADMIN,
    # registers / verifies the preset's published Skill release in the Registry
    # (one Registry write at most, no S3 write) — administrator, like install
    ("POST", "/api/system-agents/{preset_key}/skill-registration"): ADMIN,
    ("DELETE", "/api/system-agents/{preset_key}"): ADMIN,
    # ---- architect assistant (SE-039): discussion with the preset is member
    # (parity with Chat); approving a proposal deploys a NEW agent and rides the
    # same permission as POST /api/agents. Conversations are owner-bound on top. ----
    ("GET", "/api/assistant/architect"): MEMBER,
    ("GET", "/api/assistant/architect/conversations"): MEMBER,
    ("POST", "/api/assistant/architect/conversations"): MEMBER,
    ("GET", "/api/assistant/architect/conversations/{conversation_id}"): MEMBER,
    # sharing: the owner (amended 2026-10-08) or an administrator — the handler refuses
    # a non-owner member; nobody can reach a private conversation it does not own
    ("PUT", "/api/assistant/architect/conversations/{conversation_id}/sharing"): MEMBER,
    ("POST", "/api/assistant/architect/conversations/{conversation_id}/catalog"): MEMBER,
    ("PUT", "/api/assistant/architect/conversations/{conversation_id}/preparation"): MEMBER,
    ("POST", "/api/assistant/architect/conversations/{conversation_id}/preparation/skills"):
        PERM_AGENT_DEPLOY,
    ("POST", "/api/assistant/architect/conversations/{conversation_id}/turns"): MEMBER,
    ("PUT", "/api/assistant/architect/conversations/{conversation_id}/proposal"): MEMBER,
    ("POST", "/api/assistant/architect/conversations/{conversation_id}/proposal/reject"): MEMBER,
    ("POST", "/api/assistant/architect/conversations/{conversation_id}/proposal/approve"):
        PERM_AGENT_DEPLOY,
    # ---- SE-047 evaluation-assets plan: preparing/editing the private plan is
    # discussion (member, owner-bound); materializing creates AWS evaluators + a
    # Lambda + IAM → the deploy permission (amended 2026-10-08: was admin), owner-
    # bound, exact plan revision/hash; status is a ledger read; retry / review /
    # cleanup of an operation ride the same permission ----
    ("GET", "/api/assistant/architect/conversations/{conversation_id}/evaluation-plan"): MEMBER,
    ("POST", "/api/assistant/architect/conversations/{conversation_id}/evaluation-plan/prepare"):
        MEMBER,
    ("PUT", "/api/assistant/architect/conversations/{conversation_id}/evaluation-plan"): MEMBER,
    ("POST",
     "/api/assistant/architect/conversations/{conversation_id}/evaluation-plan/materialize"):
        PERM_AGENT_DEPLOY,
    ("GET", "/api/assistant/architect/conversations/{conversation_id}/evaluation-plan/operations/"
     "{operation_id}"): MEMBER,
    ("POST", "/api/assistant/architect/conversations/{conversation_id}/evaluation-plan/operations/"
     "{operation_id}/retry"): PERM_AGENT_DEPLOY,
    # SE-049: reviewed recovery of the Lambda first-initialization RevisionId conflict —
    # deploy permission + owner, reads CloudTrail / Lambda, writes only the ledger
    # review + requeue
    ("POST", "/api/assistant/architect/conversations/{conversation_id}/evaluation-plan/operations/"
     "{operation_id}/lambda-revision-review"): PERM_AGENT_DEPLOY,
    ("DELETE", "/api/assistant/architect/conversations/{conversation_id}/evaluation-plan/"
     "operations/{operation_id}/assets"): PERM_AGENT_DEPLOY,
    # ---- clearing a conversation (History panel): the footprint is a ledger read;
    # the purge is owner-bound and MEMBER at the policy layer — the handler adds
    # agents.deploy for cloud assets and agents.delete for an Agent (cleanup /
    # delete parity), so a plain transcript stays the member's to delete ----
    ("GET", "/api/assistant/architect/conversations/{conversation_id}/footprint"): MEMBER,
    ("DELETE", "/api/assistant/architect/conversations/{conversation_id}"): MEMBER,
    # ---- credential minting ----
    ("GET", "/api/apikeys"): MEMBER,
    ("POST", "/api/apikeys"): MEMBER,
    ("POST", "/api/apikeys/{key_id}/disable"): MEMBER,
    ("POST", "/api/apikeys/{key_id}/enable"): MEMBER,
    ("PATCH", "/api/apikeys/{key_id}"): MEMBER,  # scope / expiry / rate limit (T16)
    ("GET", "/api/apikeys/{key_id}/usage"): MEMBER,
    # ---- chat: the member-facing invoke surface ----
    ("POST", "/api/chat/{agent_id}"): MEMBER,
    ("GET", "/api/chat/{agent_id}/history"): MEMBER,
    ("GET", "/api/chat/{agent_id}/memory"): MEMBER,
    ("GET", "/api/chat/{agent_id}/sessions"): MEMBER,
    ("POST", "/api/chat/{agent_id}/sessions/{session_id}/stop"): MEMBER,
    # ---- studio local-debug scaffolding: prod refuses these in the handler
    # (local_exec guard) regardless of role ----
    ("GET", "/api/conversations"): MEMBER,
    ("POST", "/api/conversations"): MEMBER,
    ("GET", "/api/conversations/{session_id}"): MEMBER,
    ("DELETE", "/api/conversations/{session_id}"): MEMBER,
    ("PUT", "/api/conversations/{session_id}/code"): MEMBER,
    ("GET", "/api/conversations/{session_id}/messages"): MEMBER,
    ("POST", "/api/conversations/{session_id}/messages"): MEMBER,
    ("POST", "/api/conversations/{session_id}/messages/stream"): MEMBER,
    # ---- local code execution (also refused outright in prod; see local_exec) ----
    ("POST", "/api/execute"): MEMBER,
    ("POST", "/api/execute/stream"): MEMBER,
    ("POST", "/api/fix-code/stream"): MEMBER,
    ("GET", "/api/generate-code/status"): MEMBER,
    # ---- registry skill sources become deployable code; the two staging
    # helpers ride the deploy permission because the create wizard needs them ----
    ("POST", "/api/agent-skills/import"): PERM_AGENT_DEPLOY,
    ("GET", "/api/registry/records"): MEMBER,
    ("POST", "/api/registry/records"): MEMBER,
    ("GET", "/api/registry/records/search"): MEMBER,
    # data-plane read (ListDiscoverableRegistryRecords) of the workspace registry
    ("GET", "/api/registry/records/discoverable"): MEMBER,
    ("GET", "/api/registry/records/{record_id}"): MEMBER,
    # a data-plane read (GetAgentCard) of the agent's own runtime; no ARN from the client
    ("GET", "/api/registry/records/{record_id}/live-agent-card"): MEMBER,
    ("PUT", "/api/registry/records/{record_id}"): MEMBER,
    ("DELETE", "/api/registry/records/{record_id}"): MEMBER,
    ("POST", "/api/registry/records/{record_id}/action"): MEMBER,
    ("POST", "/api/registry/records/{record_id}/reimport"): MEMBER,
    ("GET", "/api/registry/skills/capabilities"): MEMBER,
    # installs software on the server host
    ("POST", "/api/registry/skills/capabilities/git-install"): MEMBER,
    ("POST", "/api/registry/skills/import"): MEMBER,
    # fetches remote content (SSRF-guarded); staging-only, needed by deploy
    ("POST", "/api/registry/skills/inspect"): PERM_AGENT_DEPLOY,
    ("POST", "/api/registry/sync-defaults"): MEMBER,
    ("GET", "/api/registry/attachables"): MEMBER,
    ("POST", "/api/registry/a2a-demo"): MEMBER,  # an invoke; parity with Chat
    # ---- identity: Connections (credential providers) and gateway targets
    # bound to one. Reads are plain member; creating/deleting a vault entry or a
    # target is the member-grantable identity.manage permission ----
    ("GET", "/api/identity/connections"): MEMBER,
    ("GET", "/api/identity/connections/templates"): MEMBER,
    ("GET", "/api/identity/connections/oidc-sources"): MEMBER,
    ("GET", "/api/identity/connections/{kind}/{name}"): MEMBER,
    ("POST", "/api/identity/connections/oauth2"): PERM_IDENTITY_MANAGE,
    ("POST", "/api/identity/connections/api-key"): PERM_IDENTITY_MANAGE,
    ("DELETE", "/api/identity/connections/{kind}/{name}"): PERM_IDENTITY_MANAGE,
    ("GET", "/api/identity/gateway-targets"): MEMBER,
    ("POST", "/api/identity/gateway-targets"): PERM_IDENTITY_MANAGE,
    ("DELETE", "/api/identity/gateway-targets/{target_id}"): PERM_IDENTITY_MANAGE,
    # as_user (3LO): binding a consent into the vault or revoking one is the
    # member-grantable identity.grant permission; reading one's OWN grants
    # (the router scopes every query to the caller) is plain member
    ("POST", "/api/identity/oauth/complete"): PERM_IDENTITY_GRANT,
    ("GET", "/api/identity/grants"): MEMBER,
    ("GET", "/api/identity/grants/{connection}/status"): MEMBER,
    ("DELETE", "/api/identity/grants/{connection}"): PERM_IDENTITY_GRANT,
    # the gateway's Consent Portal is workspace infrastructure
    ("GET", "/api/identity/consent-portal"): MEMBER,
    # gateway-wide singleton whose create passes an operator-named IAM role to
    # AgentCore: an administrator decision, not member-grantable
    ("POST", "/api/identity/consent-portal"): ADMIN,
    ("DELETE", "/api/identity/consent-portal"): ADMIN,
    # inbound JWT auth (P3): reading the workspace default is member (the
    # wizard shows the effective value); changing it flips how every inheriting
    # agent authenticates its callers on next deploy
    ("GET", "/api/identity/inbound-auth/default"): MEMBER,
    ("PUT", "/api/identity/inbound-auth/default"): PERM_IDENTITY_MANAGE,
    # ---- tools + demos: /tools/call can mutate external systems through a
    # gateway target, and the demos open billable cloud sessions ----
    ("GET", "/api/tools"): MEMBER,
    ("POST", "/api/tools/call"): MEMBER,
    ("POST", "/api/demos/code-interpreter"): MEMBER,
    ("GET", "/api/demos/browser/options"): MEMBER,
    ("POST", "/api/demos/browser"): MEMBER,  # takes a caller-supplied URL
    ("DELETE", "/api/demos/browser/{session_id}"): MEMBER,
    # ---- knowledge bases: reads and the retrieval playground stay open ----
    ("GET", "/api/knowledge-bases"): MEMBER,
    ("POST", "/api/knowledge-bases"): MEMBER,
    ("POST", "/api/knowledge-bases/ensure-gateway"): MEMBER,
    ("GET", "/api/knowledge-bases/{kb_id}"): MEMBER,
    ("PATCH", "/api/knowledge-bases/{kb_id}"): MEMBER,
    ("DELETE", "/api/knowledge-bases/{kb_id}"): MEMBER,
    ("POST", "/api/knowledge-bases/{kb_id}/files"): MEMBER,
    ("POST", "/api/knowledge-bases/{kb_id}/data-sources"): MEMBER,
    ("DELETE", "/api/knowledge-bases/{kb_id}/data-sources/{ds_id}"): MEMBER,
    ("GET", "/api/knowledge-bases/{kb_id}/data-sources/{ds_id}/documents"): MEMBER,
    ("GET", "/api/knowledge-bases/{kb_id}/data-sources/{ds_id}/ingestion-jobs"): MEMBER,
    ("POST", "/api/knowledge-bases/{kb_id}/data-sources/{ds_id}/sync"): MEMBER,
    ("POST", "/api/knowledge-bases/{kb_id}/query"): MEMBER,  # retrieval playground
    # ---- governance ----
    # the workspace's PII guardrail preset (T12): reading it is a member's business
    # (the wizard shows whether it exists); creating the real Bedrock resource is not
    ("GET", "/api/governance/guardrail"): MEMBER,
    ("POST", "/api/governance/guardrail"): ADMIN,
    ("GET", "/api/governance/gateways"): MEMBER,
    ("GET", "/api/governance/gateways/{gateway_id}"): MEMBER,
    ("POST", "/api/governance/gateways/{gateway_id}/manage"): MEMBER,
    ("DELETE", "/api/governance/gateways/{gateway_id}/manage"): MEMBER,
    ("GET", "/api/governance/gateways/{gateway_id}/registry-preview"): MEMBER,
    ("POST", "/api/governance/gateways/{gateway_id}/registry-import"): MEMBER,
    ("POST", "/api/governance/gateways/{gateway_id}/retire-legacy-records"): MEMBER,
    ("GET", "/api/governance/gateways/{gateway_id}/policies"): MEMBER,
    ("POST", "/api/governance/gateways/{gateway_id}/policies"): MEMBER,
    ("PUT", "/api/governance/gateways/{gateway_id}/policies/{policy_id}"): MEMBER,
    ("DELETE", "/api/governance/gateways/{gateway_id}/policies/{policy_id}"): MEMBER,
    ("POST", "/api/governance/gateways/{gateway_id}/policies/{policy_id}/promote"): MEMBER,
    ("POST", "/api/governance/gateways/{gateway_id}/policies/{policy_id}/rollback"): MEMBER,
    ("POST", "/api/governance/gateways/{gateway_id}/engine"): MEMBER,
    ("GET", "/api/governance/gateways/{gateway_id}/rate-limits"): MEMBER,
    ("POST", "/api/governance/gateways/{gateway_id}/rate-limits"): MEMBER,
    ("PUT", "/api/governance/gateways/{gateway_id}/rate-limits/{rate_limit_id}"): MEMBER,
    ("DELETE", "/api/governance/gateways/{gateway_id}/rate-limits/{rate_limit_id}"): MEMBER,
    ("POST", "/api/governance/gateways/{gateway_id}/targets/{target_id}/synchronize"): MEMBER,
    ("POST", "/api/governance/gateways/{gateway_id}/mode"): MEMBER,
    ("POST", "/api/governance/gateways/{gateway_id}/generations"): MEMBER,
    ("GET", "/api/governance/gateways/{gateway_id}/generations/{generation_id}"): MEMBER,
    ("GET", "/api/governance/gateways/{gateway_id}/audit"): MEMBER,
    ("GET", "/api/governance/gateways/{gateway_id}/decisions"): MEMBER,
    ("GET", "/api/governance/operations/{operation_id}"): MEMBER,
    ("GET", "/api/governance/policies"): MEMBER,
    ("GET", "/api/governance/decisions"): MEMBER,
    # Not a dry run: it performs a real tools/call as the chosen principal and
    # journals a PolicyDecision row (routers/governance.py:406).
    ("POST", "/api/governance/policy-test"): MEMBER,
    ("POST", "/api/governance/policy-generation"): MEMBER,
    ("GET", "/api/governance/policy-generation/{generation_id}"): MEMBER,
    ("GET", "/api/traces/{session_id}"): MEMBER,
    # ---- evaluation: runs invoke agents and create AWS eval jobs ----
    ("GET", "/api/eval/datasets"): MEMBER,
    ("POST", "/api/eval/datasets"): MEMBER,
    ("POST", "/api/eval/datasets/upload"): MEMBER,
    ("GET", "/api/eval/datasets/cloud"): MEMBER,
    ("GET", "/api/eval/datasets/cloud/{cloud_id}"): MEMBER,
    ("DELETE", "/api/eval/datasets/cloud/{cloud_id}"): MEMBER,
    ("POST", "/api/eval/datasets/cloud/{cloud_id}/publish-version"): MEMBER,
    ("DELETE", "/api/eval/datasets/cloud/{cloud_id}/versions/{version}"): MEMBER,
    ("PUT", "/api/eval/datasets/{dataset_id}"): MEMBER,
    ("DELETE", "/api/eval/datasets/{dataset_id}"): MEMBER,
    ("POST", "/api/eval/datasets/{dataset_id}/sync-to-aws"): MEMBER,
    ("POST", "/api/eval/datasets/{dataset_id}/publish-version"): MEMBER,
    # console V2 数据处理: observed sessions → local datasets (reads + ledger writes)
    ("POST", "/api/eval/datasets/from-sessions"): MEMBER,
    ("GET", "/api/eval/pipelines"): MEMBER,
    ("POST", "/api/eval/pipelines"): MEMBER,
    ("GET", "/api/eval/pipelines/{pipeline_id}"): MEMBER,
    ("PUT", "/api/eval/pipelines/{pipeline_id}"): MEMBER,
    ("DELETE", "/api/eval/pipelines/{pipeline_id}"): MEMBER,
    ("POST", "/api/eval/pipelines/{pipeline_id}/run"): MEMBER,
    ("POST", "/api/eval/pipelines/preview-logs"): MEMBER,
    ("GET", "/api/eval/agents/{agent_id}/log-streams"): MEMBER,
    ("GET", "/api/eval/log-sessions"): MEMBER,
    ("GET", "/api/eval/log-groups"): MEMBER,
    ("GET", "/api/eval/log-services"): MEMBER,
    ("GET", "/api/eval/evaluators"): MEMBER,
    ("POST", "/api/eval/evaluators"): MEMBER,
    ("GET", "/api/eval/evaluators/{evaluator_id}"): MEMBER,
    ("PUT", "/api/eval/evaluators/{evaluator_id}"): MEMBER,
    ("DELETE", "/api/eval/evaluators/{evaluator_id}"): MEMBER,
    ("GET", "/api/eval/queue"): MEMBER,
    ("GET", "/api/eval/runs"): MEMBER,
    ("POST", "/api/eval/runs"): PERM_EVAL_RUN,
    ("GET", "/api/eval/runs/{run_id}"): MEMBER,
    ("GET", "/api/eval/runs/{run_id}/results"): MEMBER,
    ("POST", "/api/eval/runs/{run_id}/stop"): PERM_EVAL_RUN,
    ("POST", "/api/eval/runs/{run_id}/recheck"): PERM_EVAL_RUN,
    ("GET", "/api/eval/runs/{run_id}/recommendation-inputs"): MEMBER,
    ("GET", "/api/eval/runs/{run_id}/recommendations"): MEMBER,
    # starts billable AWS recommendation jobs — same revocable grant as a run
    ("POST", "/api/eval/runs/{run_id}/recommendations"): PERM_EVAL_RUN,
    # re-publishes the Harness (a new version) — the same grant as a redeploy
    # saving a revision of a recommendation is ledger-only, on the generating permission
    ("PUT", "/api/eval/runs/{run_id}/recommendations/{rec_id}/edit"): PERM_EVAL_RUN,
    ("POST", "/api/eval/runs/{run_id}/recommendations/{rec_id}/accept"): PERM_AGENT_DEPLOY,
    ("DELETE", "/api/eval/runs/{run_id}"): PERM_EVAL_RUN,
    # online evaluation configs: create/resume start billed judge calls on live
    # traffic, the same cost class as starting a run; list/detail/results read AWS
    ("GET", "/api/eval/online"): MEMBER,
    ("POST", "/api/eval/online"): PERM_EVAL_RUN,
    ("GET", "/api/eval/online/{config_id}"): MEMBER,
    ("PATCH", "/api/eval/online/{config_id}"): MEMBER,
    ("POST", "/api/eval/online/{config_id}/pause"): MEMBER,
    ("POST", "/api/eval/online/{config_id}/resume"): PERM_EVAL_RUN,
    ("DELETE", "/api/eval/online/{config_id}"): MEMBER,
    ("GET", "/api/eval/online/{config_id}/results"): MEMBER,
    ("GET", "/api/eval/online/{config_id}/reports"): MEMBER,
    ("POST", "/api/eval/online/{config_id}/reports"): PERM_EVAL_RUN,
    ("GET", "/api/eval/online/{config_id}/reports/{batch_id}"): MEMBER,
    # ---- skill lab: local task assets/task sets + Runtime-backed jobs ----
    ("GET", "/api/skill-lab/status"): MEMBER,
    ("POST", "/api/skill-lab/task-assets"): MEMBER,
    ("GET", "/api/skill-lab/tasksets"): MEMBER,
    ("POST", "/api/skill-lab/tasksets"): MEMBER,
    ("GET", "/api/skill-lab/tasksets/{taskset_id}"): MEMBER,
    ("PUT", "/api/skill-lab/tasksets/{taskset_id}"): MEMBER,
    ("DELETE", "/api/skill-lab/tasksets/{taskset_id}"): MEMBER,
    ("GET", "/api/skill-lab/jobs"): MEMBER,
    ("POST", "/api/skill-lab/jobs"): MEMBER,
    ("GET", "/api/skill-lab/jobs/{job_id}"): MEMBER,
    ("POST", "/api/skill-lab/jobs/{job_id}/cancel"): MEMBER,
    ("DELETE", "/api/skill-lab/jobs/{job_id}"): MEMBER,
    ("POST", "/api/skill-lab/jobs/{job_id}/resume"): MEMBER,
    ("POST", "/api/skill-lab/jobs/{job_id}/publish"): MEMBER,
    ("POST", "/api/skill-lab/jobs/{job_id}/import-taskset"): MEMBER,
    ("POST", "/api/skill-lab/jobs/{job_id}/apply-expansion"): MEMBER,
    ("GET", "/api/skill-lab/jobs/{job_id}/train-summary"): MEMBER,
    ("GET", "/api/skill-lab/jobs/{job_id}/diff"): MEMBER,
    ("GET", "/api/skill-lab/jobs/{job_id}/log"): MEMBER,
    ("GET", "/api/skill-lab/jobs/{job_id}/results"): MEMBER,
    ("GET", "/api/skill-lab/jobs/{job_id}/artifacts"): MEMBER,
    ("GET", "/api/skill-lab/jobs/{job_id}/artifacts/raw"): MEMBER,
    ("GET", "/api/experiments"): MEMBER,
    ("GET", "/api/experiments/readiness"): MEMBER,
    ("GET", "/api/experiments/providers"): MEMBER,
    ("POST", "/api/experiments"): MEMBER,
    ("GET", "/api/experiments/{exp_id}"): MEMBER,
    ("POST", "/api/experiments/{exp_id}/action"): MEMBER,
    # canaries provision real AgentCore runtimes
    ("GET", "/api/runtime-canaries"): MEMBER,
    ("POST", "/api/runtime-canaries"): MEMBER,
    ("GET", "/api/runtime-canaries/{canary_id}"): MEMBER,
    ("POST", "/api/runtime-canaries/{canary_id}/action"): MEMBER,
    # ---- read-only consoles ----
    # the wizard's scenario-template gallery (T10): static data, no workspace
    ("GET", "/api/agent-templates"): MEMBER,
    # ---- release bundles, promotions, inbox (T20-T22) ----
    # Bundling freezes a publish and mints no AWS resource, so it rides the same
    # default-granted permission as asking for the release itself.
    ("POST", "/api/agents/{agent_id}/release-bundles"): PERM_PROMOTION_REQUEST,
    ("GET", "/api/agents/{agent_id}/release-bundles"): MEMBER,
    ("GET", "/api/release-bundles/{bundle_id}"): MEMBER,
    ("POST", "/api/promotions"): PERM_PROMOTION_REQUEST,
    ("GET", "/api/promotions"): MEMBER,
    ("GET", "/api/promotions/{promotion_id}"): MEMBER,
    # the second pair of eyes: operators and admins, never the requester themselves
    ("POST", "/api/promotions/{promotion_id}/review"): PERM_PROMOTION_APPROVE,
    # T26/T27: the read-only plan preview and live progress are plain reads; running a
    # release and undoing one are the operator's (same key as approving); their prod
    # decision is in PROD_UNPROTECTED_AGENT_ROUTES, with the reason.
    ("GET", "/api/promotions/{promotion_id}/plan"): MEMBER,
    ("GET", "/api/promotions/{promotion_id}/execution"): MEMBER,
    ("POST", "/api/promotions/{promotion_id}/execute"): PERM_PROMOTION_APPROVE,
    ("POST", "/api/promotions/{promotion_id}/rollback"): PERM_PROMOTION_APPROVE,
    # the release policy of a target workspace: readable by anyone who can see the plan,
    # writable only by an administrator (it decides what may ship, and when)
    ("GET", "/api/release-policies/{workspace_id}"): MEMBER,
    ("PUT", "/api/release-policies/{workspace_id}"): ADMIN,
    # the inbox reads pending accounts and workspaces as well as this workspace's work
    ("GET", "/api/inbox"): ADMIN,
    # ---- spend attribution and alerts (T28/T29) ----
    # Spend is admin business: the per-person breakdown names who spent what, which is
    # the same reason the TTFA view is admin-only.
    ("GET", "/api/costs"): ADMIN,
    ("GET", "/api/costs/month-to-date"): ADMIN,
    # Rules are the workspace's operating posture — readable by any member (an operator
    # needs to see what is watched), writable by an administrator only.
    ("GET", "/api/alerts"): MEMBER,
    ("POST", "/api/alerts"): ADMIN,
    ("PATCH", "/api/alerts/{rule_id}"): ADMIN,
    ("DELETE", "/api/alerts/{rule_id}"): ADMIN,
    # Evaluation costs a billed read and can notify, so it is not a member action.
    ("POST", "/api/alerts/evaluate"): ADMIN,
    # ---- fleet, governance health, template marketplace (T37-T39) ----
    # The fleet spans environments a member may not be granted, so it is admin-only and
    # hub-global (it deliberately does not operate inside one workspace).
    ("GET", "/api/fleet"): ADMIN,
    ("GET", "/api/governance/health"): MEMBER,
    # Reads are cross-workspace by design (that is what publishing is for); writing is
    # scoped to the publishing workspace, which the handler enforces.
    ("GET", "/api/marketplace/templates"): MEMBER,
    ("POST", "/api/marketplace/templates"): MEMBER,
    ("POST", "/api/marketplace/templates/{template_id}/use"): MEMBER,
    ("DELETE", "/api/marketplace/templates/{template_id}"): MEMBER,
    # ---- logical resource mapping (T23) ----
    # Reads are plain member (a builder previews what a bundle resolves to). Writes decide
    # which of the SELECTED (target) workspace's resources a promoted agent is wired to,
    # so they are the approver's permission, not the builder's. Not in PROD_PROTECTED:
    # mapping prod resources is exactly what an operator does there, and it mints no AWS
    # resource; the write is journaled in audit_events by the handler.
    ("GET", "/api/resource-mappings"): MEMBER,
    ("PUT", "/api/resource-mappings/{kind}/{name}"): PERM_PROMOTION_APPROVE,
    ("DELETE", "/api/resource-mappings/{kind}/{name}"): PERM_PROMOTION_APPROVE,
    ("GET", "/api/release-bundles/{bundle_id}/resolution"): MEMBER,
    # ---- GitOps export and environment comparison (T31/T32) ----
    # All read-only. `compare` reads sibling workspaces' ledger rows, filtered to the
    # caller's grants in the handler; `drift` reads AWS for the current workspace only.
    ("GET", "/api/release-bundles/{bundle_id}/export"): MEMBER,
    ("GET", "/api/environments/compare"): MEMBER,
    ("GET", "/api/environments/drift"): MEMBER,
    # ---- share links (T13/T14) and thumbs feedback (T15) ----
    # Creating a link is not an agent mutation, so it is deliberately NOT in
    # PROD_PROTECTED: a prod workspace may still hand out a chat link.
    ("GET", "/api/agents/{agent_id}/share-links"): MEMBER,
    ("POST", "/api/agents/{agent_id}/share-links"): MEMBER,
    ("POST", "/api/share-links/{link_id}/revoke"): MEMBER,
    ("POST", "/api/chat/{agent_id}/feedback"): MEMBER,
    ("GET", "/api/feedback"): MEMBER,
    # The account-free surface (T13): outside `/api`, so no console session, and
    # PUBLIC + hub-global so no workspace header is read either — the link's own
    # row names the workspace. Each handler resolves the token first and answers
    # 404 for every unusable state.
    ("GET", "/share/{token}"): PUBLIC,
    ("POST", "/share/{token}/chat"): PUBLIC,
    ("POST", "/share/{token}/feedback"): PUBLIC,
    # SME review (T34): the same account-free posture under `/share/review`
    ("GET", "/share/review/{token}"): PUBLIC,
    ("POST", "/share/review/{token}/rate"): PUBLIC,
    # Agent-DLC annotation links (design §7.4): the same posture. The link row names
    # the workspace, the task and the annotator; the judge's verdict and the agreement
    # numbers are never served here.
    ("GET", "/share/annotate/{token}"): PUBLIC,
    ("POST", "/share/annotate/{token}/label"): PUBLIC,
    ("GET", "/api/agents/{agent_id}/review-links"): MEMBER,
    ("POST", "/api/agents/{agent_id}/review-links"): MEMBER,
    # ---- business self-service: intent view, curated answers, issue box (T33/T35/T36) ----
    # Deliberately NOT in PROD_PROTECTED: the point of a curated answer is that a business
    # owner fixes production without a redeploy. Rule writes are journaled in audit_events.
    # ── Agent-DLC (docs/agent-dlc-design.md) ────────────────────────────────
    # Reads are MEMBER: the standard and its evidence are the team's shared picture.
    # Writes split by whose decision they are: editing the ruler is engineering
    # (`criteria.manage`), signing it / admitting samples / declaring a judge
    # calibrated / approving a waiver / signing a release are named people's.
    ("GET", "/api/criteria-sets"): MEMBER,
    ("POST", "/api/criteria-sets"): PERM_CRITERIA_MANAGE,
    ("GET", "/api/criteria-sets/{lineage_id}"): MEMBER,
    ("PUT", "/api/criteria-sets/{lineage_id}"): PERM_CRITERIA_MANAGE,
    ("DELETE", "/api/criteria-sets/{lineage_id}"): PERM_CRITERIA_MANAGE,
    ("POST", "/api/criteria-sets/{lineage_id}/versions"): PERM_CRITERIA_MANAGE,
    ("POST", "/api/criteria-sets/{lineage_id}/publish"): PERM_CRITERIA_MANAGE,
    ("POST", "/api/criteria-sets/{lineage_id}/sign"): PERM_CRITERIA_SIGN,
    ("POST", "/api/criteria-sets/{lineage_id}/adopt-template"): PERM_CRITERIA_MANAGE,
    ("GET", "/api/criteria-sets/{lineage_id}/diff"): MEMBER,
    ("GET", "/api/golden-sets"): MEMBER,
    ("POST", "/api/golden-sets"): PERM_CRITERIA_MANAGE,
    ("GET", "/api/golden-sets/{dataset_id}"): MEMBER,
    ("POST", "/api/golden-sets/{dataset_id}/items"): PERM_CRITERIA_MANAGE,
    # seeding writes the holdout once: the same right that admits golden samples
    ("POST", "/api/golden-sets/{dataset_id}/seed"): PERM_GOLDEN_ADMIT,
    ("POST", "/api/golden-sets/{dataset_id}/move"): PERM_CRITERIA_MANAGE,
    ("POST", "/api/golden-sets/{dataset_id}/retire"): PERM_CRITERIA_MANAGE,
    ("GET", "/api/annotation-tasks"): MEMBER,
    ("POST", "/api/annotation-tasks"): PERM_CRITERIA_MANAGE,
    ("GET", "/api/annotation-tasks/{task_id}"): MEMBER,
    # labelling is the annotator's own act; the service refuses a non-annotator
    ("POST", "/api/annotation-tasks/{task_id}/labels"): MEMBER,
    ("POST", "/api/annotation-tasks/{task_id}/adjudicate"): PERM_JUDGE_CALIBRATE,
    ("GET", "/api/annotation-tasks/{task_id}/agreement"): MEMBER,
    ("POST", "/api/annotation-tasks/{task_id}/decide"): PERM_JUDGE_CALIBRATE,
    ("GET", "/api/calibration/{criterion_key}"): MEMBER,
    ("GET", "/api/annotation-tasks/{task_id}/links"): MEMBER,
    ("POST", "/api/annotation-tasks/{task_id}/links"): PERM_JUDGE_CALIBRATE,
    ("DELETE", "/api/annotation-tasks/{task_id}/links/{link_id}"): PERM_JUDGE_CALIBRATE,
    ("GET", "/api/agents/{agent_id}/release"): MEMBER,
    ("POST", "/api/agents/{agent_id}/release/migrate"): PERM_RELEASE_SIGN,
    ("POST", "/api/agents/{agent_id}/release/evaluate"): PERM_EVAL_RUN,
    ("GET", "/api/agents/{agent_id}/release/gate"): MEMBER,
    ("POST", "/api/agents/{agent_id}/release/sign"): PERM_RELEASE_SIGN,
    ("POST", "/api/agents/{agent_id}/release/block"): PERM_RELEASE_SIGN,
    ("POST", "/api/agents/{agent_id}/release/rollback"): PERM_RELEASE_SIGN,
    ("GET", "/api/release-records"): MEMBER,
    ("GET", "/api/release-records/{record_id}"): MEMBER,
    ("POST", "/api/agents/{agent_id}/waivers"): MEMBER,  # asking is not deciding
    ("POST", "/api/waivers/{waiver_id}/approve"): PERM_WAIVER_APPROVE,
    ("POST", "/api/waivers/{waiver_id}/reject"): PERM_WAIVER_APPROVE,
    ("DELETE", "/api/waivers/{waiver_id}"): PERM_WAIVER_APPROVE,
    ("GET", "/api/admission"): MEMBER,
    ("GET", "/api/admission/{candidate_id}"): MEMBER,
    ("POST", "/api/admission/{candidate_id}/admit"): PERM_GOLDEN_ADMIT,
    ("POST", "/api/admission/{candidate_id}/reject"): PERM_GOLDEN_ADMIT,
    ("POST", "/api/admission/{candidate_id}/duplicate"): PERM_GOLDEN_ADMIT,
    ("GET", "/api/agents/{agent_id}/watch"): MEMBER,
    ("PUT", "/api/agents/{agent_id}/watch"): PERM_CRITERIA_MANAGE,
    ("POST", "/api/agents/{agent_id}/watch/run"): PERM_EVAL_RUN,
    ("GET", "/api/eval/runs/{run_id}/criteria"): MEMBER,
    ("POST", "/api/eval/runs/{run_id}/criteria/snapshot"): PERM_EVAL_RUN,
    ("GET", "/api/eval/runs/compare"): MEMBER,
    ("POST", "/api/eval/runs/estimate"): MEMBER,
    ("GET", "/api/agents/{agent_id}/ladder"): MEMBER,
    ("GET", "/api/agents/{agent_id}/scorecard"): MEMBER,
    ("GET", "/api/audit"): ADMIN,
    ("GET", "/api/intents"): MEMBER,
    ("GET", "/api/agents/{agent_id}/rules"): MEMBER,
    ("POST", "/api/agents/{agent_id}/rules"): MEMBER,
    ("PATCH", "/api/agents/{agent_id}/rules/{rule_id}"): MEMBER,
    ("DELETE", "/api/agents/{agent_id}/rules/{rule_id}"): MEMBER,
    ("PUT", "/api/agents/{agent_id}/rules-order"): MEMBER,
    ("PUT", "/api/agents/{agent_id}/rules-enabled"): MEMBER,
    ("POST", "/api/agents/{agent_id}/rules/test"): MEMBER,
    ("GET", "/api/issues"): MEMBER,
    ("POST", "/api/issues"): MEMBER,
    ("POST", "/api/issues/sync"): MEMBER,
    ("GET", "/api/issues/{issue_id}"): MEMBER,
    ("POST", "/api/issues/{issue_id}/resolve"): MEMBER,
    ("POST", "/api/issues/{issue_id}/reopen"): MEMBER,
    ("POST", "/api/issues/{issue_id}/fixes"): MEMBER,
    # ---- channel publishing (T30) ----
    # Creating a channel link is like creating a share link: MEMBER, not prod-protected.
    # The webhook is PUBLIC: Slack / Feishu call it, authenticated per request by the
    # adapter (signing secret / verification token) on top of the link token in the path.
    ("POST", "/api/agents/{agent_id}/channel-links"): MEMBER,
    ("POST", "/share/channels/{platform}/{token}"): PUBLIC,
    ("GET", "/api/overview"): MEMBER,
    ("GET", "/api/overview/online-quality"): MEMBER,
    # per-user onboarding activity (TTFA) — admin-only like the Users console
    ("GET", "/api/overview/ttfa"): ADMIN,
    # ---- hub-global notices: members read published snapshots only ----
    ("GET", "/api/announcements"): MEMBER,
    ("GET", "/api/announcements/manage"): ADMIN,
    ("GET", "/api/announcements/{announcement_id}"): ADMIN,
    ("POST", "/api/announcements"): ADMIN,
    ("PUT", "/api/announcements/{announcement_id}"): ADMIN,
    ("POST", "/api/announcements/{announcement_id}/publish"): ADMIN,
    ("POST", "/api/announcements/{announcement_id}/unpublish"): ADMIN,
    ("DELETE", "/api/announcements/{announcement_id}"): ADMIN,
    # ---- hub-global videos: only published snapshots reach members ----
    ("GET", "/api/videos"): MEMBER,
    ("GET", "/api/videos/manage"): ADMIN,
    ("GET", "/api/videos/manage/{video_id}"): ADMIN,
    ("POST", "/api/videos/manage"): ADMIN,
    ("PUT", "/api/videos/manage/{video_id}"): ADMIN,
    ("POST", "/api/videos/manage/{video_id}/publish"): ADMIN,
    ("POST", "/api/videos/manage/{video_id}/unpublish"): ADMIN,
    ("DELETE", "/api/videos/manage/{video_id}"): ADMIN,
    ("GET", "/api/memory/overview"): MEMBER,
    ("GET", "/api/memory/actors"): MEMBER,
    ("GET", "/api/memory/events"): MEMBER,
    ("GET", "/api/memory/extraction-jobs"): MEMBER,
    ("GET", "/api/memory/namespaces"): MEMBER,
    ("GET", "/api/memory/records"): MEMBER,
    ("POST", "/api/memory/records/search"): MEMBER,  # a search, not a mutation
    ("GET", "/api/memory/sessions"): MEMBER,
    ("GET", "/api/memory/resources"): MEMBER,
    # creates a billable AgentCore Memory resource, registered as managed
    ("POST", "/api/memory/resources"): PERM_MEMORY_MANAGE,
    ("GET", "/api/memory/resources/{memory_id}"): MEMBER,  # managed memories only
    # description / event expiry; managed memories only
    ("PUT", "/api/memory/resources/{memory_id}"): PERM_MEMORY_MANAGE,
    # irreversible (every event and record goes); managed memories only
    ("DELETE", "/api/memory/resources/{memory_id}"): PERM_MEMORY_MANAGE,
    # brings a memory the platform did not create under workspace management
    ("POST", "/api/memory/resources/{memory_id}/adopt"): ADMIN,
    ("GET", "/api/observability/dashboard"): MEMBER,
    ("GET", "/api/observability/sessions"): MEMBER,
    ("GET", "/api/observability/sessions/{session_id}"): MEMBER,
    ("GET", "/api/observability/sessions/{session_id}/transcript"): MEMBER,
    # on-demand Evaluate: a judge inference, persists nothing
    ("POST", "/api/observability/sessions/{session_id}/evaluate"): MEMBER,
    ("GET", "/api/observability/traces"): MEMBER,
    ("GET", "/api/observability/traces/{trace_id}"): MEMBER,
    ("GET", "/api/observability/prices"): MEMBER,
    ("POST", "/api/observability/prices/refresh"): MEMBER,  # rewrites shared config
    # ---- console account management ----
    ("GET", "/api/users"): ADMIN,
    ("GET", "/api/users/stats"): ADMIN,
    ("PATCH", "/api/users/{user_id}"): ADMIN,
    ("DELETE", "/api/users/{user_id}"): ADMIN,
    # ---- workspace administration (hub-global, see WORKSPACE_EXEMPT) ----
    ("GET", "/api/workspaces"): MEMBER,  # returns only the caller's workspaces
    ("POST", "/api/workspaces"): ADMIN,
    # The hub's own account/role, for a spoke stack's parameters.
    ("GET", "/api/workspaces/hub-identity"): ADMIN,
    # Probes an AssumeRole before anything is recorded; writes nothing.
    ("POST", "/api/workspaces/preflight"): ADMIN,
    ("PATCH", "/api/workspaces/{workspace_id}"): ADMIN,
    ("DELETE", "/api/workspaces/{workspace_id}"): ADMIN,
    ("POST", "/api/workspaces/{workspace_id}/purge"): ADMIN,
    ("POST", "/api/workspaces/{workspace_id}/bootstrap"): ADMIN,
    ("POST", "/api/workspaces/{workspace_id}/observability/repair"): ADMIN,
    ("GET", "/api/workspaces/{workspace_id}/bootstrap"): ADMIN,
    ("GET", "/api/workspaces/{workspace_id}/grants"): ADMIN,
    # Bulk grant/revoke from the workspace's side (per-user replacement stays on
    # PATCH /api/users/{id}); both write only `user_workspaces`.
    ("PUT", "/api/workspaces/{workspace_id}/grants"): ADMIN,
    # Shared vs isolated member resource visibility (services/visibility.py).
    ("GET", "/api/workspaces/{workspace_id}/settings/visibility"): ADMIN,
    ("PUT", "/api/workspaces/{workspace_id}/settings/visibility"): ADMIN,
}

# Hub-global route prefixes: nothing under them operates inside a workspace.
HUB_GLOBAL_PREFIXES = (
    "/api/auth", "/api/users", "/api/workspaces", "/api/announcements", "/api/videos",
    # the wizard's scenario-template catalogue (T10) is static data — no account,
    # no region, nothing to scope
    "/api/agent-templates",
    # the fleet view (T37) is the one read that deliberately spans every environment
    "/api/fleet",
    # the account-free share surface (T13): the token's row decides the workspace
    "/share",
)


def is_hub_global(path_format: str) -> bool:
    """Whether a path sits under a hub-global prefix.

    Matched on path segments, not raw string prefixes: a bare `startswith` would
    also swallow a future `/api/userspace` and silently exempt it from the
    workspace boundary, which is the one direction that fails open.
    """
    return any(
        path_format == prefix or path_format.startswith(f"{prefix}/")
        for prefix in HUB_GLOBAL_PREFIXES
    )

# The routes that are NOT workspace-scoped. Listed rather than derived so the
# posture is auditable entry by entry; `tests/test_route_policy.py` asserts the
# set against the rule "PUBLIC (no identity to resolve a workspace for) or
# hub-global prefix" in both directions, so it can neither rot nor grow quietly.
WORKSPACE_EXEMPT: frozenset[tuple[str, str]] = frozenset(
    {
        ("GET", "/api/health"),
        ("GET", "/api/docs"),
        ("GET", "/api/openapi.json"),
        ("GET", "/api/auth/status"),
        ("POST", "/api/auth/login"),
        ("POST", "/api/auth/register"),
        ("POST", "/api/auth/logout"),
        ("GET", "/api/agent-templates"),
        ("GET", "/api/fleet"),
        ("GET", "/share/{token}"),
        ("POST", "/share/{token}/chat"),
        ("POST", "/share/{token}/feedback"),
        ("GET", "/share/review/{token}"),
        ("POST", "/share/review/{token}/rate"),
        ("GET", "/share/annotate/{token}"),
        ("POST", "/share/annotate/{token}/label"),
        ("POST", "/share/channels/{platform}/{token}"),
        ("GET", "/api/announcements"),
        ("GET", "/api/announcements/manage"),
        ("GET", "/api/announcements/{announcement_id}"),
        ("POST", "/api/announcements"),
        ("PUT", "/api/announcements/{announcement_id}"),
        ("POST", "/api/announcements/{announcement_id}/publish"),
        ("POST", "/api/announcements/{announcement_id}/unpublish"),
        ("DELETE", "/api/announcements/{announcement_id}"),
        ("GET", "/api/videos"),
        ("GET", "/api/videos/manage"),
        ("GET", "/api/videos/manage/{video_id}"),
        ("POST", "/api/videos/manage"),
        ("PUT", "/api/videos/manage/{video_id}"),
        ("POST", "/api/videos/manage/{video_id}/publish"),
        ("POST", "/api/videos/manage/{video_id}/unpublish"),
        ("DELETE", "/api/videos/manage/{video_id}"),
        ("GET", "/api/users"),
        ("GET", "/api/users/stats"),
        ("PATCH", "/api/users/{user_id}"),
        ("DELETE", "/api/users/{user_id}"),
        ("GET", "/api/workspaces"),
        ("POST", "/api/workspaces"),
        ("GET", "/api/workspaces/hub-identity"),
        # Tests credentials for a workspace that does not exist yet, so there is
        # no workspace to resolve — the candidate comes from the body.
        ("POST", "/api/workspaces/preflight"),
        ("PATCH", "/api/workspaces/{workspace_id}"),
        ("DELETE", "/api/workspaces/{workspace_id}"),
        # Deletes the target's own scoped rows; the target is the path parameter,
        # and the caller's own selection is irrelevant to it.
        ("POST", "/api/workspaces/{workspace_id}/purge"),
        # Operates ON a workspace that is not usable yet; the target is the path
        # parameter, not the caller's X-Workspace header.
        ("POST", "/api/workspaces/{workspace_id}/bootstrap"),
        ("POST", "/api/workspaces/{workspace_id}/observability/repair"),
        ("GET", "/api/workspaces/{workspace_id}/bootstrap"),
        ("GET", "/api/workspaces/{workspace_id}/grants"),
        ("PUT", "/api/workspaces/{workspace_id}/grants"),
        # The target is the path parameter, like grants.
        ("GET", "/api/workspaces/{workspace_id}/settings/visibility"),
        ("PUT", "/api/workspaces/{workspace_id}/settings/visibility"),
    }
)

# ---- third dimension (T05): prod protection ----
# The agent-mutating routes. On a workspace whose tier is `prod`, a member calling
# one of these gets 403 `workspace.prod_protected` (changes must arrive through
# promotion); an administrator is let through as break-glass and the call is
# journaled in `audit_events`. Everything else — reads, chat/invoke, eval,
# observability — stays open in prod. `tests/test_prod_protection.py` pins every
# `perm:agents.*` route to either this set or `PROD_UNPROTECTED_AGENT_ROUTES`, so
# a new lifecycle route cannot ship without a prod decision, and every non-read route
# under the agent, canary and experiment namespaces to this set or a documented exemption.
PROD_PROTECTED: frozenset[tuple[str, str]] = frozenset(
    {
        ("POST", "/api/agents"),  # create / deploy
        ("POST", "/api/agents/discovery/import"),
        ("POST", "/api/agents/uploads"),  # BYOC artifact staging
        ("DELETE", "/api/agents/{agent_id}"),
        ("POST", "/api/agents/{agent_id}/convert"),
        ("POST", "/api/agents/{agent_id}/redeploy"),  # edit = redeploy
        ("POST", "/api/agents/{agent_id}/inbound-auth"),  # re-publishes the runtime
        # accepting an AI recommendation republishes the agent (a new Harness version)
        # through the same republish path as redeploy, so prod treats it the same way
        ("POST", "/api/eval/runs/{run_id}/recommendations/{rec_id}/accept"),
        ("POST", "/api/agents/{agent_id}/snapshots/{seq}/rollback"),  # redeploy of an old spec
        # optimization flows that change what a live agent serves: a runtime canary mints
        # a candidate version from a member-supplied prompt / code and its setup already
        # rolls DEFAULT to it; an experiment's accept + promote redeploys the agent in place
        ("POST", "/api/runtime-canaries"),
        ("POST", "/api/runtime-canaries/{canary_id}/action"),
        ("POST", "/api/experiments/{exp_id}/action"),
        # Agent-DLC: the standard, its samples and what production serves
        ("POST", "/api/criteria-sets/{lineage_id}/publish"),
        ("POST", "/api/criteria-sets/{lineage_id}/sign"),
        ("POST", "/api/golden-sets/{dataset_id}/items"),
        ("POST", "/api/golden-sets/{dataset_id}/seed"),
        # taking a case OUT of the gate is as much a change to the standard as adding
        # one: a retired item leaves the gate's denominator, and a move shifts it out
        # of the replayed split
        ("POST", "/api/golden-sets/{dataset_id}/move"),
        ("POST", "/api/golden-sets/{dataset_id}/retire"),
        # a `not_aligned` verdict demotes a gating judge criterion to `observe`
        ("POST", "/api/annotation-tasks/{task_id}/decide"),
        ("POST", "/api/admission/{candidate_id}/admit"),
        ("POST", "/api/waivers/{waiver_id}/approve"),
        ("POST", "/api/agents/{agent_id}/release/migrate"),
        ("POST", "/api/agents/{agent_id}/release/sign"),
        ("POST", "/api/agents/{agent_id}/release/rollback"),
        ("PUT", "/api/agents/{agent_id}/watch"),
        ("POST", "/api/agents/{agent_id}/release/block"),
        ("POST", "/api/agent-skills/import"),  # deploy-flow skill upload to S3
        # the architect assistant's two deploy-side writes
        ("POST", "/api/assistant/architect/conversations/{conversation_id}/preparation/skills"),
        ("POST", "/api/assistant/architect/conversations/{conversation_id}/proposal/approve"),
        # system presets: already admin-only, listed so a prod install is journaled
        ("POST", "/api/system-agents/{preset_key}/install"),
        ("POST", "/api/system-agents/{preset_key}/skill-registration"),
        ("DELETE", "/api/system-agents/{preset_key}"),
    }
)
# `perm:agents.*` routes deliberately left open in prod, each with its reason.
PROD_UNPROTECTED_AGENT_ROUTES: dict[tuple[str, str], str] = {
    ("POST", "/api/registry/skills/inspect"): "preview only: parses a source, writes no S3/AWS",
    # T27: these publish agents, but into the promotion's TARGET workspace, while this guard
    # reads the tier of the REQUEST's workspace (the source). They are gated harder than the
    # guard could gate them: `promotion.approve` is held by operators/admins only, never by
    # a member, and a prod target is protected by the blocking gates (ENFORCE, window,
    # evaluation) instead. Listing them here would refuse an operator working from a prod
    # source and protect nothing about the prod target.
    ("POST", "/api/promotions/{promotion_id}/execute"): "targets another workspace; operator-only",
    ("POST", "/api/promotions/{promotion_id}/rollback"): "targets another workspace; operator-only",
    # Agent-DLC: these three change no agent. Evaluating a candidate and a scheduled
    # re-run only read the agent through its candidate endpoint (that is the point of
    # gating), and asking for a waiver is a request a risk owner still has to approve.
    ("POST", "/api/agents/{agent_id}/release/evaluate"): (
        "evaluates the candidate endpoint; it changes nothing the agent serves"
    ),
    ("POST", "/api/agents/{agent_id}/watch/run"): (
        "a read-only re-evaluation of the golden split"
    ),
    ("POST", "/api/agents/{agent_id}/waivers"): (
        "requesting a waiver is not granting one — approval is prod-protected"
    ),
    # The architect assistant's evaluation assets (upstream 2026-10-08 moved them from
    # ADMIN to `perm:agents.deploy`): they create / retry / review / clean up the eval
    # datasets, evaluators and test Lambda of a conversation's plan. Evaluation stays
    # open in prod; none of these changes what an agent serves.
    ("POST",
     "/api/assistant/architect/conversations/{conversation_id}/evaluation-plan/materialize"): (
        "creates evaluation assets for the plan; changes no agent"
    ),
    ("POST", "/api/assistant/architect/conversations/{conversation_id}/evaluation-plan/operations/"
     "{operation_id}/retry"): "retries an evaluation-asset operation; changes no agent",
    ("POST", "/api/assistant/architect/conversations/{conversation_id}/evaluation-plan/operations/"
     "{operation_id}/lambda-revision-review"): (
        "reviews an evaluation-asset Lambda conflict; ledger write + requeue only"
    ),
    ("DELETE", "/api/assistant/architect/conversations/{conversation_id}/evaluation-plan/"
     "operations/{operation_id}/assets"): "cleans up evaluation assets; changes no agent",
}


def is_prod_protected(method: str, path_format: str) -> bool:
    lookup = "GET" if method == "HEAD" else method
    return (lookup, path_format) in PROD_PROTECTED


def _guard_prod(request: Request, scope: Any, method: str, path_format: str) -> None:
    """Refuse a member's agent mutation on a `prod` workspace; journal an admin's."""
    if (getattr(scope.row, "tier", None) or "dev") != TIER_PROD:
        return
    if not is_prod_protected(method, path_format):
        return
    identity = require_identity(request)
    if not identity.is_admin:
        raise AppError(
            "workspace.prod_protected",
            f"workspace '{scope.id}' is a prod workspace — agent changes must arrive "
            "through promotion, not be made here directly",
            {"workspace_id": scope.id, "tier": TIER_PROD},
            status_code=403,
        )
    record_audit_event(
        workspace_id=scope.id,
        actor=identity.username,
        action=f"{method} {path_format}",
        target=request.url.path,
    )


# Routers whose classification was extrapolated from the signed-off principle
# rather than reviewed route by route. Since the 2026-08-11 amendment they are
# all plain MEMBER anyway; the list survives as a pointer to what to re-examine
# if per-user data partitioning ever tightens the posture again.
UNREVIEWED_PREFIXES = (
    "/api/eval/",
    "/api/experiments",
    "/api/runtime-canaries",
    "/api/conversations",
    "/api/observability/",
)


def required_role(method: str, path_format: str) -> str | None:
    """The role a route demands, or None when it is not in the table."""
    # Starlette answers HEAD from the GET handler; authorize it the same way.
    lookup = "GET" if method == "HEAD" else method
    return ROUTE_POLICY.get((lookup, path_format))


def is_workspace_scoped(method: str, path_format: str) -> bool:
    """Whether this route operates inside one workspace environment."""
    lookup = "GET" if method == "HEAD" else method
    return (lookup, path_format) not in WORKSPACE_EXEMPT


def enforce_route_policy(request: Request) -> None:
    """App-level dependency enforcing ROUTE_POLICY.

    A dependency rather than middleware because `scope["route"]` is only set once
    the router has matched, so this sees the exact `path_format` instead of
    re-implementing path matching.
    """
    path = request.url.path
    if path != API_PREFIX and not path.startswith(f"{API_PREFIX}/"):
        return  # /v1 carries its own X-Api-Key auth; static and redirects are open
    if request.method == "OPTIONS":
        return  # CORS preflight never reaches a handler
    route: Any = request.scope.get("route")
    path_format = getattr(route, "path_format", None) or path
    role = required_role(request.method, path_format)
    if role is None:
        # Default-deny: refuse rather than serve an unclassified route.
        raise AppError(
            "auth.route_unclassified",
            f"{request.method} {path_format} is missing from ROUTE_POLICY "
            "(app/core/route_policy.py) — classify it before serving it.",
            status_code=500,
        )
    if role == ADMIN:
        require_admin(request)
    elif role.startswith(_PERM_PREFIX):
        require_permission(request, role.removeprefix(_PERM_PREFIX))
    if is_workspace_scoped(request.method, path_format):
        # Resolved here rather than per handler: enforcement must not depend on a
        # router remembering to declare the dependency. Handlers read the result
        # back through `require_workspace`.
        scope = resolve_workspace(request)
        _guard_prod(request, scope, request.method, path_format)
