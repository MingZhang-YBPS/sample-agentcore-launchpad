import { ArrowRight, ChevronDown, Layers, ListChecks, Store } from "lucide-react";
import { type KeyboardEvent, type ReactNode, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";

import type { GlossaryTerm } from "../../../v2/Glossary";
import { Term } from "../../onboarding/Glossary";
import { Lamp, type Signal } from "../../ui";
import {
  type Answers,
  CAP_BASE_ROWS,
  CAP_BUILDER_ROWS,
  type CapColumn,
  CAP_COLUMNS,
  CAPABILITIES,
  type CapLevel,
  type CapRow,
  capColumn,
  DLC,
  HAVE,
  type Question,
  QUESTIONS,
  type Recommendation,
  recommend,
  relevant,
  USE,
  type WayKey,
  wayHref,
} from "./chooser";
import "./chooser.css";

const P = "v3.create.chooser";
const OPTIONS: Record<Question, readonly string[]> = { have: HAVE, use: USE, dlc: DLC };
/** The glossary term a way's name is explained by, where one exists. */
const WAY_TERM: Partial<Record<WayKey, GlossaryTerm>> = { harness: "harness", mcp: "mcp", import: "runtime" };
/** The ways whose wizard takes a preselected knowledge base. */
const KB_WAYS: WayKey[] = ["harness", "strands", "sdk"];
const LEVEL_SIGNAL: Record<CapLevel, Signal> = { yes: "ok", partial: "wait", no: "act", info: "off" };

/** Arrow keys move between a radio group's options, as the scenario gallery does. */
function radioKeys(e: KeyboardEvent<HTMLDivElement>) {
  if (!["ArrowRight", "ArrowDown", "ArrowLeft", "ArrowUp", "Home", "End"].includes(e.key)) return;
  const options = [...e.currentTarget.querySelectorAll<HTMLButtonElement>(':scope > [role="radio"]')];
  const index = options.indexOf(e.target as HTMLButtonElement);
  if (index < 0) return;
  e.preventDefault();
  const next = e.key === "Home" ? 0 : e.key === "End" ? options.length - 1 :
    (index + (e.key === "ArrowRight" || e.key === "ArrowDown" ? 1 : -1) + options.length) % options.length;
  options[next].focus();
  options[next].click();
}

function WayName({ way }: { way: WayKey }) {
  const { t } = useTranslation();
  const term = WAY_TERM[way];
  return term ? <Term term={term}>{t(`${P}.way.${way}`)}</Term> : <>{t(`${P}.way.${way}`)}</>;
}

/** The answer the recommendation is waiting on, or the readout itself. */
function Readout({ rec, answers }: { rec: Recommendation | null; answers: Answers }) {
  const { t } = useTranslation();
  if (!rec) {
    return (
      <div className="v3-chooser-readout pending" aria-live="polite">
        <div className="tag">{t(`${P}.pending`)}</div>
        <div className="lamps" aria-hidden="true">
          {QUESTIONS.map((q) => <Lamp key={q} s={answers[q] !== undefined ? "ok" : "off"} />)}
        </div>
        <p>{t(`${P}.pendingSub`)}</p>
      </div>
    );
  }
  return (
    // keyed on the result so a new recommendation replays its entrance
    <div key={`${rec.row}`} className="v3-chooser-readout" data-signal="ok" aria-live="polite">
      <div className="tag">{t(`${P}.recommended`)}</div>
      <h2 className="way"><WayName way={rec.primary} /></h2>
      <dl>
        <dt>{t(`${P}.why`)}</dt>
        <dd>{t(`${P}.row.${rec.row}.why`)}</dd>
        <dt>{t(`${P}.cost`)}</dt>
        <dd>{t(`${P}.row.${rec.row}.cost`)}</dd>
      </dl>
      {rec.defaults.kb && KB_WAYS.includes(rec.primary) && <p className="carry"><Layers size={13} aria-hidden="true" /> {t(`${P}.kbPreset`)}</p>}
      <Link to={wayHref(rec.primary, rec.defaults)} className="v3-btn primary go">
        {t(`${P}.useThis`, { way: t(`${P}.way.${rec.primary}`) })} <ArrowRight size={14} aria-hidden="true" />
      </Link>
      {rec.alternative && (
        <Link to={wayHref(rec.alternative, rec.defaults)} className="alt">
          <span className="k">{t(`${P}.alternative`)}</span>
          <span>
            <b>{t(`${P}.way.${rec.alternative}`)}</b> — {t(`${P}.row.${rec.row}.alt`)}
          </span>
          <ArrowRight size={13} aria-hidden="true" className="arrow" />
        </Link>
      )}
    </div>
  );
}

/** Workshop 20-build §0 as a table, the recommended method's column lit. */
function Comparison({ highlight }: { highlight: WayKey | null }) {
  const { t } = useTranslation();
  const col = highlight ? capColumn(highlight) : null;
  return (
    <details className="v3-chooser-compare">
      <summary>
        <ListChecks size={14} aria-hidden="true" />
        <span>{t(`${P}.compare`)}</span>
        <small>{t(`${P}.compareSub`)}</small>
        <ChevronDown size={14} aria-hidden="true" className="chev" />
      </summary>
      {highlight && !col && <p className="none">{t(`${P}.compareNone`, { way: t(`${P}.way.${highlight}`) })}</p>}
      <div className="v3-table-scroll" tabIndex={0} role="region" aria-label={t(`${P}.compare`)}>
        <table className="v3-table">
          <thead>
            <tr>
              <th scope="col">{t(`${P}.capHead`)}</th>
              {CAP_COLUMNS.map((c) => (
                <th key={c} scope="col" className={c === col ? "lit" : undefined} aria-current={c === col ? "true" : undefined}>
                  {t(`${P}.col.${c}`)}
                  {c === col && <span className="pin">{t(`${P}.recommended`)}</span>}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {CAP_BASE_ROWS.map((r) => <CapabilityRow key={r} row={r} col={col} />)}
          </tbody>
          {/* the Strands builder options: a section of its own under a ruled caption row */}
          <tbody className="builder">
            <tr className="section">
              <th scope="colgroup" colSpan={CAP_COLUMNS.length + 1}>
                <span className="tag">{t(`${P}.capBuilder`)}</span>
                <span className="sub">{t(`${P}.capBuilderSub`)}</span>
              </th>
            </tr>
            {CAP_BUILDER_ROWS.map((r) => <CapabilityRow key={r} row={r} col={col} />)}
          </tbody>
        </table>
      </div>
    </details>
  );
}

function CapabilityRow({ row, col }: { row: CapRow; col: CapColumn | null }) {
  const { t } = useTranslation();
  return (
    <tr>
      <th scope="row">{t(`${P}.cap.${row}`)}</th>
      {CAP_COLUMNS.map((c) => {
        const level = CAPABILITIES[row][c];
        return (
          <td key={c} className={c === col ? "lit" : undefined} data-level={level}>
            <span className="cell">
              {level !== "info" && <Lamp s={LEVEL_SIGNAL[level]} title={t(`${P}.level.${level}`)} />}
              <span>{t(`${P}.cell.${row}.${c}`)}</span>
            </span>
          </td>
        );
      })}
    </tr>
  );
}

function ReadyCard({ to, icon, title, desc }: { to: string; icon: ReactNode; title: string; desc: string }) {
  return (
    <Link to={to} className="v3-way">
      <span className="icon" aria-hidden="true">{icon}</span>
      <span className="text">
        <b>{title}</b>
        <small>{desc}</small>
      </span>
      <ArrowRight size={14} className="go" aria-hidden="true" />
    </Link>
  );
}

/**
 * The new-agent chooser (storyline-ux §3.6): three questions on the left, the
 * recommendation reading out on the right as soon as it is decided, the
 * capability comparison under it, and the ready-made starts below. `onSkip`
 * shows every way instead (the page remembers it).
 */
export function Chooser({ onSkip, initial = {} }: { onSkip: () => void; initial?: Answers }) {
  const { t } = useTranslation();
  const [answers, setAnswers] = useState<Answers>(initial);
  const rec = recommend(answers);
  const live = relevant(answers);
  const set = (q: Question, v: string) =>
    setAnswers((prev) => ({ ...prev, [q]: v, ...(q === "have" && v !== "code" ? { sdk: undefined } : {}) }));

  return (
    <div className="v3-chooser">
      <section className="v3-panel v3-chooser-main" aria-labelledby="v3-chooser-title">
        <div className="v3-chooser-head">
          <div>
            <h2 id="v3-chooser-title">{t(`${P}.title`)}</h2>
            <p>{t(`${P}.sub`)}</p>
          </div>
          <button type="button" className="v3-btn ghost sm" onClick={onSkip}>{t(`${P}.skip`)}</button>
        </div>
        <div className="v3-chooser-body">
          <ol className="v3-chooser-qs">
            {QUESTIONS.map((q, i) => {
              const muted = !live.has(q) && answers.have !== undefined;
              return (
                <li key={q} className={muted ? "q muted" : "q"}>
                  <div className="q-head">
                    <span className="n" aria-hidden="true">{String(i + 1).padStart(2, "0")}</span>
                    <span id={`v3-chooser-${q}`} className="q-text">{t(`${P}.q.${q}`)}</span>
                    {muted && <span className="q-note">{t(`${P}.notNeeded`)}</span>}
                  </div>
                  <div className="opts" role="radiogroup" aria-labelledby={`v3-chooser-${q}`} onKeyDown={radioKeys}>
                    {OPTIONS[q].map((v, j) => {
                      const on = answers[q] === v;
                      const focusable = on || (answers[q] === undefined && j === 0);
                      return (
                        <button key={v} type="button" role="radio" aria-checked={on} tabIndex={focusable ? 0 : -1}
                          className={on ? "opt on" : "opt"} onClick={() => set(q, v)}>
                          <b>{t(`${P}.${q}.${v}`)}</b>
                          <small>{t(`${P}.${q}.${v}Sub`)}</small>
                        </button>
                      );
                    })}
                  </div>
                  {q === "have" && answers.have === "code" && (
                    <label className="v3-chooser-sdk">
                      <input type="checkbox" checked={!!answers.sdk} onChange={(e) => setAnswers((prev) => ({ ...prev, sdk: e.target.checked }))} />
                      <span>
                        <b>{t(`${P}.sdk`)}</b>
                        <small>{t(`${P}.sdkSub`)}</small>
                      </span>
                    </label>
                  )}
                </li>
              );
            })}
          </ol>
          <div className="v3-chooser-side">
            <Readout rec={rec} answers={answers} />
          </div>
        </div>
        <Comparison highlight={rec?.primary ?? null} />
      </section>

      <section className="v3-panel" aria-labelledby="v3-chooser-ready">
        <div className="v3-panel-head" id="v3-chooser-ready">{t(`${P}.ready`)}</div>
        <p className="v3-chooser-ready-sub">{t(`${P}.readySub`)}</p>
        <div className="v3-grid c2">
          <ReadyCard to="/agents/new" icon={<Layers size={16} />} title={t(`${P}.presets`)} desc={t(`${P}.presetsSub`)} />
          <ReadyCard to="/v3/fleet?view=market" icon={<Store size={16} />} title={t(`${P}.market`)} desc={t(`${P}.marketSub`)} />
        </div>
      </section>
    </div>
  );
}
