import { LayoutTemplate, SlidersHorizontal, Sparkles } from "lucide-react";
import type { ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { useNavigate } from "react-router-dom";

import { Button } from "../../ui";
import { FIRST_AGENT_PATHS } from "./common";

const STEPS: { key: "describe" | "template" | "configure"; to: string; icon: ReactNode; primary?: boolean }[] = [
  { key: "describe", to: FIRST_AGENT_PATHS.assistant, icon: <Sparkles size={18} aria-hidden="true" />, primary: true },
  { key: "template", to: FIRST_AGENT_PATHS.template, icon: <LayoutTemplate size={18} aria-hidden="true" /> },
  { key: "configure", to: FIRST_AGENT_PATHS.configure, icon: <SlidersHorizontal size={18} aria-hidden="true" /> },
];

/**
 * First-run hero for a workspace with no agents: three ways to the first deployed
 * agent, with the architect assistant as the primary call to action. Replaces the
 * KPI row (every tile would read zero) until the first agent exists.
 */
export function FirstAgentHero() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  return (
    <section className="v2-card v2-home-hero" data-testid="v2-home-first-run">
      <div className="v2-card-body">
        <h2 className="v2-home-hero-title">{t("v2.home.firstRun.title")}</h2>
        <p className="v2-muted v2-home-hero-sub">{t("v2.home.firstRun.sub")}</p>
        <ol className="v2-home-hero-steps">
          {STEPS.map((step, i) => (
            <li key={step.key} className={step.primary ? "primary" : undefined} data-testid={`v2-home-first-${step.key}`}>
              <div className="head">
                <span className="n">{i + 1}</span>
                <span className="icon">{step.icon}</span>
              </div>
              <h3>{t(`v2.home.firstRun.${step.key}`)}</h3>
              <p>{t(`v2.home.firstRun.${step.key}Desc`)}</p>
              <Button kind={step.primary ? "primary" : undefined} onClick={() => navigate(step.to)}>
                {t(`v2.home.firstRun.${step.key}Cta`)}
              </Button>
            </li>
          ))}
        </ol>
      </div>
    </section>
  );
}
