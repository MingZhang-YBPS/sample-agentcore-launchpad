// Glossary hints (T03): one plain-language sentence per platform term, shown on
// hover AND keyboard focus. Entries are i18n keys `glossary.<term>` (en + zh-CN).
import "./glossary.css";

import { Info } from "lucide-react";
import { type ReactNode, useId } from "react";
import { useTranslation } from "react-i18next";

export const GLOSSARY_TERMS = [
  "agent",
  "harness",
  "runtime",
  "registry",
  "gateway",
  "mcp",
  "cedarPolicy",
  "knowledgeBase",
  "memory",
  "evaluator",
  "onlineEvaluation",
  "dataset",
  "experiment",
  "canary",
  "skill",
  "a2a",
  "workspace",
  "observability",
  "trace",
] as const;

export type GlossaryTerm = (typeof GLOSSARY_TERMS)[number];

/** The i18n key of a term's one-sentence explanation. */
const glossaryKey = (term: GlossaryTerm) => `glossary.${term}`;

/**
 * A small focusable (i) after a label. Hover or Tab onto it shows the term's
 * explanation in a `role="tooltip"` bubble that the icon references through
 * `aria-describedby`, so screen readers announce it too.
 */
export function HintIcon({ term, label }: { term: GlossaryTerm; label?: string }) {
  const { t } = useTranslation();
  const id = useId();
  return (
    <span
      className="v2-hint"
      tabIndex={0}
      aria-label={t("glossary.whatIs", { term: label ?? t(`glossary.name.${term}`) })}
      aria-describedby={id}
      data-testid={`v2-hint-${term}`}
    >
      <Info size={13} aria-hidden="true" />
      <span role="tooltip" id={id} className="v2-hint-tip">
        {t(glossaryKey(term))}
      </span>
    </span>
  );
}

/** Inline term with a dotted underline; the explanation shows on hover/focus. */
export function Term({ term, children }: { term: GlossaryTerm; children: ReactNode }) {
  const { t } = useTranslation();
  const id = useId();
  return (
    <span className="v2-hint v2-term" tabIndex={0} aria-describedby={id}>
      {children}
      <span role="tooltip" id={id} className="v2-hint-tip">
        {t(glossaryKey(term))}
      </span>
    </span>
  );
}

/** A label followed by its HintIcon — the common form for `Field` labels. */
export function HintLabel({ term, children }: { term: GlossaryTerm; children: ReactNode }) {
  return (
    <>
      {children}
      <HintIcon term={term} label={typeof children === "string" ? children : undefined} />
    </>
  );
}
