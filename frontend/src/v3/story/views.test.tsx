import { renderToStaticMarkup } from "react-dom/server";
import { MemoryRouter } from "react-router-dom";
import type { ReactNode } from "react";
import { beforeAll, describe, expect, it, vi } from "vitest";

import i18n from "../../i18n";
import type { MyNextSteps } from "../../lib/api/story";
import { TermCard } from "../onboarding/Glossary";
import { nextAfter, noteVisible, pagePosition, requirementsFor, settledNote, stepHref, storyParams, storyViewer } from "./flow";
import { JourneyStripView } from "./JourneyStrip";
import { NextStepCardView } from "./NextStepCard";
import { PrereqsView } from "./Prereqs";
import { StoryHeaderView } from "./StoryHeader";
import { type AgentStoryFacts, type AgentStoryState, type StoryStep, workshopRoles } from "./stories";
import { YourTurnView } from "./YourTurn";

const html = (node: ReactNode) => renderToStaticMarkup(<MemoryRouter>{node}</MemoryRouter>);

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
  agent: { id: "ag-1", name: "lab-service-assistant", display_name: "Service assistant", status: "active", method: "harness", kind: "agent" },
  facts: { ...EMPTY, ...facts },
});
const PUBLISHED = { sessions: 1, criteria_published: true, criteria_lineage_id: "lin-1" };

const developer = storyViewer(workshopRoles(["developer"], false), false, (k) => ["agents.deploy", "eval.run", "criteria.manage"].includes(k));
const bizOwner = storyViewer(workshopRoles(["biz_owner"], false), false, (k) => ["criteria.sign", "golden.admit"].includes(k));
const hrefFor = (s: AgentStoryState) => (step: StoryStep) => stepHref(step, storyParams("ag-1", s));

beforeAll(async () => {
  vi.stubGlobal("document", { documentElement: {} });
  await i18n.changeLanguage("en");
});

describe("NextStepCard", () => {
  const s = state(PUBLISHED);
  const { next, then } = nextAfter("evaluate.criteria.draft", s);

  it("done, the next step with its button, and what comes then", () => {
    const out = html(<NextStepCardView done="Criteria table v3 published" next={next} then={then} waiting={false} href={hrefFor(s)} onDismiss={() => undefined} />);
    expect(out).toContain("Criteria table v3 published");
    expect(out).toContain('data-step="evaluate.criteria.sign"');
    expect(out).toContain("Sign the criteria table");
    expect(out).toContain('href="/v3/standards?agent=ag-1&amp;view=criteria&amp;lineage=lin-1"');
    expect(out).toContain('data-testid="v3-next-step-go"');
    expect(out).toContain("Build the golden set and seal its splits");
    expect(out).toContain('role="status"');
    expect(out).toContain('aria-label="Dismiss"');
  });

  it("another role's turn: waiting on them, a link to hand over, no button", () => {
    const out = html(<NextStepCardView done="published" next={next} then={then} waiting href={hrefFor(s)} />);
    expect(out).toContain("Waiting on biz-owner");
    expect(out).toContain('data-testid="v3-next-step-copy"');
    expect(out).toContain("Copy link for them");
    expect(out).not.toContain('data-testid="v3-next-step-go"');
  });

  it("the end of the line", () => {
    const out = html(<NextStepCardView done="Issue marked fixed" next={null} then={null} waiting={false} href={hrefFor(s)} />);
    expect(out).toContain('data-testid="v3-next-step-end"');
    expect(out).not.toContain("v3-next-step-then");
  });

  it("stays where it was shown, and only there", () => {
    expect(noteVisible("/v3/gate?agent=a", "/v3/gate?agent=a")).toBe(true);
    expect(noteVisible("/v3/gate?agent=a", "/v3/gate?agent=b")).toBe(false);
    expect(noteVisible(null, "/v3/gate")).toBe(false);
  });

  it("a settled deploy: first deploy → created, live endpoints → candidate", () => {
    const t = (k: string, o?: Record<string, unknown>) => `${k}:${JSON.stringify(o ?? {})}`;
    expect(settledNote(state(), 1, t).after).toBe("build.create");
    const cand = settledNote(state({ endpoint_mode: "live" }), "4", t);
    expect(cand.after).toBe("evaluate.candidate");
    expect(String(cand.done)).toContain('"version":"4"');
  });
});

describe("JourneyStrip", () => {
  it("chapters with step dots, the current one amber, and Continue for the viewer's turn", () => {
    const s = state({ sessions: 1 });
    const out = html(<JourneyStripView state={s} viewer={developer} collapsed={false} onToggle={() => undefined} href={hrefFor(s)} />);
    for (const c of ["Build", "Evaluate", "Release", "Observe", "Improve"]) expect(out).toContain(c);
    expect(out).toContain('data-step="evaluate.criteria.draft" aria-label="Draft the criteria table · Current step"');
    expect(out).toMatch(/data-s="current" data-step="evaluate.criteria.draft"/);
    expect(out).toMatch(/data-s="done" data-step="build.create"/);
    expect(out).toMatch(/data-s="optional" data-step="build.kb"/);
    expect(out).toContain('data-testid="v3-journey-continue"');
    expect(out).toContain("Continue: Draft the criteria table (developer)");
    expect(out).toContain('aria-expanded="true"');
  });

  it("someone else's turn: Waiting for the role, with the link", () => {
    const s = state(PUBLISHED);
    const out = html(<JourneyStripView state={s} viewer={developer} collapsed={false} onToggle={() => undefined} href={hrefFor(s)} />);
    expect(out).toContain('data-testid="v3-journey-waiting"');
    expect(out).toContain("Waiting on biz-owner");
    const mine = html(<JourneyStripView state={s} viewer={bizOwner} collapsed={false} onToggle={() => undefined} href={hrefFor(s)} />);
    expect(mine).toContain("Continue: Sign the criteria table (biz-owner)");
  });

  it("folded: one line with the meter, the steps hidden", () => {
    const s = state({ sessions: 1 });
    const out = html(<JourneyStripView state={s} viewer={developer} collapsed onToggle={() => undefined} href={hrefFor(s)} />);
    expect(out).toContain('aria-expanded="false"');
    expect(out).toContain("v3-journey-meter");
    expect(out).toMatch(/<ol[^>]*hidden=""/);
  });
});

describe("StoryHeader", () => {
  it("Evaluate · Step 3 / 8 · the step, with previous and next", () => {
    const s = state({ ...PUBLISHED, criteria_signed: true });
    const pos = pagePosition("/v3/standards", "golden", s)!;
    const out = html(<StoryHeaderView pos={pos} href={hrefFor(s)} />);
    expect(out).toContain("Evaluate");
    expect(out).toContain("Step 3 / 8");
    expect(out).toContain("Build the golden set and seal its splits");
    expect(out).toContain('data-testid="v3-story-prev"');
    expect(out).toContain('href="/v3/tasks?view=new&amp;agent=ag-1"');
  });
});

describe("YourTurn", () => {
  const agent = { id: "ag-1", name: "lab-service-assistant", display_name: null, status: "active", method: "harness", kind: "agent" as const };
  const data: MyNextSteps = {
    workspace_id: "ws", project_id: "p", roles: ["biz-owner"], computed_at: "",
    mine: [{ agent, step: "evaluate.criteria.sign", chapter: "evaluate", perm: "criteria.sign", roles: ["biz-owner"] }],
    waiting: [
      { agent: { ...agent, id: "ag-2", name: "lab-policy-advisor" }, step: "release.sign", chapter: "release", perm: "release.sign", roles: ["release-ops"] },
      { agent, step: "no.such.step", chapter: "release", perm: "x", roles: ["developer"] },
    ],
  };

  it("my turns with a button each; the others' quiet, saying who has it", () => {
    const out = html(<YourTurnView data={data} />);
    expect(out).toContain("Your turn (biz-owner)");
    expect(out).toContain("Sign the criteria table");
    expect(out).toContain('href="/v3/standards?agent=ag-1&amp;view=criteria"');
    expect(out).toContain("Waiting on others");
    expect(out).toContain("lab-policy-advisor · Waiting on release-ops");
    expect(out).not.toContain("no.such.step");
  });

  it("nothing on either side renders nothing", () => {
    expect(html(<YourTurnView data={{ ...data, mine: [], waiting: [] }} />)).toBe("");
    expect(html(<YourTurnView data={{ ...data, mine: [] }} />)).toContain("Nothing waits on you right now.");
  });
});

describe("Prereqs", () => {
  it("done ticked, the first missing one lit with its action, someone else's says who", () => {
    const s = state({ sessions: 1 });
    const out = html(<PrereqsView reqs={requirementsFor("evaluate.golden.seal", s, developer)} href={hrefFor(s)} />);
    expect(out).toContain('data-testid="v3-prereqs"');
    expect(out).toContain("Before this page: 2 to go");
    expect(out).toMatch(/data-state="done" data-step="build.chat"/);
    expect(out).toMatch(/data-state="next" data-step="evaluate.criteria.draft"/);
    expect(out).toContain("Needs biz-owner");
    expect(out).toContain("Open the criteria table");
  });

  it("nothing missing renders nothing", () => {
    expect(html(<PrereqsView reqs={[]} href={() => "/"} />)).toBe("");
  });
});

describe("Agent-DLC term card", () => {
  it("the sentence, the steps it is used in as links for the agent, who, related words", () => {
    const out = html(<TermCard term="goldenSet" agentId="ag-1" />);
    expect(out).toContain("Golden set");
    expect(out).toContain("Used in");
    expect(out).toContain('href="/v3/standards?agent=ag-1&amp;view=golden"');
    expect(out).toContain('data-step="feedback.loop"');
    expect(out).toContain("biz-owner admits");
    expect(out).toContain("Sample admission");
  });
});
