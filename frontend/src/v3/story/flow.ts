// The storyline's moves (storyline-ux §3): what comes after a step just done, where
// a page sits on the line, what a page still lacks, and the viewer the role checks
// read. Pure functions over `AGENT_STORY`, so the card, the strip, the page header
// and the empty states all agree on the same order — and are table-tested.
import type { ReactNode } from "react";

import { type ActionParams, NEXT_ACTIONS } from "./actions";
import {
  AGENT_STORY,
  type AgentStoryState,
  currentStep,
  isMyTurn,
  stepParams,
  type StoryStep,
  type StoryViewer,
} from "./stories";

const byKey = new Map(AGENT_STORY.map((s) => [s.key, s]));

export function storyStep(key: string): StoryStep | null {
  return byKey.get(key) ?? null;
}

/** The console path a step's action opens, with what the page already knows. */
export function stepHref(step: StoryStep, params: ActionParams): string {
  return NEXT_ACTIONS[step.action].to(params);
}

/** The action parameters: the agent's facts (lineage, golden set) under the caller's own. */
export function storyParams(agentId: string | null, state: AgentStoryState | null, extra: ActionParams = {}): ActionParams {
  return { ...(state ? stepParams(state) : {}), ...(agentId ? { agent_id: agentId } : {}), ...extra };
}

/**
 * The next two required steps after `doneKey` (the card's "next" and "then", §3.1).
 * Order is the story's; with the agent's facts, steps already done are skipped (the
 * user may have jumped ahead, D-3). Optional steps never fill "next": they are
 * offered on the strip, not pushed.
 */
export function nextAfter(doneKey: string, state: AgentStoryState | null): { next: StoryStep | null; then: StoryStep | null } {
  const at = AGENT_STORY.findIndex((s) => s.key === doneKey);
  const rest = AGENT_STORY.slice(at + 1).filter((s) => !s.optional && !(state && s.done(state)));
  return { next: rest[0] ?? null, then: rest[1] ?? null };
}

/** The pages whose `?view=` is a page of its own (the standards workbench's tabs). */
const VIEW_PAGES = new Set(["/v3/standards"]);

function target(step: StoryStep): { path: string; view: string | null } {
  const url = new URL(NEXT_ACTIONS[step.action].to({ agent_id: "_" }), "http://x");
  return { path: url.pathname, view: url.searchParams.get("view") };
}

/** The steps a page serves: its path, and its view where the view is a page. */
export function stepsOnPage(pathname: string, view: string | null): StoryStep[] {
  return AGENT_STORY.filter((s) => {
    const to = target(s);
    if (to.path !== pathname) return false;
    return !VIEW_PAGES.has(pathname) || !to.view || to.view === view;
  });
}

export interface StoryPosition {
  step: StoryStep;
  /** 1-based within the chapter, optional steps counted (they are on the line too) */
  n: number;
  total: number;
  prev: StoryStep | null;
  next: StoryStep | null;
}

/**
 * Where a page sits on the line (§3.3): of the steps it serves, the agent's current
 * one when it is here, else the first one not done, else the first. Null off the line.
 */
export function pagePosition(pathname: string, view: string | null, state: AgentStoryState | null): StoryPosition | null {
  const here = stepsOnPage(pathname, view);
  if (here.length === 0) return null;
  const current = state ? currentStep(AGENT_STORY, state) : null;
  const step = (current && here.includes(current) ? current : null)
    ?? (state ? here.find((s) => !s.done(state)) : null)
    ?? here[0];
  const chapter = AGENT_STORY.filter((s) => s.chapter === step.chapter);
  const i = AGENT_STORY.indexOf(step);
  return {
    step,
    n: chapter.indexOf(step) + 1,
    total: chapter.length,
    prev: AGENT_STORY[i - 1] ?? null,
    next: AGENT_STORY[i + 1] ?? null,
  };
}

export interface Requirement {
  step: StoryStep;
  done: boolean;
  /** the first one not done: lit, the rest dim (as the launch sequence) */
  next: boolean;
  /** not the viewer's to do: "needs biz-owner" */
  waiting: boolean;
}

/**
 * What a page still lacks before `stepKey` makes sense (§3.5, guided-console §6): the
 * required steps before it, from the same `AGENT_STORY` — no per-page list. Empty
 * when every one is done (the empty state then says nothing extra).
 */
export function requirementsFor(stepKey: string, state: AgentStoryState, viewer: StoryViewer | null): Requirement[] {
  const at = AGENT_STORY.findIndex((s) => s.key === stepKey);
  if (at < 0) return [];
  const before = AGENT_STORY.slice(0, at).filter((s) => !s.optional);
  if (before.every((s) => s.done(state))) return [];
  const first = before.find((s) => !s.done(state));
  return before.map((s) => {
    const done = s.done(state);
    return { step: s, done, next: s === first, waiting: !done && viewer !== null && !isMyTurn(s, viewer) };
  });
}

/**
 * The viewer the role checks read: the workshop roles `/api/me/next-steps` reports and
 * the console's key check. `admin` is administrators only; `project.use` and
 * `project.write` are the project's own floor — the console does not hold them as
 * keys, so it does not hide a step on them (the server still refuses).
 */
export function storyViewer(roles: StoryViewer["roles"], isAdmin: boolean, can: (key: string) => boolean): StoryViewer {
  return {
    roles,
    can: (key) => (key === "admin" ? isAdmin : key.startsWith("project.") ? true : can(key)),
  };
}

/** A next-step card's content (§3.1), set by an action's success path. */
export interface NextStepNote {
  /** what just happened, already translated ("Criteria table v3 published") */
  done: ReactNode;
  /** the `AGENT_STORY` step the action completed; the card offers the ones after it */
  after: string;
  agentId: string | null;
  /** parameters the page knows (lineage, golden set), under the facts' own */
  params?: ActionParams;
}

/** Whether the card belongs where the user is: it stays until dismissed or they move on. */
export const noteVisible = (at: string | null, here: string) => at !== null && at === here;

/** What a deploy that just settled means on the line: a first deploy, or a candidate. */
export function settledNote(
  state: AgentStoryState, version: string | number | null | undefined, t: (k: string, o?: Record<string, unknown>) => string,
): NextStepNote {
  const candidate = state.facts.endpoint_mode === "live";
  return {
    done: candidate
      // the facts are cached for 30 s: the version comes from the agent row the page polls
      ? t("story.done.candidate", { version: version ?? state.facts.candidate_version ?? "—" })
      : t("story.done.created", { name: state.agent.display_name || state.agent.name }),
    after: candidate ? "evaluate.candidate" : "build.create",
    agentId: state.agent.id,
  };
}
