import "./story.css";

import { ArrowRight, ChevronDown, Link2, Route } from "lucide-react";
import { useEffect, useId, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";

import { type NextStepNote, settledNote, stepHref, storyParams } from "./flow";
import { NextStepCard } from "./NextStepCard";
import { useJourneyCollapsed } from "./prefs";
import {
  AGENT_STORY,
  type AgentStoryState,
  chapterProgress,
  currentStep,
  isMyTurn,
  onStoryline,
  type StoryStep,
  type StoryViewer,
  storyStatus,
} from "./stories";
import { useAgentStory, useCopyStepLink, useStoryViewer } from "./useStory";

interface ViewProps {
  state: AgentStoryState;
  viewer: StoryViewer;
  collapsed: boolean;
  onToggle: () => void;
  href: (step: StoryStep) => string;
  onCopy?: (step: StoryStep) => void;
}

/**
 * The agent's journey (storyline-ux §3.2), from its facts (render-tested): one
 * segment per chapter, a dot per step — mint done, amber the current one (breathing),
 * hollow the later ones, dashed the optional ones — and on the right the one thing to
 * do: "Continue: <step> (<role>)", or "Waiting on <role>" with a link to hand them.
 * Every dot is a link (D-3: nothing is locked, later steps are only dimmer).
 */
export function JourneyStripView({ state, viewer, collapsed, onToggle, href, onCopy }: ViewProps) {
  const { t } = useTranslation();
  const bodyId = useId();
  const status = new Map(storyStatus(AGENT_STORY, state).map((r) => [r.step.key, r.status]));
  const chapters = chapterProgress(AGENT_STORY, state);
  const current = currentStep(AGENT_STORY, state);
  const mine = current ? isMyTurn(current, viewer) : false;
  const required = AGENT_STORY.filter((s) => !s.optional);
  const doneCount = required.filter((s) => s.done(state)).length;

  const cta = !current ? (
    <span className="v3-journey-end" data-testid="v3-journey-end">{t("story.journey.allDone")}</span>
  ) : mine ? (
    <Link to={href(current)} className="v3-btn sm primary v3-journey-cta" data-testid="v3-journey-continue" data-step={current.key}>
      {t("story.journey.continue", { step: t(current.labelKey), role: t(`story.role.${current.role}`) })}
      <ArrowRight size={13} aria-hidden="true" />
    </Link>
  ) : (
    <span className="v3-journey-wait" data-testid="v3-journey-waiting" data-step={current.key}>
      <span className="v3-journey-waittext">
        <b>{t("story.waiting", { role: t(`story.role.${current.role}`) })}</b>
        <small>{t(current.labelKey)}</small>
      </span>
      <button type="button" className="v3-btn ghost sm" onClick={() => onCopy?.(current)} title={t("story.copyLink")}
        aria-label={t("story.copyLink")}>
        <Link2 size={13} aria-hidden="true" />
      </button>
    </span>
  );

  return (
    <section className="v3-journey" data-collapsed={collapsed || undefined} data-testid="v3-journey"
      aria-label={t("story.journey.title")}>
      <header className="v3-journey-head">
        <button type="button" className="v3-journey-toggle" aria-expanded={!collapsed} aria-controls={bodyId} onClick={onToggle}>
          <Route size={14} aria-hidden="true" />
          <span>{t("story.journey.title")}</span>
          <span className="mono v3-journey-count">{doneCount}/{required.length}</span>
          <ChevronDown size={14} aria-hidden="true" className="chev" />
        </button>
        {collapsed && (
          <span className="v3-journey-meter" aria-hidden="true">
            {required.map((s) => <i key={s.key} data-s={status.get(s.key)} />)}
          </span>
        )}
        <span className="v3-journey-act">{cta}</span>
      </header>
      <ol id={bodyId} className="v3-journey-line" hidden={collapsed}>
        {chapters.map((c, ci) => (
          <li key={c.chapter} className="v3-journey-chapter" data-complete={c.done === c.total || undefined}
            data-here={current?.chapter === c.chapter || undefined} style={{ ["--i" as string]: ci }}>
            <div className="v3-journey-label">
              <span>{t(`story.chapter.${c.chapter}`)}</span>
              <span className="mono">{c.done}/{c.total}</span>
            </div>
            <div className="v3-journey-dots">
              {c.steps.map(({ step }) => {
                const s = status.get(step.key) ?? "later";
                const name = t(step.labelKey);
                const what = t(`story.status.${s}`);
                return (
                  <Link key={step.key} to={href(step)} className="v3-journey-dot" data-s={s} data-step={step.key}
                    aria-label={`${name} · ${what}`}>
                    <i aria-hidden="true" />
                    <span className="v3-journey-tip" role="tooltip">
                      <b>{name}</b>
                      <small>{what} · {t(`story.role.${step.role}`)}</small>
                    </span>
                  </Link>
                );
              })}
            </div>
          </li>
        ))}
      </ol>
    </section>
  );
}

/**
 * The strip on the V3 agent detail page, with the facts it reads. MCP servers and
 * system presets do not walk the line, so they get nothing. When a deploy this page
 * watched settles (`deploying` → `active`), the next-step card for "created" or "new
 * candidate" opens right under the strip.
 */
export function JourneyStrip({ agentId, status, version }: { agentId: string; status: string; version?: string | number | null }) {
  const { t } = useTranslation();
  const story = useAgentStory(agentId, status);
  const viewer = useStoryViewer();
  const copy = useCopyStepLink();
  const [collapsed, setCollapsed] = useJourneyCollapsed();
  const [note, setNote] = useState<NextStepNote | null>(null);
  const settling = useRef(false);
  const prev = useRef(status);
  useEffect(() => {
    if (prev.current === "deploying" && status === "active") settling.current = true;
    prev.current = status;
  }, [status]);
  const state = story.data;
  useEffect(() => {
    // wait for the facts read after the status change, then say what comes next
    if (!settling.current || !state || story.loading || state.agent.status !== "active") return;
    settling.current = false;
    if (onStoryline(state.agent)) setNote(settledNote(state, version, t));
  }, [state, story.loading, version, t]);

  if (!state || !onStoryline(state.agent)) return null;
  const params = storyParams(agentId, state);
  const href = (s: StoryStep) => stepHref(s, params);
  return (
    <>
      <JourneyStripView state={state} viewer={viewer} collapsed={collapsed} onToggle={() => setCollapsed(!collapsed)}
        href={href} onCopy={(s) => copy(href(s))} />
      {note && <NextStepCard note={note} state={state} onDismiss={() => setNote(null)} />}
    </>
  );
}
