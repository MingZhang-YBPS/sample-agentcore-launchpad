import "./structuredAnswer.css";

import { Braces, Check, Copy, Table2 } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";

import { scalarText, structuredView } from "../../lib/structured-answer";

/**
 * The structured answer under an agent's text answer (SB-1, strands-builder §2.4): a
 * flat object as a key → value table, anything else as JSON, with a switch to the
 * raw JSON and a copy. Skin-neutral: `structuredAnswer.css` maps it onto the V2 or
 * V3 tokens of the page that hosts it.
 */
export function StructuredAnswer({ value }: { value: unknown }) {
  const { t } = useTranslation();
  const view = structuredView(value);
  const [raw, setRaw] = useState(false);
  const [copied, setCopied] = useState(false);
  if (!view) return null;
  const table = view.kind === "table" && !raw;
  const copy = () =>
    void navigator.clipboard?.writeText(view.json).then(() => {
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1400);
    }, () => undefined);
  return (
    <figure className="lp-structured" data-view={table ? "table" : "json"} data-testid="chat-structured">
      <figcaption className="lp-structured-head">
        <span className="lp-structured-label">{t("strandsBuilder.structured.label")}</span>
        {view.kind === "table" && (
          <span className="lp-structured-count">{t("strandsBuilder.structured.fields", { count: view.rows.length })}</span>
        )}
        <span className="lp-structured-tools">
          {view.kind === "table" && (
            <button type="button" onClick={() => setRaw((r) => !r)} aria-pressed={raw}
              title={t(raw ? "strandsBuilder.structured.asTable" : "strandsBuilder.structured.asJson")}>
              {raw ? <Table2 size={12} aria-hidden="true" /> : <Braces size={12} aria-hidden="true" />}
              {t(raw ? "strandsBuilder.structured.asTable" : "strandsBuilder.structured.asJson")}
            </button>
          )}
          <button type="button" onClick={copy} title={t("strandsBuilder.structured.copy")}>
            {copied ? <Check size={12} aria-hidden="true" /> : <Copy size={12} aria-hidden="true" />}
            {t(copied ? "strandsBuilder.structured.copied" : "strandsBuilder.structured.copy")}
          </button>
        </span>
      </figcaption>
      {table && view.kind === "table" ? (
        <table className="lp-structured-table">
          <tbody>
            {view.rows.map((row) => (
              <tr key={row.key}>
                <th scope="row">{row.key}</th>
                <td data-type={row.value === null ? "null" : typeof row.value}>{scalarText(row.value)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        <pre className="lp-structured-json">{view.json}</pre>
      )}
    </figure>
  );
}
