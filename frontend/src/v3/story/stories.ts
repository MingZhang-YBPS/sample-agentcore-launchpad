// The storyline (storyline-ux §2): the workshop's main line as ordered steps, each
// ticked from data — never from a click — with the role that does it and the
// action (`NEXT_ACTIONS`) that takes the user there. Code constants on purpose
// (D-4): the story ships with the version that implements its pages.
//
// `AGENT_STORY` is mirrored by `backend/app/services/agent_story.py` (`AGENT_STEPS`)
// for `GET /api/me/next-steps`; `stories.test.ts` reads that file so the two lists
// stay the same steps, keys and roles.
import type { NextActionKey } from "./actions";

export type Chapter = "define" | "build" | "evaluate" | "release" | "observe" | "feedback";
export const AGENT_CHAPTERS: readonly Chapter[] = ["build", "evaluate", "release", "observe", "feedback"];

/** The five workshop accounts' roles (`project role template` → workshop name). */
export type WorkshopRole = "developer" | "biz-owner" | "release-ops" | "sme" | "admin";
export const WORKSHOP_ROLES: readonly WorkshopRole[] = ["developer", "biz-owner", "release-ops", "sme", "admin"];

/** `GET /api/agents/{id}/story` — what each step is ticked from. */
export interface AgentStoryFacts {
  agent_active: boolean;
  sessions: number;
  knowledge_bases: number;
  skills: number;
  criteria_lineage_id: string | null;
  criteria_draft: boolean;
  criteria_draft_version: number | null;
  criteria_published: boolean;
  criteria_version: number | null;
  criteria_signed: boolean;
  criteria_signed_by: string | null;
  golden_id: string | null;
  golden_sealed: boolean;
  golden_items: number;
  golden_from_feedback: number;
  dev_run_done: boolean;
  dev_run_id: string | null;
  endpoint_mode: string;
  candidate_version: string | null;
  candidate_record_id: string | null;
  gate_verdict: string | null;
  gate_record_id: string | null;
  release_live: boolean;
  api_keys: number;
  cedar_mode: string | null;
  /** null = unknown (the telemetry probe has no cached answer yet) */
  spans_7d: boolean | null;
  alert_rules: number;
  issues_open: number;
  issues_resolved: number;
  calibrated: boolean;
  waivers_pending: number;
}

export interface StoryAgent {
  id: string;
  name: string;
  display_name: string | null;
  status: string;
  method: string;
  /** an MCP server and a system preset do not walk the storyline */
  kind: "agent" | "mcp" | "system";
}

export interface AgentStoryState {
  agent: StoryAgent;
  facts: AgentStoryFacts;
}

export interface StoryStep<S = AgentStoryState> {
  key: string;
  chapter: Chapter;
  /** `story.step.<key>` (en + zh-CN) */
  labelKey: string;
  /** who usually does it (shown as "waiting on …") */
  role: WorkshopRole;
  /** every workshop role whose turn it can be */
  roles: readonly WorkshopRole[];
  /** the permission key the step's action needs; `admin` = administrators only */
  perm: string;
  action: NextActionKey;
  done: (s: S) => boolean;
  optional?: boolean;
}

function step<S>(
  key: string, chapter: Chapter, roles: readonly WorkshopRole[], perm: string, action: NextActionKey,
  done: (s: S) => boolean, optional = false,
): StoryStep<S> {
  return { key, chapter, labelKey: `story.step.${key}`, role: roles[0], roles, perm, action, done, optional };
}

/** The agent's main line — storyline-ux §2.2, workshop part 2. */
export const AGENT_STORY: readonly StoryStep[] = [
  step("build.create", "build", ["developer"], "agents.deploy", "agent.create", (s) => s.facts.agent_active),
  step("build.chat", "build", ["developer"], "project.use", "chat.open", (s) => s.facts.sessions > 0),
  step("build.kb", "build", ["developer"], "agents.deploy", "kb.open", (s) => s.facts.knowledge_bases > 0, true),
  step("build.skill", "build", ["developer", "biz-owner"], "agents.deploy", "skill.register", (s) => s.facts.skills > 0, true),
  step("evaluate.criteria.draft", "evaluate", ["developer"], "criteria.manage", "criteria.open",
    (s) => s.facts.criteria_draft || s.facts.criteria_published),
  step("evaluate.criteria.sign", "evaluate", ["biz-owner"], "criteria.sign", "criteria.sign",
    (s) => s.facts.criteria_published && s.facts.criteria_signed),
  step("evaluate.golden.seal", "evaluate", ["biz-owner"], "golden.admit", "golden.open", (s) => s.facts.golden_sealed),
  step("evaluate.dev_run", "evaluate", ["developer"], "eval.run", "eval.new", (s) => s.facts.dev_run_done),
  // the migrate route needs release.sign (route_policy), so the step is release-ops'
  step("evaluate.dual_endpoint", "evaluate", ["release-ops"], "release.sign", "release.migrate",
    (s) => s.facts.endpoint_mode === "live"),
  step("evaluate.candidate", "evaluate", ["developer"], "agents.deploy", "agent.redeploy",
    (s) => Boolean(s.facts.candidate_version) || s.facts.release_live),
  step("evaluate.gate", "evaluate", ["release-ops"], "eval.run", "gate.open",
    (s) => Boolean(s.facts.gate_verdict) || s.facts.release_live),
  step("evaluate.calibrate", "evaluate", ["sme", "biz-owner"], "project.use", "calibration.open", (s) => s.facts.calibrated, true),
  step("release.sign", "release", ["release-ops"], "release.sign", "gate.open", (s) => s.facts.release_live),
  step("release.api_key", "release", ["developer"], "project.write", "apikey.create", (s) => s.facts.api_keys > 0),
  step("release.cedar", "release", ["release-ops"], "project.write", "cedar.open", (s) => s.facts.cedar_mode === "ENFORCE", true),
  step("observe.traces", "observe", ["developer"], "project.use", "traces.open", (s) => s.facts.spans_7d === true),
  // alert rules are workspace-wide and written by administrators
  step("observe.alerts", "observe", ["release-ops"], "admin", "alerts.open", (s) => s.facts.alert_rules > 0, true),
  step("feedback.triage", "feedback", ["developer", "biz-owner"], "project.write", "issues.open",
    (s) => s.facts.issues_open === 0 || s.facts.issues_resolved > 0),
  step("feedback.loop", "feedback", ["biz-owner"], "golden.admit", "admission.open", (s) => s.facts.golden_from_feedback > 0),
];

/** The administrator's line: from a workspace to a project people can work in. */
export interface PlatformStoryState {
  workspaceReady: boolean;
  accounts: number;
  projects: number;
  members: number;
  modelsConfigured: boolean;
  networkProfiles: number;
  storageMounts: number;
}

export const PLATFORM_STORY: readonly StoryStep<PlatformStoryState>[] = [
  step("platform.workspace", "define", ["admin"], "admin", "workspace.open", (s) => s.workspaceReady),
  step("platform.accounts", "define", ["admin"], "admin", "users.open", (s) => s.accounts > 0),
  step("platform.projects", "define", ["admin"], "admin", "projects.open", (s) => s.projects > 0 && s.members > 0),
  step("platform.models", "define", ["admin"], "admin", "models.open", (s) => s.modelsConfigured),
  step("platform.network", "define", ["admin"], "admin", "network.open", (s) => s.networkProfiles > 0, true),
  step("platform.storage", "define", ["admin"], "admin", "storage.open", (s) => s.storageMounts > 0, true),
];

/** The current step: the first required step not done; later ones only dim (D-3, no lock). */
export function currentStep<S>(steps: readonly StoryStep<S>[], state: S): StoryStep<S> | null {
  return steps.find((s) => !s.optional && !s.done(state)) ?? null;
}

export type StepStatus = "done" | "current" | "later" | "optional";

/** Each step's place: done, the current one, later (dimmed, still clickable) or an open optional. */
export function storyStatus<S>(steps: readonly StoryStep<S>[], state: S): { step: StoryStep<S>; status: StepStatus }[] {
  const current = currentStep(steps, state);
  return steps.map((s) => ({
    step: s,
    status: s.done(state) ? "done" : s === current ? "current" : s.optional ? "optional" : "later",
  }));
}

/** Per chapter: how many required steps are done (the journey strip, §3.2). */
export function chapterProgress<S>(steps: readonly StoryStep<S>[], state: S, chapters: readonly Chapter[] = AGENT_CHAPTERS) {
  return chapters.map((chapter) => {
    const inChapter = steps.filter((s) => s.chapter === chapter);
    const required = inChapter.filter((s) => !s.optional);
    return {
      chapter,
      steps: inChapter.map((s) => ({ step: s, done: s.done(state) })),
      done: required.filter((s) => s.done(state)).length,
      total: required.length,
    };
  }).filter((c) => c.steps.length > 0);
}

/** The project role templates (`access.ROLE_TEMPLATES`) the workshop names. */
const ROLE_OF_TEMPLATE: Record<string, WorkshopRole> = {
  developer: "developer",
  biz_owner: "biz-owner",
  release_ops: "release-ops",
  sme: "sme",
};

/** The caller's workshop roles from their project role keys; an administrator is `admin`. */
export function workshopRoles(roleKeys: readonly string[], isAdmin: boolean): WorkshopRole[] {
  if (isAdmin) return ["admin"];
  return [...new Set(roleKeys.map((k) => ROLE_OF_TEMPLATE[k]).filter((r): r is WorkshopRole => Boolean(r)))];
}

export interface StoryViewer {
  roles: readonly WorkshopRole[];
  /** the caller's effective key check; `admin` must answer true only for administrators */
  can: (key: string) => boolean;
}

/**
 * Whether a step is the viewer's to do: they hold its key and play one of its roles.
 * An administrator, or a caller whose project role is not a workshop template (a
 * custom role, `inherit`), is matched on the key alone — the same rule as the
 * backend's `agent_story.is_my_turn`.
 */
export function isMyTurn<S>(s: StoryStep<S>, viewer: StoryViewer): boolean {
  if (!viewer.can(s.perm)) return false;
  if (viewer.roles.length === 0 || viewer.roles.includes("admin")) return true;
  return s.roles.some((r) => viewer.roles.includes(r));
}

/** The steps a viewer may act on next: the current one when it is theirs (role filtering). */
export function myNextStep<S>(steps: readonly StoryStep<S>[], state: S, viewer: StoryViewer): StoryStep<S> | null {
  const current = currentStep(steps, state);
  return current && isMyTurn(current, viewer) ? current : null;
}

/** Whether an agent walks the storyline at all (MCP servers and presets do not, §3.2). */
export const onStoryline = (agent: Pick<StoryAgent, "kind">) => agent.kind === "agent";

/** The parameters a step's action reads from the agent's facts. */
export function stepParams(state: AgentStoryState): Record<string, string> {
  const out: Record<string, string> = { agent_id: state.agent.id };
  if (state.facts.criteria_lineage_id) out.lineage_id = state.facts.criteria_lineage_id;
  if (state.facts.golden_id) out.dataset_id = state.facts.golden_id;
  return out;
}
