import { BookOpen, Search } from "lucide-react";
import { type ReactNode, useId, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";

import { GLOSSARY_TERMS, type GlossaryTerm } from "../../v2/Glossary";
import { Btn, Dialog } from "../ui";
import { closeGlossary, openGlossary, useGlossaryOpen } from "./state";

/**
 * A platform term inline: dotted underline, its one-sentence meaning on hover and
 * keyboard focus (announced through aria-describedby), and a click that opens the
 * full glossary on that term. The sentences are V2's (`glossary.<term>`), so both
 * consoles explain a word the same way.
 */
export function Term({ term, children }: { term: GlossaryTerm; children?: ReactNode }) {
  const { t } = useTranslation();
  const id = useId();
  return (
    <span className="v3-term">
      <button type="button" className="v3-term-word" aria-describedby={id} onClick={() => openGlossary(term)}>
        {children ?? t(`glossary.name.${term}`)}
      </button>
      <span role="tooltip" id={id} className="v3-term-tip">
        {t(`glossary.${term}`)}
      </span>
    </span>
  );
}

/** The glossary dialog, mounted once by the shell; opened by Term, ⌘K or the help menu. */
export function GlossaryDialog() {
  const { t } = useTranslation();
  const open = useGlossaryOpen();
  const [q, setQ] = useState("");
  const rows = useMemo(() => {
    const needle = q.trim().toLowerCase();
    return GLOSSARY_TERMS.filter(
      (term) =>
        !needle ||
        `${term} ${t(`glossary.name.${term}`)} ${t(`glossary.name.${term}`, { lng: "en" })} ${t(`glossary.${term}`)}`
          .toLowerCase()
          .includes(needle),
    );
  }, [q, t]);
  if (!open) return null;
  const focus = open !== "all" ? (open as GlossaryTerm) : null;
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
      <dl className="v3-glossary">
        {rows.map((term) => (
          <div key={term} className={term === focus ? "on" : undefined} ref={(el) => term === focus && el?.scrollIntoView({ block: "nearest" })}>
            <dt>{t(`glossary.name.${term}`)}</dt>
            <dd>{t(`glossary.${term}`)}</dd>
          </div>
        ))}
        {rows.length === 0 && <p style={{ color: "var(--v3-text-3)" }}>{t("v3.onboard.glossary.none")}</p>}
      </dl>
    </Dialog>
  );
}
