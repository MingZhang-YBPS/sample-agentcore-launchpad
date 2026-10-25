import "./story.css";

import { ArrowLeft, ArrowRight } from "lucide-react";
import { useTranslation } from "react-i18next";
import { Link, useLocation, useSearchParams } from "react-router-dom";

import { pagePosition, stepHref, type StoryPosition, storyParams } from "./flow";
import { onStoryline, type StoryStep } from "./stories";
import { useAgentStory } from "./useStory";

/**
 * Where this page sits on the agent's line (storyline-ux §3.3), as the eyebrow
 * reads: "EVALUATE · STEP 3 / 8 · Build the golden set", with the steps either side
 * (render-tested). The chapter carries the signal colour of the console's eyebrow;
 * the step name is plain text, so the line reads as a caption, not a control.
 */
export function StoryHeaderView({ pos, href }: { pos: StoryPosition; href: (step: StoryStep) => string }) {
  const { t } = useTranslation();
  return (
    <nav className="v3-storypos" aria-label={t("story.header.label")} data-testid="v3-story-header" data-step={pos.step.key}>
      <span className="v3-storypos-where">
        <span className="v3-storypos-chapter">{t(`story.chapter.${pos.step.chapter}`)}</span>
        <span className="v3-storypos-sep" aria-hidden="true">·</span>
        <span className="mono v3-storypos-n">{t("story.stepOf", { n: pos.n, total: pos.total })}</span>
        <span className="v3-storypos-sep" aria-hidden="true">·</span>
        <span className="v3-storypos-name">{t(pos.step.labelKey)}</span>
      </span>
      <span className="v3-storypos-moves">
        {pos.prev && (
          <Link to={href(pos.prev)} className="v3-storypos-move" data-dir="prev" title={t(pos.prev.labelKey)}
            data-testid="v3-story-prev">
            <ArrowLeft size={12} aria-hidden="true" /> {t("story.header.prev")}
          </Link>
        )}
        {pos.next && (
          <Link to={href(pos.next)} className="v3-storypos-move" data-dir="next" title={t(pos.next.labelKey)}
            data-testid="v3-story-next">
            {t("story.header.next")} <ArrowRight size={12} aria-hidden="true" />
          </Link>
        )}
      </span>
    </nav>
  );
}

/**
 * The position line for a storyline page when an agent is chosen (`?agent=`); a page
 * off the line, no agent, or an agent that does not walk the line (MCP server,
 * preset) shows nothing. `view` overrides the page's `?view=` (the standards
 * workbench defaults to its scorecard).
 */
export function StoryHeader({ view }: { view?: string | null }) {
  const location = useLocation();
  const [params] = useSearchParams();
  const agentId = params.get("agent");
  const story = useAgentStory(agentId);
  const state = story.data;
  if (!agentId || (state && !onStoryline(state.agent))) return null;
  const pos = pagePosition(location.pathname, view !== undefined ? view : params.get("view"), state);
  if (!pos) return null;
  const p = storyParams(agentId, state);
  return <StoryHeaderView pos={pos} href={(s) => stepHref(s, p)} />;
}
