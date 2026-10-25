// E2E walkthrough of the workshop main line (storyline-ux §5, SG-5): from an empty
// agent to the feedback loop, one required `AGENT_STORY` step at a time. The backend
// is a mocked `/api/agents/{id}/story` that answers the facts the step just made
// true; after each step the next-step card and the journey strip's "continue" must
// both point at the next step, on the page that serves it, for the same agent.
import { renderToStaticMarkup } from "react-dom/server";
import type { ReactNode } from "react";
import { MemoryRouter } from "react-router-dom";
import { afterAll, beforeAll, describe, expect, it, vi } from "vitest";

import i18n from "../../../i18n";
import { storyApi } from "../../../lib/api/story";
import { nextAfter, pagePosition, stepHref, storyParams, storyViewer } from "../flow";
import { JourneyStripView } from "../JourneyStrip";
import { NextStepCardView } from "../NextStepCard";
import {
  AGENT_STORY,
  type AgentStoryFacts,
  type AgentStoryState,
  currentStep,
  type StoryStep,
  type StoryViewer,
  type WorkshopRole,
  workshopRoles,
} from "../stories";

const AGENT = "ag-walk";
const html = (node: ReactNode) => renderToStaticMarkup(<MemoryRouter>{node}</MemoryRouter>);

/** The workshop's accounts: their project role template and the keys it grants. */
const ACCOUNTS: Record<Exclude<WorkshopRole, "admin">, { template: string; keys: string[] }> = {
  developer: { template: "developer", keys: ["agents.deploy", "eval.run", "criteria.manage"] },
  "biz-owner": { template: "biz_owner", keys: ["criteria.sign", "golden.admit"] },
  "release-ops": { template: "release_ops", keys: ["release.sign", "eval.run"] },
  sme: { template: "sme", keys: [] },
};
const viewerOf = (role: WorkshopRole): StoryViewer => {
  if (role === "admin") return storyViewer(workshopRoles([], true), true, () => true);
  const { template, keys } = ACCOUNTS[role];
  return storyViewer(workshopRoles([template], false), false, (k) => keys.includes(k));
};

/** A fresh workspace: the agent row exists (deploying), nothing else has happened —
 *  except a user's feedback is waiting, which is what the feedback chapter works on. */
const START: AgentStoryFacts = {
  agent_active: false, sessions: 0, knowledge_bases: 0, skills: 0,
  criteria_lineage_id: null, criteria_draft: false, criteria_draft_version: null,
  criteria_published: false, criteria_version: null, criteria_signed: false, criteria_signed_by: null,
  golden_id: null, golden_sealed: false, golden_items: 0, golden_from_feedback: 0,
  dev_run_done: false, dev_run_id: null, endpoint_mode: "default",
  candidate_version: null, candidate_record_id: null, gate_verdict: null, gate_record_id: null,
  release_live: false, api_keys: 0, cedar_mode: null, spans_7d: null, alert_rules: 0,
  issues_open: 1, issues_resolved: 0, calibrated: false, waivers_pending: 0,
};

/** What doing each required step changes in the facts the backend reports. */
const EFFECT: Record<string, Partial<AgentStoryFacts>> = {
  "build.create": { agent_active: true },
  "build.chat": { sessions: 1 },
  "evaluate.criteria.draft": { criteria_draft: true, criteria_draft_version: 1, criteria_lineage_id: "lin-walk" },
  "evaluate.criteria.sign": { criteria_published: true, criteria_version: 1, criteria_signed: true, criteria_signed_by: "bob" },
  "evaluate.golden.seal": { golden_id: "gs-walk", golden_sealed: true, golden_items: 24 },
  "evaluate.dev_run": { dev_run_done: true, dev_run_id: "run-walk" },
  "evaluate.dual_endpoint": { endpoint_mode: "live" },
  "evaluate.candidate": { candidate_version: "2", candidate_record_id: "rec-walk" },
  "evaluate.gate": { gate_verdict: "pass", gate_record_id: "gate-walk" },
  "release.sign": { release_live: true },
  "release.api_key": { api_keys: 1 },
  "observe.traces": { spans_7d: true },
  "feedback.triage": { issues_open: 0, issues_resolved: 1 },
  "feedback.loop": { golden_from_feedback: 1 },
};

const REQUIRED = AGENT_STORY.filter((s) => !s.optional);

/** The mocked backend: `/story` answers whatever the walk has done so far. */
let facts: AgentStoryFacts = { ...START };
const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
  const url = String(input);
  if (url === `/api/agents/${AGENT}/story`) {
    return new Response(JSON.stringify({
      agent: { id: AGENT, name: "lab-service-assistant", display_name: "Service assistant",
        status: facts.agent_active ? "active" : "deploying", method: "harness", kind: "agent" },
      facts, cached: false, ttl_seconds: 30,
    }), { status: 200, headers: { "Content-Type": "application/json" } });
  }
  return new Response(JSON.stringify({ code: "http.404", message: "Not Found" }), { status: 404 });
});

async function readStory(): Promise<AgentStoryState> {
  const p = await storyApi.agentStory(AGENT);
  return { agent: p.agent, facts: p.facts };
}

/** Where a link lands: the step the landing page's header shows, and its agent. */
function landing(href: string, state: AgentStoryState) {
  const url = new URL(href, "http://console");
  const agent = url.pathname === "/v3/agents" ? url.searchParams.get("id") : url.searchParams.get("agent");
  return { step: pagePosition(url.pathname, url.searchParams.get("view"), state)?.step.key ?? null, agent };
}

const attr = (out: string, testId: string, name: string) =>
  new RegExp(`<[^>]*data-testid="${testId}"[^>]*>`).exec(out)?.[0].match(new RegExp(`${name}="([^"]*)"`))?.[1]
    .replace(/&amp;/g, "&") ?? null;

beforeAll(async () => {
  vi.stubGlobal("document", { documentElement: {} });
  vi.stubGlobal("fetch", fetchMock);
  await i18n.changeLanguage("en");
});

afterAll(() => {
  vi.unstubAllGlobals();
});

describe("workshop main line, step by step (storyline-ux §5)", () => {
  it("the mocked backend walks every required step", () => {
    expect(REQUIRED.map((s) => s.key).sort()).toEqual(Object.keys(EFFECT).sort());
  });

  it("an empty agent starts at build.create and every step hands over to the next", async () => {
    facts = { ...START };
    let state = await readStory();
    expect(currentStep(AGENT_STORY, state)?.key).toBe("build.create");

    const walked: string[] = [];
    let handovers = 0;
    for (const [i, step] of REQUIRED.entries()) {
      // the step's own account does it; the backend now reports its fact
      facts = { ...facts, ...EFFECT[step.key] };
      state = await readStory();
      expect(step.done(state), `${step.key} ticked from data`).toBe(true);
      walked.push(step.key);

      const expected: StoryStep | undefined = REQUIRED[i + 1];
      const params = storyParams(AGENT, state);
      const href = (s: StoryStep) => stepHref(s, params);

      // 1) the next-step card the success path shows
      const { next, then } = nextAfter(step.key, state);
      expect(next?.key ?? null, `card after ${step.key}`).toBe(expected?.key ?? null);
      const card = html(<NextStepCardView done="ok" next={next} then={then} waiting={false} href={href} />);
      if (!expected) {
        expect(card).toContain('data-testid="v3-next-step-end"');
        continue;
      }
      expect(attr(card, "v3-next-step-next", "data-step")).toBe(expected.key);
      const go = attr(card, "v3-next-step-go", "href");
      expect(go, `card button after ${step.key}`).not.toBeNull();
      expect(landing(go!, state), `card lands for ${expected.key}`).toEqual({ step: expected.key, agent: AGENT });

      // 2) the journey strip's "continue", for the account whose turn it now is
      expect(currentStep(AGENT_STORY, state)?.key).toBe(expected.key);
      const strip = html(<JourneyStripView state={state} viewer={viewerOf(expected.role)} collapsed={false}
        onToggle={() => undefined} href={href} />);
      expect(attr(strip, "v3-journey-continue", "data-step"), `strip after ${step.key}`).toBe(expected.key);
      const cont = attr(strip, "v3-journey-continue", "href");
      expect(landing(cont!, state), `strip lands for ${expected.key}`).toEqual({ step: expected.key, agent: AGENT });
      expect(cont).toBe(go);

      // 3) anyone else sees "waiting on <role>" instead of a button they cannot use
      const other = (Object.keys(ACCOUNTS) as WorkshopRole[]).find((r) => !expected.roles.includes(r) && r !== "sme");
      if (other && !viewerOf(other).can(expected.perm)) {
        const theirs = html(<JourneyStripView state={state} viewer={viewerOf(other)} collapsed={false}
          onToggle={() => undefined} href={href} />);
        expect(theirs).toContain('data-testid="v3-journey-waiting"');
        expect(theirs).not.toContain('data-testid="v3-journey-continue"');
        handovers += 1;
      }
    }

    expect(walked).toEqual(REQUIRED.map((s) => s.key));
    // the line changes hands (developer → biz-owner → release-ops …) more than once
    expect(handovers).toBeGreaterThan(3);
    expect(currentStep(AGENT_STORY, state)).toBeNull();
    const end = html(<JourneyStripView state={state} viewer={viewerOf("developer")} collapsed={false}
      onToggle={() => undefined} href={(s) => stepHref(s, storyParams(AGENT, state))} />);
    expect(end).toContain('data-testid="v3-journey-end"');
    // every read went through the mocked story endpoint
    expect(fetchMock.mock.calls.every(([u]) => String(u) === `/api/agents/${AGENT}/story`)).toBe(true);
  });

  it("links carry what the page needs: the lineage on criteria, the golden set on golden", async () => {
    facts = { ...START, ...EFFECT["build.create"], ...EFFECT["build.chat"], ...EFFECT["evaluate.criteria.draft"] };
    const state = await readStory();
    const { next } = nextAfter("evaluate.criteria.draft", state);
    const to = new URL(stepHref(next!, storyParams(AGENT, state)), "http://console");
    expect(to.pathname).toBe("/v3/standards");
    expect(to.searchParams.get("view")).toBe("criteria");
    expect(to.searchParams.get("lineage")).toBe("lin-walk");
    expect(to.searchParams.get("agent")).toBe(AGENT);
  });
});
