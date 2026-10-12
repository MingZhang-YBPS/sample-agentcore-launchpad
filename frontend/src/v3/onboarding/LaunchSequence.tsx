import { ArrowRight, Check, ChevronDown, X } from "lucide-react";
import { useId, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";

import { api, type AgentInfo, type Workspace } from "../../lib/api";
import type { ReleaseState } from "../../lib/dlc";
import { launchSteps } from "./steps";
import { useLoad } from "../hooks";
import { Lamp, type Signal } from "../ui";
import { useLaunchHidden } from "./state";

/**
 * Launch sequence — the newcomer's way through, on the command center. It reads
 * like the rest of the console: a lamp per step, the next one lit, done ones
 * quiet. Hidden per workspace once dismissed (the help menu brings it back), and
 * it folds itself into one line when every step is done.
 */
export function LaunchSequence({
  workspace,
  agents,
  releases,
}: {
  workspace: Workspace | null;
  agents: AgentInfo[];
  releases: Record<string, ReleaseState | null> | null;
}) {
  const { t } = useTranslation();
  const ws = workspace?.id ?? "";
  const [hidden, setHidden] = useLaunchHidden(ws);
  const [expanded, setExpanded] = useState<boolean | null>(null);
  const stepsId = useId();
  const showSteps = expanded ?? agents.length === 0;
  const live = agents.filter((a) => a.status === "active").slice(0, 3);
  // has anyone talked to an agent here yet: the first few live agents' sessions
  const conversed = useLoad(
    () =>
      live.length
        ? Promise.all(live.map((a) => api.listChatSessions(a.id).then((r) => r.sessions.length > 0).catch(() => false))).then((xs) => xs.some(Boolean))
        : Promise.resolve(false),
    `v3-launch-chat:${ws}:${live.map((a) => a.id).join(",")}`,
  );
  const evaluated = useLoad(
    () => api.listEvaluationRuns({ limit: 1 }).then((r) => r.runs.length > 0).catch(() => false),
    `v3-launch-eval:${ws}`,
  );
  if (hidden || !workspace) return null;

  const steps = launchSteps({
    workspace,
    agents,
    conversed: Boolean(conversed.data),
    evaluated: Boolean(evaluated.data),
    releases: releases ?? {},
  });
  const done = steps.filter((s) => s.done).length;
  const next = steps.find((s) => !s.done) ?? null;

  if (!next) {
    return (
      <div className="v3-launch done" data-tour="launch">
        <Lamp s="ok" live />
        <span>{t("v3.onboard.launch.allDone")}</span>
        <button type="button" className="v3-btn ghost sm" onClick={() => setHidden(true)}>{t("v3.onboard.launch.hide")}</button>
      </div>
    );
  }

  return (
    <section className="v3-launch" data-tour="launch" aria-label={t("v3.onboard.launch.title")}>
      <header>
        <button type="button" className="v3-launch-toggle" aria-expanded={showSteps} aria-controls={stepsId}
          onClick={() => setExpanded(!showSteps)}>
          <span>{t("v3.onboard.launch.title")}</span>
          <ChevronDown size={14} />
        </button>
        <span className="count mono">{done}/{steps.length}</span>
        <span className="meter" aria-hidden="true">
          {steps.map((s) => <i key={s.key} data-on={s.done ? "true" : undefined} />)}
        </span>
        <button type="button" className="v3-btn ghost sm" aria-label={t("v3.onboard.launch.hide")} title={t("v3.onboard.launch.hide")}
          onClick={() => setHidden(true)}>
          <X size={14} />
        </button>
      </header>
      {!showSteps && <div className="v3-launch-next">
        <span><Lamp s="wait" /> {t(`v3.onboard.launch.${next.key}`)}</span>
        <Link to={next.to} className="v3-btn sm">{t(`v3.onboard.launch.${next.key}Cta`)} <ArrowRight size={13} /></Link>
      </div>}
      <ol id={stepsId} hidden={!showSteps}>
        {steps.map((s, i) => {
          const isNext = s.key === next.key;
          const sig: Signal = s.done ? "ok" : isNext ? "wait" : "off";
          return (
            <li key={s.key} data-state={s.done ? "done" : isNext ? "next" : "later"}>
              <div className="top">
                <span className="n mono">{String(i + 1).padStart(2, "0")}</span>
                <Lamp s={sig} live={isNext} />
                {s.done && <Check size={14} className="tick" aria-hidden="true" />}
              </div>
              <b>{t(`v3.onboard.launch.${s.key}`)}</b>
              <small>{t(`v3.onboard.launch.${s.key}Sub`)}</small>
              {!s.done && (
                <Link to={s.to} className={isNext ? "v3-btn sm primary" : "v3-btn sm ghost"}>
                  {t(`v3.onboard.launch.${s.key}Cta`)} <ArrowRight size={13} />
                </Link>
              )}
            </li>
          );
        })}
      </ol>
    </section>
  );
}
