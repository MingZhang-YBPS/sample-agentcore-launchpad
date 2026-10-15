import "./story.css";

import { ArrowRight, Check, Link2, X } from "lucide-react";
import type { ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";

import { nextAfter, type NextStepNote, stepHref, storyParams } from "./flow";
import { type AgentStoryState, isMyTurn, type StoryStep } from "./stories";
import { useAgentStory, useCopyStepLink, useStoryViewer } from "./useStory";

interface ViewProps {
  done: ReactNode;
  next: StoryStep | null;
  then: StoryStep | null;
  /** the next step is someone else's: say who, offer the link instead of the button */
  waiting: boolean;
  href: (step: StoryStep) => string;
  onCopy?: (step: StoryStep) => void;
  onDismiss?: () => void;
}

/**
 * The card itself, from resolved steps (render-tested). Mint rail: this went as
 * intended; the route line runs from DONE to NEXT, and THEN sits quieter under it —
 * the eye lands on the one button that continues the story. When the next step is
 * another role's, the rail stays mint (nothing went wrong) and the NEXT row turns
 * amber: "waiting on biz-owner", with a link to hand them.
 */
export function NextStepCardView({ done, next, then, waiting, href, onCopy, onDismiss }: ViewProps) {
  const { t } = useTranslation();
  return (
    <div className="v3-nextcard" role="status" data-testid="v3-next-step" data-waiting={waiting || undefined}>
      <span className="v3-nextcard-tick" aria-hidden="true"><Check size={13} strokeWidth={3} /></span>
      <div className="v3-nextcard-body">
        <div className="v3-nextcard-done">{done}</div>
        {next ? (
          <div className="v3-nextcard-row" data-kind={waiting ? "wait" : "next"} data-testid="v3-next-step-next" data-step={next.key}>
            <span className="v3-nextcard-tag">{t("story.callout.next")}</span>
            <span className="v3-nextcard-step">
              <b>{t(next.labelKey)}</b>
              <span className="v3-nextcard-role">
                {waiting ? t("story.waiting", { role: t(`story.role.${next.role}`) }) : t(`story.chapter.${next.chapter}`)}
              </span>
            </span>
            {waiting ? (
              <button type="button" className="v3-btn sm" onClick={() => onCopy?.(next)} data-testid="v3-next-step-copy">
                <Link2 size={13} aria-hidden="true" /> {t("story.copyLink")}
              </button>
            ) : (
              <Link to={href(next)} className="v3-btn sm primary" data-testid="v3-next-step-go">
                {t("story.card.go")} <ArrowRight size={13} aria-hidden="true" />
              </Link>
            )}
          </div>
        ) : (
          <div className="v3-nextcard-row" data-kind="end" data-testid="v3-next-step-end">
            <span className="v3-nextcard-tag">{t("story.callout.next")}</span>
            <span className="v3-nextcard-step"><b>{t("story.card.end")}</b></span>
          </div>
        )}
        {next && then && (
          <div className="v3-nextcard-then" data-testid="v3-next-step-then" data-step={then.key}>
            <span className="v3-nextcard-tag">{t("story.card.then")}</span>
            <Link to={href(then)} className="v3-nextcard-thenlink">{t(then.labelKey)}</Link>
          </div>
        )}
      </div>
      {onDismiss && (
        <button type="button" className="v3-btn ghost sm v3-nextcard-close" aria-label={t("story.callout.dismiss")}
          title={t("story.callout.dismiss")} onClick={onDismiss}>
          <X size={13} aria-hidden="true" />
        </button>
      )}
    </div>
  );
}

/**
 * "Done — here is what comes next" (storyline-ux §3.1), in place of the success toast
 * at the ten key actions. Reads the agent's facts so steps already done are skipped
 * and the links carry the right lineage / golden set; `state` skips that read when
 * the host already has it.
 */
export function NextStepCard({ note, state, onDismiss }: { note: NextStepNote; state?: AgentStoryState | null; onDismiss: () => void }) {
  const own = useAgentStory(state === undefined ? note.agentId : null);
  const facts = state ?? own.data;
  const viewer = useStoryViewer();
  const copy = useCopyStepLink();
  const { next, then } = nextAfter(note.after, facts);
  const params = storyParams(note.agentId, facts, note.params);
  const href = (s: StoryStep) => stepHref(s, params);
  return (
    <NextStepCardView done={note.done} next={next} then={then} waiting={Boolean(next && !isMyTurn(next, viewer))}
      href={href} onCopy={(s) => copy(href(s))} onDismiss={onDismiss} />
  );
}
