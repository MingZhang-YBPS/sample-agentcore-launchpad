// The one action table (storyline-ux §4, guided-console §3.2): storyline steps, an
// error's `next`, disabled-button reasons and glossary cards all name an action key;
// only this table knows the route behind it. The backend never builds a URL, so a
// page redesign changes this file alone.
import type { ApiError, ApiNextStep } from "../../lib/api";

export type ActionParams = Record<string, string | undefined>;

export interface NextAction {
  /** the console path (with search) the action opens */
  to: (p: ActionParams) => string;
  /** `apiNext.<action>` (en + zh-CN) */
  label: string;
}

/** `path?k=v…`, leaving out the parameters the caller does not have. */
function at(path: string, query: Record<string, string | undefined> = {}): string {
  const search = new URLSearchParams();
  for (const [k, v] of Object.entries(query)) if (v) search.set(k, v);
  const s = search.toString();
  return s ? `${path}?${s}` : path;
}

const standards = (view: string, p: ActionParams, extra: Record<string, string | undefined> = {}) =>
  at("/v3/standards", { agent: p.agent_id, view, ...extra });

const action = (key: string, to: NextAction["to"]): NextAction => ({ to, label: `apiNext.${key}` });

export const NEXT_ACTIONS = {
  // build
  "agent.create": action("agent.create", () => "/v3/create"),
  "agent.open": action("agent.open", (p) => at("/v3/agents", { id: p.agent_id })),
  "agent.redeploy": action("agent.redeploy", (p) => at("/v3/agents", { id: p.agent_id, tab: "deploy" })),
  "chat.open": action("chat.open", (p) => at("/v3/chat", { agent: p.agent_id })),
  "kb.open": action("kb.open", () => "/v3/knowledge"),
  "skill.register": action("skill.register", () => "/v3/registry"),
  // evaluate: criteria, golden sets, calibration, runs
  "criteria.open": action("criteria.open", (p) => standards("criteria", p, { lineage: p.lineage_id })),
  "criteria.publish": action("criteria.publish", (p) => standards("criteria", p, { lineage: p.lineage_id })),
  "criteria.open_draft": action("criteria.open_draft", (p) => standards("criteria", p, { lineage: p.lineage_id })),
  "criteria.sign": action("criteria.sign", (p) => standards("criteria", p, { lineage: p.lineage_id })),
  "golden.create": action("golden.create", (p) => standards("golden", p)),
  "golden.open": action("golden.open", (p) => standards("golden", p, { golden: p.dataset_id })),
  "admission.open": action("admission.open", (p) => standards("admission", p)),
  "calibration.open": action("calibration.open", (p) => standards("calibration", p, { task: p.task_id })),
  "eval.new": action("eval.new", (p) => at("/v3/tasks", { view: "new", agent: p.agent_id })),
  // release
  "release.migrate": action("release.migrate", (p) => standards("release", p)),
  "waiver.open": action("waiver.open", (p) => standards("release", p)),
  "gate.open": action("gate.open", (p) => at("/v3/gate", { agent: p.agent_id })),
  "apikey.create": action("apikey.create", (p) => at("/v3/agents", { id: p.agent_id, tab: "access" })),
  "cedar.open": action("cedar.open", () => "/v3/governance"),
  // observe and feedback
  "traces.open": action("traces.open", (p) => at("/v3/observability", { tab: "traces", agent: p.agent_id })),
  "alerts.open": action("alerts.open", () => "/v3/costs"),
  "issues.open": action("issues.open", (p) => at("/v3/issues", { agent: p.agent_id })),
  // the platform (administrators)
  "workspace.open": action("workspace.open", (p) => at("/v3/workspaces", { id: p.workspace_id })),
  "workspace.fix_role": action("workspace.fix_role", (p) => at("/v3/workspaces", { id: p.workspace_id })),
  "users.open": action("users.open", () => "/v3/users"),
  "projects.open": action("projects.open", () => "/v3/projects"),
  // a project's settings tab (its quotas and monthly budget, projects §11.3)
  "project.settings": action("project.settings", (p) => at("/v3/projects", { id: p.project_id, tab: "settings" })),
  "models.open": action("models.open", () => "/v3/models"),
  "network.open": action("network.open", () => "/v3/network"),
  "storage.open": action("storage.open", () => "/v3/storage"),
} satisfies Record<string, NextAction>;

export type NextActionKey = keyof typeof NEXT_ACTIONS;

export function isNextActionKey(key: string): key is NextActionKey {
  return Object.prototype.hasOwnProperty.call(NEXT_ACTIONS, key);
}

/**
 * A default next step for codes the backend sends without one and that need no
 * parameter (guided-console §3.2). A `next` from the backend always wins.
 */
export const DEFAULT_NEXT: Record<string, NextActionKey> = {
  "aws.access_denied": "workspace.fix_role",
  "workspace.assume_role_failed": "workspace.fix_role",
  "workspace.not_ready": "workspace.open",
  "agent.not_found": "agent.create",
};

export interface ResolvedAction {
  key: NextActionKey;
  to: string;
  label: string;
}

/** The actions the console can take a user to; unknown ones are dropped (separate releases). */
export function resolveNext(steps: readonly ApiNextStep[], extra: ActionParams = {}): ResolvedAction[] {
  return steps.flatMap((s) => {
    if (!isNextActionKey(s.action)) return [];
    const spec = NEXT_ACTIONS[s.action];
    return [{ key: s.action, to: spec.to({ ...extra, ...s.params }), label: spec.label }];
  });
}

/** An error's way out: the backend's `next`, else the code's `DEFAULT_NEXT`. */
export function errorNext(err: Pick<ApiError, "code" | "next">, extra: ActionParams = {}): ResolvedAction[] {
  const given = resolveNext(err.next ?? [], extra);
  if (given.length) return given;
  const fallback = DEFAULT_NEXT[err.code];
  return fallback ? resolveNext([{ action: fallback, params: {} }], extra) : [];
}
