import "./story.css";

import { ArrowRight, Check } from "lucide-react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";

import { type Requirement, requirementsFor, stepHref, storyParams } from "./flow";
import { onStoryline, type StoryStep } from "./stories";
import { useAgentStory, useStoryViewer } from "./useStory";

/**
 * What this page still lacks (storyline-ux §3.5, guided-console §6), render-tested:
 * the steps before it, done ones ticked and quiet, the first missing one lit with
 * its link, later ones dim; a step that is someone else's says who.
 */
export function PrereqsView({ reqs, href }: { reqs: Requirement[]; href: (step: StoryStep) => string }) {
  const { t } = useTranslation();
  if (reqs.length === 0) return null;
  const missing = reqs.filter((r) => !r.done).length;
  return (
    <div className="v3-prereqs" data-testid="v3-prereqs">
      <div className="v3-prereqs-head">{t("story.prereqs.title", { count: missing })}</div>
      <ol>
        {reqs.map((r) => (
          <li key={r.step.key} data-state={r.done ? "done" : r.next ? "next" : "later"} data-step={r.step.key}>
            <span className="v3-prereqs-mark" aria-hidden="true">{r.done ? <Check size={11} strokeWidth={3} /> : null}</span>
            <span className="v3-prereqs-name">
              {t(r.step.labelKey)}
              {r.waiting && <small>{t("story.prereqs.needs", { role: t(`story.role.${r.step.role}`) })}</small>}
            </span>
            {!r.done && (
              <Link to={href(r.step)} className={r.next ? "v3-btn sm primary" : "v3-btn sm ghost"}>
                {t(`apiNext.${r.step.action}`)} {r.next && <ArrowRight size={12} aria-hidden="true" />}
              </Link>
            )}
          </li>
        ))}
      </ol>
    </div>
  );
}

/** The checklist for an empty state, before `stepKey` (an `AGENT_STORY` key), for `agentId`. */
export function StoryPrereqs({ agentId, stepKey }: { agentId: string | null | undefined; stepKey: string }) {
  const story = useAgentStory(agentId);
  const viewer = useStoryViewer();
  const state = story.data;
  if (!agentId || !state || !onStoryline(state.agent)) return null;
  const p = storyParams(agentId, state);
  return <PrereqsView reqs={requirementsFor(stepKey, state, viewer)} href={(s) => stepHref(s, p)} />;
}
