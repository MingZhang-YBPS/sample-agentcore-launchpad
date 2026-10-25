import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import { nextAfter, pagePosition, requirementsFor, stepHref, stepsOnPage, storyParams, storyStep, storyViewer } from "./flow";
import { AGENT_STORY, type AgentStoryFacts, type AgentStoryState, workshopRoles } from "./stories";
import { DLC_TERM_CARDS, DLC_TERMS } from "./terms";

const here = dirname(fileURLToPath(import.meta.url));
const locale = (lang: string) => JSON.parse(readFileSync(resolve(here, `../../locales/${lang}/common.json`), "utf-8"));

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
const SIGNED = { sessions: 1, criteria_published: true, criteria_signed: true, criteria_lineage_id: "lin-1" };

const developer = storyViewer(workshopRoles(["developer"], false), false, (k) => ["agents.deploy", "eval.run", "criteria.manage"].includes(k));
const bizOwner = storyViewer(workshopRoles(["biz_owner"], false), false, (k) => ["criteria.sign", "golden.admit"].includes(k));

describe("the next-step card's steps (storyline-ux §3.1)", () => {
  // the ten key actions: the step each completes → the next required one and the one after
  const TABLE: [string, string, string | null][] = [
    ["build.create", "build.chat", "evaluate.criteria.draft"],
    ["evaluate.criteria.draft", "evaluate.criteria.sign", "evaluate.golden.seal"],
    ["evaluate.criteria.sign", "evaluate.golden.seal", "evaluate.dev_run"],
    ["evaluate.golden.seal", "evaluate.dev_run", "evaluate.dual_endpoint"],
    ["evaluate.dev_run", "evaluate.dual_endpoint", "evaluate.candidate"],
    ["evaluate.candidate", "evaluate.gate", "release.sign"],
    ["evaluate.gate", "release.sign", "release.api_key"],
    ["release.sign", "release.api_key", "observe.traces"],
    ["release.api_key", "observe.traces", "feedback.triage"],
    ["feedback.triage", "feedback.loop", null],
  ];
  it.each(TABLE)("after %s: next %s, then %s (optional steps are never pushed)", (done, next, then) => {
    const out = nextAfter(done, null);
    expect(out.next?.key).toBe(next);
    expect(out.then?.key ?? null).toBe(then);
  });

  it("with the facts, steps already done are skipped (the user jumped ahead)", () => {
    const out = nextAfter("evaluate.criteria.sign", state({ ...SIGNED, golden_sealed: true }));
    expect(out.next?.key).toBe("evaluate.dev_run");
  });

  it("the last step leaves nothing next", () => {
    expect(nextAfter("feedback.loop", null)).toEqual({ next: null, then: null });
  });

  it("links carry the agent and the facts' lineage, the page's own params win", () => {
    const p = storyParams("ag-1", state(SIGNED), { dataset_id: "gs-9" });
    expect(stepHref(storyStep("evaluate.criteria.sign")!, p)).toBe("/v3/standards?agent=ag-1&view=criteria&lineage=lin-1");
    expect(stepHref(storyStep("evaluate.golden.seal")!, p)).toBe("/v3/standards?agent=ag-1&view=golden&golden=gs-9");
  });
});

describe("the page header's position (§3.3)", () => {
  it("every step's page finds the step back (reverse lookup covers the line)", () => {
    for (const s of AGENT_STORY) {
      const url = new URL(stepHref(s, { agent_id: "ag-1" }), "http://x");
      expect(stepsOnPage(url.pathname, url.searchParams.get("view")).map((x) => x.key)).toContain(s.key);
    }
  });

  it("Evaluate · step 3 / 8 on the golden view, with the steps either side", () => {
    const pos = pagePosition("/v3/standards", "golden", state(SIGNED));
    expect(pos?.step.key).toBe("evaluate.golden.seal");
    expect([pos?.n, pos?.total]).toEqual([3, 8]);
    expect(pos?.prev?.key).toBe("evaluate.criteria.sign");
    expect(pos?.next?.key).toBe("evaluate.dev_run");
  });

  it("a page serving two steps shows the agent's current one", () => {
    expect(pagePosition("/v3/standards", "criteria", state({ sessions: 1 }))?.step.key).toBe("evaluate.criteria.draft");
    expect(pagePosition("/v3/standards", "criteria", state({ sessions: 1, criteria_published: true }))?.step.key)
      .toBe("evaluate.criteria.sign");
    expect(pagePosition("/v3/gate", null, state({ ...SIGNED, golden_sealed: true, dev_run_done: true, endpoint_mode: "live", candidate_version: "2", gate_verdict: "PASS" }))?.step.key)
      .toBe("release.sign");
  });

  it("off the line, or the standards scorecard, shows nothing", () => {
    expect(pagePosition("/v3/costs-nowhere", null, null)).toBeNull();
    expect(pagePosition("/v3/standards", "scorecard", null)).toBeNull();
    expect(pagePosition("/v3/tasks", null, null)?.step.key).toBe("evaluate.dev_run");
  });
});

describe("empty-state prerequisites from AGENT_STORY (§3.5)", () => {
  it("the gate lists the required steps before it, the first missing one lit", () => {
    const reqs = requirementsFor("evaluate.gate", state(SIGNED), developer);
    expect(reqs.map((r) => r.step.key)).toEqual([
      "build.create", "build.chat", "evaluate.criteria.draft", "evaluate.criteria.sign",
      "evaluate.golden.seal", "evaluate.dev_run", "evaluate.dual_endpoint", "evaluate.candidate",
    ]);
    const next = reqs.filter((r) => r.next);
    expect(next.map((r) => r.step.key)).toEqual(["evaluate.golden.seal"]);
    // sealing is biz-owner's: the developer sees who it waits on
    expect(reqs.find((r) => r.step.key === "evaluate.golden.seal")?.waiting).toBe(true);
    expect(requirementsFor("evaluate.gate", state(SIGNED), bizOwner).find((r) => r.step.key === "evaluate.golden.seal")?.waiting).toBe(false);
  });

  it("nothing missing renders nothing", () => {
    expect(requirementsFor("evaluate.golden.seal", state(SIGNED), developer)).toEqual([]);
    expect(requirementsFor("no.such.step", state(), developer)).toEqual([]);
  });
});

describe("the viewer", () => {
  it("admin steps are administrators' only; the project floor keys never hide a step", () => {
    const v = storyViewer(["developer"], false, () => false);
    expect(v.can("admin")).toBe(false);
    expect(v.can("project.use")).toBe(true);
    expect(v.can("project.write")).toBe(true);
    expect(v.can("criteria.sign")).toBe(false);
    expect(storyViewer(["admin"], true, () => true).can("admin")).toBe(true);
  });
});

describe("Agent-DLC term cards (guided-console §5.2)", () => {
  const en = locale("en");
  const zh = locale("zh-CN");
  it("seventeen terms, each step a storyline step, each related word a card", () => {
    expect(DLC_TERMS).toHaveLength(17);
    for (const term of DLC_TERMS) {
      const card = DLC_TERM_CARDS[term];
      expect(card.steps.length).toBeGreaterThan(0);
      for (const k of card.steps) expect(storyStep(k), `${term} → ${k}`).not.toBeNull();
      for (const r of card.related) expect(DLC_TERMS).toContain(r);
    }
  });

  it("name, sentence and owner in en and zh-CN; zh-CN uses full-width punctuation", () => {
    for (const term of DLC_TERMS) {
      for (const field of ["name", "short", "owner"]) {
        expect(en.story.term[term]?.[field], `en ${term}.${field}`).toBeTruthy();
        expect(zh.story.term[term]?.[field], `zh ${term}.${field}`).toBeTruthy();
      }
      expect(zh.story.term[term].short).not.toMatch(/[,;:!?]\s|[一-鿿][,.;:!?]/);
    }
  });

  it("every story.* key the surfaces use exists in both languages", () => {
    const keys = (o: Record<string, unknown>, p = ""): string[] =>
      Object.entries(o).flatMap(([k, v]) => (v && typeof v === "object" ? keys(v as Record<string, unknown>, `${p}${k}.`) : [`${p}${k}`]));
    expect(keys(zh.story).sort()).toEqual(keys(en.story).sort());
  });
});
