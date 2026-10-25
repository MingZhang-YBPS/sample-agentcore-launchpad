import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import { NEXT_ACTIONS } from "./actions";
import {
  AGENT_STORY,
  type AgentStoryFacts,
  type AgentStoryState,
  chapterProgress,
  currentStep,
  isMyTurn,
  myNextStep,
  PLATFORM_STORY,
  type PlatformStoryState,
  stepParams,
  type StoryViewer,
  storyStatus,
  workshopRoles,
} from "./stories";

const here = dirname(fileURLToPath(import.meta.url));
const read = (rel: string) => readFileSync(resolve(here, rel), "utf-8");
const locale = (lang: string) => JSON.parse(read(`../../locales/${lang}/common.json`));

const EMPTY: AgentStoryFacts = {
  agent_active: true, sessions: 0, knowledge_bases: 0, skills: 0,
  criteria_lineage_id: null, criteria_draft: false, criteria_draft_version: null,
  criteria_published: false, criteria_version: null, criteria_signed: false, criteria_signed_by: null,
  golden_id: null, golden_sealed: false, golden_items: 0, golden_from_feedback: 0,
  dev_run_done: false, dev_run_id: null, endpoint_mode: "default",
  candidate_version: null, candidate_record_id: null, gate_verdict: null, gate_record_id: null,
  release_live: false, api_keys: 0, cedar_mode: null, spans_7d: null, alert_rules: 0,
  issues_open: 0, issues_resolved: 0, calibrated: false, waivers_pending: 0,
};
const state = (facts: Partial<AgentStoryFacts> = {}): AgentStoryState => ({
  agent: { id: "ag-1", name: "lab-service-assistant", display_name: null, status: "active", method: "harness", kind: "agent" },
  facts: { ...EMPTY, ...facts },
});

/** Facts with every step up to (not including) `key` done, in story order. */
const PROGRESS: [string, Partial<AgentStoryFacts>][] = [
  ["build.create", { agent_active: false }],
  ["build.chat", {}],
  ["evaluate.criteria.draft", { sessions: 1 }],
  ["evaluate.criteria.sign", { sessions: 1, criteria_published: true, criteria_lineage_id: "lin-1" }],
  ["evaluate.golden.seal", { sessions: 1, criteria_published: true, criteria_signed: true }],
  ["evaluate.dev_run", { sessions: 1, criteria_published: true, criteria_signed: true, golden_sealed: true }],
  ["evaluate.dual_endpoint", { sessions: 1, criteria_published: true, criteria_signed: true, golden_sealed: true, dev_run_done: true }],
  ["evaluate.candidate", { sessions: 1, criteria_published: true, criteria_signed: true, golden_sealed: true, dev_run_done: true, endpoint_mode: "live" }],
  ["evaluate.gate", { sessions: 1, criteria_published: true, criteria_signed: true, golden_sealed: true, dev_run_done: true, endpoint_mode: "live", candidate_version: "2" }],
  ["release.sign", { sessions: 1, criteria_published: true, criteria_signed: true, golden_sealed: true, dev_run_done: true, endpoint_mode: "live", candidate_version: "2", gate_verdict: "PASS" }],
  ["release.api_key", { sessions: 1, criteria_published: true, criteria_signed: true, golden_sealed: true, dev_run_done: true, endpoint_mode: "live", release_live: true }],
  ["observe.traces", { sessions: 1, criteria_published: true, criteria_signed: true, golden_sealed: true, dev_run_done: true, endpoint_mode: "live", release_live: true, api_keys: 1 }],
  ["feedback.triage", { sessions: 1, criteria_published: true, criteria_signed: true, golden_sealed: true, dev_run_done: true, endpoint_mode: "live", release_live: true, api_keys: 1, spans_7d: true, issues_open: 2 }],
  ["feedback.loop", { sessions: 1, criteria_published: true, criteria_signed: true, golden_sealed: true, dev_run_done: true, endpoint_mode: "live", release_live: true, api_keys: 1, spans_7d: true }],
];

describe("AGENT_STORY: done from data, the current step", () => {
  it.each(PROGRESS)("the current step is %s", (key, facts) => {
    expect(currentStep(AGENT_STORY, state(facts))?.key).toBe(key);
  });

  it("the whole line done has no current step", () => {
    const facts = { ...PROGRESS[PROGRESS.length - 1][1], golden_from_feedback: 1 };
    expect(currentStep(AGENT_STORY, state(facts))).toBeNull();
  });

  it("optional steps are never current, and later steps dim without locking", () => {
    const rows = storyStatus(AGENT_STORY, state({ sessions: 1 }));
    expect(rows.find((r) => r.step.key === "build.kb")?.status).toBe("optional");
    expect(rows.find((r) => r.step.key === "evaluate.criteria.draft")?.status).toBe("current");
    expect(rows.find((r) => r.step.key === "release.sign")?.status).toBe("later");
    // done out of order still reads done (D-3: steps are not locked)
    const jumped = storyStatus(AGENT_STORY, state({ sessions: 1, api_keys: 1 }));
    expect(jumped.find((r) => r.step.key === "release.api_key")?.status).toBe("done");
  });

  it("a draft counts as drafted; signing needs the published version signed", () => {
    expect(AGENT_STORY.find((s) => s.key === "evaluate.criteria.draft")?.done(state({ criteria_draft: true }))).toBe(true);
    const sign = AGENT_STORY.find((s) => s.key === "evaluate.criteria.sign")!;
    expect(sign.done(state({ criteria_signed: true }))).toBe(false);
    expect(sign.done(state({ criteria_published: true, criteria_signed: true }))).toBe(true);
  });

  it("unknown spans are not done; feedback triage needs nothing open or one handled", () => {
    const traces = AGENT_STORY.find((s) => s.key === "observe.traces")!;
    expect(traces.done(state({ spans_7d: null }))).toBe(false);
    expect(traces.done(state({ spans_7d: false }))).toBe(false);
    const triage = AGENT_STORY.find((s) => s.key === "feedback.triage")!;
    expect(triage.done(state({ issues_open: 2 }))).toBe(false);
    expect(triage.done(state({ issues_open: 2, issues_resolved: 1 }))).toBe(true);
    expect(triage.done(state())).toBe(true);
  });

  it("chapter progress counts required steps only", () => {
    const build = chapterProgress(AGENT_STORY, state({ sessions: 1 }))[0];
    expect(build).toMatchObject({ chapter: "build", done: 2, total: 2 });
    expect(build.steps).toHaveLength(4);
  });

  it("an action reads the agent, its criteria lineage and golden set", () => {
    expect(stepParams(state({ criteria_lineage_id: "lin-1", golden_id: "gs-1" }))).toEqual({
      agent_id: "ag-1", lineage_id: "lin-1", dataset_id: "gs-1",
    });
    const sign = AGENT_STORY.find((s) => s.key === "evaluate.criteria.sign")!;
    expect(NEXT_ACTIONS[sign.action].to(stepParams(state({ criteria_lineage_id: "lin-1" }))))
      .toBe("/v3/standards?agent=ag-1&view=criteria&lineage=lin-1");
  });
});

// ── role filtering: the five workshop accounts ──────────────────────────────────

const KEYS: Record<string, string[]> = {
  // access.ROLE_TEMPLATES (backend/app/services/access.py)
  developer: ["project.use", "project.write", "agents.deploy", "agents.import", "agents.delete", "agents.convert",
    "eval.run", "promotion.request", "identity.manage", "identity.grant", "memory.manage", "criteria.manage"],
  biz_owner: ["project.use", "project.write", "eval.run", "criteria.manage", "criteria.sign", "golden.admit",
    "judge.calibrate", "identity.grant"],
  release_ops: ["project.use", "project.write", "mcp.approve", "eval.run", "promotion.request", "promotion.approve",
    "identity.grant", "criteria.manage", "waiver.approve", "release.sign"],
  sme: ["project.use", "identity.grant"],
};
const viewer = (account: "developer" | "biz-owner" | "release-ops" | "sme" | "admin"): StoryViewer => {
  if (account === "admin") return { roles: workshopRoles([], true), can: () => true };
  const template = account.replace("-", "_");
  const keys = new Set(KEYS[template]);
  return { roles: workshopRoles([template], false), can: (k) => k !== "admin" && keys.has(k) };
};

/** For each current step: which of the five accounts it is the turn of. */
const TURNS: [string, Partial<AgentStoryFacts>, string[]][] = [
  ["build.chat", PROGRESS[1][1], ["developer", "admin"]],
  ["evaluate.criteria.draft", PROGRESS[2][1], ["developer", "admin"]],
  ["evaluate.criteria.sign", PROGRESS[3][1], ["biz-owner", "admin"]],
  ["evaluate.golden.seal", PROGRESS[4][1], ["biz-owner", "admin"]],
  ["evaluate.dev_run", PROGRESS[5][1], ["developer", "admin"]],
  ["evaluate.dual_endpoint", PROGRESS[6][1], ["release-ops", "admin"]],
  ["evaluate.candidate", PROGRESS[7][1], ["developer", "admin"]],
  ["evaluate.gate", PROGRESS[8][1], ["release-ops", "admin"]],
  ["release.sign", PROGRESS[9][1], ["release-ops", "admin"]],
  ["release.api_key", PROGRESS[10][1], ["developer", "admin"]],
  ["feedback.triage", PROGRESS[12][1], ["developer", "biz-owner", "admin"]],
  ["feedback.loop", PROGRESS[13][1], ["biz-owner", "admin"]],
];
const ACCOUNTS = ["developer", "biz-owner", "release-ops", "sme", "admin"] as const;

describe("role filtering over the five workshop accounts", () => {
  it.each(TURNS)("%s is the turn of %j", (key, facts, whose) => {
    const s = state(facts);
    expect(currentStep(AGENT_STORY, s)?.key).toBe(key);
    const mine = ACCOUNTS.filter((a) => myNextStep(AGENT_STORY, s, viewer(a)) !== null);
    expect(mine).toEqual(whose);
  });

  it("the sme labels for calibration but is never handed a signature", () => {
    const calibrate = AGENT_STORY.find((s) => s.key === "evaluate.calibrate")!;
    expect(isMyTurn(calibrate, viewer("sme"))).toBe(true);
    for (const s of AGENT_STORY.filter((x) => ["criteria.sign", "golden.admit", "release.sign"].includes(x.perm))) {
      expect(isMyTurn(s, viewer("sme"))).toBe(false);
    }
  });

  it("holding the key is not enough without the role, and the role not without the key", () => {
    const gate = AGENT_STORY.find((s) => s.key === "evaluate.gate")!;
    expect(viewer("developer").can("eval.run")).toBe(true);
    expect(isMyTurn(gate, viewer("developer"))).toBe(false);
    const revoked: StoryViewer = { roles: ["release-ops"], can: () => false };
    expect(isMyTurn(gate, revoked)).toBe(false);
    // a custom project role is matched on its keys alone
    expect(isMyTurn(gate, { roles: workshopRoles(["outsourced"], false), can: (k) => k === "eval.run" })).toBe(true);
  });

  it("administrator-only steps stay the administrator's", () => {
    const alerts = AGENT_STORY.find((s) => s.key === "observe.alerts")!;
    expect(isMyTurn(alerts, viewer("release-ops"))).toBe(false);
    expect(isMyTurn(alerts, viewer("admin"))).toBe(true);
  });

  it("maps project role templates to workshop roles", () => {
    expect(workshopRoles(["biz_owner", "release_ops", "inherit"], false)).toEqual(["biz-owner", "release-ops"]);
    expect(workshopRoles(["developer"], true)).toEqual(["admin"]);
  });
});

describe("PLATFORM_STORY", () => {
  const platform = (s: Partial<PlatformStoryState> = {}): PlatformStoryState => ({
    workspaceReady: false, accounts: 0, projects: 0, members: 0, modelsConfigured: false,
    networkProfiles: 0, storageMounts: 0, ...s,
  });
  it("walks workspace → accounts → projects → models, network and storage optional", () => {
    expect(currentStep(PLATFORM_STORY, platform())?.key).toBe("platform.workspace");
    expect(currentStep(PLATFORM_STORY, platform({ workspaceReady: true }))?.key).toBe("platform.accounts");
    expect(currentStep(PLATFORM_STORY, platform({ workspaceReady: true, accounts: 4, projects: 1 }))?.key).toBe("platform.projects");
    expect(currentStep(PLATFORM_STORY, platform({ workspaceReady: true, accounts: 4, projects: 1, members: 4 }))?.key).toBe("platform.models");
    expect(currentStep(PLATFORM_STORY, platform({ workspaceReady: true, accounts: 4, projects: 1, members: 4, modelsConfigured: true }))).toBeNull();
  });
  it("is the administrator's only", () => {
    for (const s of PLATFORM_STORY) {
      expect(isMyTurn(s, viewer("admin"))).toBe(true);
      expect(ACCOUNTS.filter((a) => a !== "admin").some((a) => isMyTurn(s, viewer(a)))).toBe(false);
    }
  });
});

// ── consistency: actions, copy, and the backend mirror ─────────────────────────

describe("consistency", () => {
  const steps = [...AGENT_STORY, ...PLATFORM_STORY];
  it("every step's action is in NEXT_ACTIONS and keys are unique", () => {
    for (const s of steps) expect(NEXT_ACTIONS[s.action], s.key).toBeDefined();
    expect(new Set(steps.map((s) => s.key)).size).toBe(steps.length);
  });

  it("every step, chapter and role has en + zh-CN copy", () => {
    for (const lang of ["en", "zh-CN"]) {
      const story = locale(lang).story;
      for (const s of steps) {
        expect(story.step[s.key], `${lang} ${s.key}`).toBeTruthy();
        expect(story.chapter[s.chapter], `${lang} ${s.chapter}`).toBeTruthy();
        for (const r of s.roles) expect(story.role[r], `${lang} ${r}`).toBeTruthy();
      }
    }
  });

  it("mirrors the backend's AGENT_STEPS: keys, order, permission keys and roles", () => {
    const py = read("../../../../backend/app/services/agent_story.py");
    const backend = [...py.matchAll(/Step\("([a-z_.]+)", "([a-z]+)", "([a-z_.]+)", \(([^)]*)\)/g)].map((m) => ({
      key: m[1], chapter: m[2], perm: m[3], roles: [...m[4].matchAll(/"([a-z-]+)"/g)].map((r) => r[1]),
    }));
    expect(backend.map((b) => b.key)).toEqual(AGENT_STORY.map((s) => s.key));
    for (const s of AGENT_STORY) {
      const b = backend.find((x) => x.key === s.key)!;
      expect({ chapter: b.chapter, perm: b.perm, roles: b.roles }, s.key).toEqual({ chapter: s.chapter, perm: s.perm, roles: [...s.roles] });
    }
    const block = py.slice(py.indexOf("AGENT_STEPS"), py.indexOf("WORKSHOP_ROLES ="));
    const optional = block.split("Step(\"").slice(1)
      .filter((chunk) => chunk.includes("optional=True")).map((chunk) => chunk.slice(0, chunk.indexOf("\"")));
    expect(optional).toEqual(AGENT_STORY.filter((s) => s.optional).map((s) => s.key));
  });
});
