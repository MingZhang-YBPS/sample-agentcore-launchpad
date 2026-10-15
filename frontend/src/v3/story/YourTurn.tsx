import "./story.css";

import { ArrowRight, Hourglass } from "lucide-react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";

import type { MyNextSteps, NextStepRow } from "../../lib/api/story";
import { storyApi } from "../../lib/api/story";
import { useWorkspace } from "../../workspace/workspace-context";
import { useLoad } from "../hooks";
import { Lamp } from "../ui";
import { stepHref, storyStep } from "./flow";

const agentLabel = (row: NextStepRow) => row.agent.display_name || row.agent.name;

/** A row's link: the step's action on that agent; unknown steps (a newer backend) are dropped. */
function rowHref(row: NextStepRow): string | null {
  const step = storyStep(row.step);
  return step ? stepHref(step, { agent_id: row.agent.id }) : null;
}

/**
 * "Your turn / Waiting on others" (storyline-ux §3.4), from `/api/me/next-steps`
 * (render-tested). Your turns are amber lamps with a button each — they wait on
 * you; the others' are quiet rows that only say who has the ball. Nothing to show
 * at all renders nothing: the launch sequence above covers a new workspace.
 */
export function YourTurnView({ data, limit = 6 }: { data: MyNextSteps; limit?: number }) {
  const { t } = useTranslation();
  const mine = data.mine.filter((r) => rowHref(r) !== null);
  const waiting = data.waiting.filter((r) => storyStep(r.step) !== null);
  if (mine.length === 0 && waiting.length === 0) return null;
  const roles = data.roles.filter((r) => r !== "admin").map((r) => t(`story.role.${r}`)).join(" · ");
  return (
    <section className="v3-yourturn" data-testid="v3-your-turn" aria-label={t("story.turn.title")}>
      <div className="v3-yourturn-col" data-kind="mine">
        <header>
          <span className="v3-yourturn-title">{roles ? t("story.turn.mineAs", { roles }) : t("story.turn.mine")}</span>
          <span className="mono v3-yourturn-n">{mine.length}</span>
        </header>
        {mine.length === 0 ? (
          <p className="v3-yourturn-quiet" data-testid="v3-your-turn-none">{t("story.turn.none")}</p>
        ) : (
          <ul>
            {mine.slice(0, limit).map((row) => (
              <li key={`${row.agent.id}:${row.step}`} data-step={row.step}>
                <Lamp s="wait" live />
                <span className="v3-yourturn-what">
                  <b>{t(`story.step.${row.step}`)}</b>
                  <small className="mono">{agentLabel(row)} · {t(`story.chapter.${row.chapter}`)}</small>
                </span>
                <Link to={rowHref(row) as string} className="v3-btn sm" data-testid="v3-your-turn-go">
                  {t("story.card.go")} <ArrowRight size={13} aria-hidden="true" />
                </Link>
              </li>
            ))}
            {mine.length > limit && <li className="v3-yourturn-more">{t("story.turn.more", { count: mine.length - limit })}</li>}
          </ul>
        )}
      </div>
      {waiting.length > 0 && (
        <div className="v3-yourturn-col" data-kind="waiting">
          <header>
            <span className="v3-yourturn-title">{t("story.turn.waiting")}</span>
            <span className="mono v3-yourturn-n">{waiting.length}</span>
          </header>
          <ul>
            {waiting.slice(0, limit).map((row) => (
              <li key={`${row.agent.id}:${row.step}`} data-step={row.step}>
                <Hourglass size={13} aria-hidden="true" className="v3-yourturn-glass" />
                <span className="v3-yourturn-what">
                  <span>{t(`story.step.${row.step}`)}</span>
                  <small className="mono">{agentLabel(row)} · {t("story.waiting", { role: t(`story.role.${row.roles[0] ?? "developer"}`) })}</small>
                </span>
              </li>
            ))}
            {waiting.length > limit && <li className="v3-yourturn-more">{t("story.turn.more", { count: waiting.length - limit })}</li>}
          </ul>
        </div>
      )}
    </section>
  );
}

/** The home page's panel: the caller's turns across the current project's agents. */
export function YourTurn({ refresh = "" }: { refresh?: string }) {
  const { current } = useWorkspace();
  const steps = useLoad(() => storyApi.myNextSteps(), `v3-home-turns:${current?.id ?? ""}:${refresh}`);
  return steps.data ? <YourTurnView data={steps.data} /> : null;
}
