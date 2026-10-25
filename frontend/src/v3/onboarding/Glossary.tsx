import "../story/story.css";

import { BookOpen, Search } from "lucide-react";
import { type ReactNode, useId, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link, useLocation } from "react-router-dom";

import { GLOSSARY_TERMS, type GlossaryTerm } from "../../v2/Glossary";
import { stepHref, storyStep } from "../story/flow";
import { DLC_TERM_CARDS, DLC_TERMS, type DlcTerm, isDlcTerm } from "../story/terms";
import { Btn, Dialog } from "../ui";
import { closeGlossary, openGlossary, useGlossaryOpen } from "./state";

/** A term's name and one sentence: V2's platform words, or the Agent-DLC cards' (`story.term.*`). */
const termName = (term: string) => (isDlcTerm(term) ? `story.term.${term}.name` : `glossary.name.${term}`);
const termShort = (term: string) => (isDlcTerm(term) ? `story.term.${term}.short` : `glossary.${term}`);

/**
 * A platform term inline: dotted underline, its one-sentence meaning on hover and
 * keyboard focus (announced through aria-describedby), and a click that opens the
 * full glossary on that term. The sentences are V2's (`glossary.<term>`), so both
 * consoles explain a word the same way.
 */
export function Term({ term, children }: { term: GlossaryTerm | DlcTerm; children?: ReactNode }) {
  const { t } = useTranslation();
  const id = useId();
  return (
    <span className="v3-term">
      <button type="button" className="v3-term-word" aria-describedby={id} onClick={() => openGlossary(term)}>
        {children ?? t(termName(term))}
      </button>
      <span role="tooltip" id={id} className="v3-term-tip">
        {t(termShort(term))}
        {isDlcTerm(term) && (
          <span className="v3-term-steps">
            {DLC_TERM_CARDS[term].steps.map((k) => t(`story.step.${k}`)).join(" → ")}
          </span>
        )}
      </span>
    </span>
  );
}

/** The glossary dialog, mounted once by the shell; opened by Term, ⌘K or the help menu. */
export function GlossaryDialog() {
  const { t } = useTranslation();
  const open = useGlossaryOpen();
  const [q, setQ] = useState("");
  const location = useLocation();
  const agentId = new URLSearchParams(location.search).get("agent") ?? undefined;
  const matches = useMemo(() => {
    const needle = q.trim().toLowerCase();
    return (term: string) =>
      !needle ||
      `${term} ${t(termName(term))} ${t(termName(term), { lng: "en" })} ${t(termShort(term))}`
        .toLowerCase()
        .includes(needle);
  }, [q, t]);
  const rows = GLOSSARY_TERMS.filter(matches);
  const cards = DLC_TERMS.filter(matches);
  if (!open) return null;
  const focus = open !== "all" ? open : null;
  return (
    <Dialog
      wide
      title={
        <span style={{ display: "inline-flex", gap: 10, alignItems: "center" }}>
          <BookOpen size={18} aria-hidden="true" /> {t("v3.onboard.glossary.title")}
        </span>
      }
      onClose={() => {
        setQ("");
        closeGlossary();
      }}
      foot={<Btn kind="ghost" onClick={closeGlossary}>{t("v3.onboard.close")}</Btn>}
    >
      <div style={{ position: "relative", marginBottom: 12 }}>
        <Search size={14} style={{ position: "absolute", left: 12, top: 11, color: "var(--v3-text-3)" }} aria-hidden="true" />
        <input className="v3-input" style={{ paddingLeft: 34 }} value={q} autoFocus
          onChange={(e) => setQ(e.target.value)} placeholder={t("v3.onboard.glossary.search")} aria-label={t("v3.onboard.glossary.search")} />
      </div>
      <div className="v3-glossary-scroll">
      {cards.length > 0 && (
        <section className="v3-dlc-terms" aria-label={t("story.term.section")}>
          <div className="v3-dlc-terms-head">{t("story.term.section")}</div>
          <div className="v3-dlc-terms-grid">
            {cards.map((term) => (
              <TermCard key={term} term={term} focused={term === focus} agentId={agentId} />
            ))}
          </div>
        </section>
      )}
      <dl className="v3-glossary">
        {rows.map((term) => (
          <div key={term} className={term === focus ? "on" : undefined} ref={(el) => term === focus && el?.scrollIntoView({ block: "nearest" })}>
            <dt>{t(`glossary.name.${term}`)}</dt>
            <dd>{t(`glossary.${term}`)}</dd>
          </div>
        ))}
        {rows.length === 0 && cards.length === 0 && <p style={{ color: "var(--v3-text-3)" }}>{t("v3.onboard.glossary.none")}</p>}
      </dl>
      </div>
    </Dialog>
  );
}

/**
 * One Agent-DLC term as a card (guided-console §5.1): the sentence, who is
 * responsible, and the storyline steps it is used in — each a link that lands on that
 * step for the agent in the URL (or just the page without one) — then related words.
 */
export function TermCard({ term, focused, agentId }: { term: DlcTerm; focused?: boolean; agentId?: string }) {
  const { t } = useTranslation();
  const card = DLC_TERM_CARDS[term];
  return (
    <article className="v3-dlc-term" data-on={focused || undefined} data-term={term}
      ref={(el) => focused && el?.scrollIntoView({ block: "nearest" })}>
      <h3>{t(`story.term.${term}.name`)}</h3>
      <p>{t(`story.term.${term}.short`)}</p>
      <div className="v3-dlc-term-used">
        <span className="v3-dlc-term-tag">{t("story.term.usedIn")}</span>
        <ol>
          {card.steps.map((key) => {
            const step = storyStep(key);
            if (!step) return null;
            return (
              <li key={key}>
                <Link to={stepHref(step, { agent_id: agentId })} onClick={closeGlossary} data-step={key}>
                  <span className="mono">{t(`story.chapter.${step.chapter}`)}</span>
                  {t(step.labelKey)}
                </Link>
              </li>
            );
          })}
        </ol>
      </div>
      <div className="v3-dlc-term-foot">
        <span><span className="v3-dlc-term-tag">{t("story.term.owner")}</span> {t(`story.term.${term}.owner`)}</span>
        <span className="v3-dlc-term-related">
          {card.related.map((r) => (
            <button key={r} type="button" onClick={() => openGlossary(r)}>{t(`story.term.${r}.name`)}</button>
          ))}
        </span>
      </div>
    </article>
  );
}
