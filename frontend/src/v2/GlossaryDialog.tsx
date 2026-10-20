// The searchable V2 glossary dialog the shell mounts once; opened through
// `openGlossary` (./glossaryTerms) from the help menu and the command palette.
// The term hints (HintIcon / Term / HintLabel) stay in ./Glossary.
import { BookOpen } from "lucide-react";
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import { closeGlossary, GLOSSARY_TERMS, glossaryMatches, useGlossary } from "./glossaryTerms";
import { Modal, SearchInput } from "./ui";

/** The glossary dialog, mounted once by the shell; opened through `openGlossary`. */
export function GlossaryDialog() {
  const { t, i18n } = useTranslation();
  const { open, term: focus } = useGlossary();
  const [q, setQ] = useState("");
  const findRef = useRef<HTMLDivElement>(null);
  const listRef = useRef<HTMLDListElement>(null);
  const returnTo = useRef<HTMLElement | null>(null);
  const tEn = useMemo(() => i18n.getFixedT("en"), [i18n]);

  const rows = GLOSSARY_TERMS.filter((term) =>
    glossaryMatches(term, q, t(`glossary.name.${term}`), tEn(`glossary.name.${term}`), t(`glossary.${term}`)),
  );

  // Remember what had focus, move it into the search box (a layout effect, so
  // the first typed key is not lost), and give it back on close.
  useLayoutEffect(() => {
    if (!open) return;
    returnTo.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    findRef.current?.querySelector("input")?.focus();
    return () => {
      const el = returnTo.current;
      returnTo.current = null;
      if (el?.isConnected) el.focus();
    };
  }, [open]);
  // land on the requested row
  useEffect(() => {
    if (open && focus) listRef.current?.querySelector(`[data-term="${focus}"]`)?.scrollIntoView({ block: "nearest" });
  }, [open, focus]);

  const close = useCallback(() => {
    setQ("");
    closeGlossary();
  }, []);

  return (
    <Modal
      open={open}
      onClose={close}
      wide
      testId="v2-glossary"
      title={
        <span className="v2-glossary-title">
          <BookOpen size={16} aria-hidden="true" />
          {t("v2.glossary.title")}
        </span>
      }
    >
      <div className="v2-glossary-find" ref={findRef}>
        <SearchInput value={q} onChange={setQ} placeholder={t("v2.glossary.search")} testId="v2-glossary-search" />
      </div>
      <dl className="v2-glossary" ref={listRef}>
        {rows.map((term) => (
          <div key={term} data-term={term} className={term === focus ? "on" : undefined}>
            <dt>{t(`glossary.name.${term}`)}</dt>
            <dd>{t(`glossary.${term}`)}</dd>
          </div>
        ))}
      </dl>
      {rows.length === 0 && <p className="v2-muted">{t("v2.glossary.none")}</p>}
    </Modal>
  );
}
