import { useState } from "react";
import { useTranslation } from "react-i18next";

import { ApiError, apiErrorDetailText, apiErrorFacts, errorMessage } from "../lib/api";

interface ApiErrorDetailProps {
  /** What the catch branch got (or a validation string the page wrote itself). */
  error: unknown;
  "data-testid"?: string;
}

/**
 * Body of an error notice: the console headline, then a collapsible "AWS
 * details" with what the backend said — operation, AWS error code, AWS's own
 * text, request ids, time — and one button that copies it all (projects §19).
 *
 * Render it inside the page's red notice. A string or a non-API error renders as
 * plain text; an `ApiError` without AWS facts or a request id renders its
 * message only, so the component can replace `{error}` anywhere.
 */
export function ApiErrorDetail({ error, "data-testid": testId = "api-error" }: ApiErrorDetailProps) {
  const { t } = useTranslation();
  const [copied, setCopied] = useState(false);
  if (error == null || error === "") return null;
  if (!(error instanceof ApiError)) {
    return <span data-testid={testId}>{typeof error === "string" ? error : errorMessage(error)}</span>;
  }
  const facts = apiErrorFacts(error);
  if (!facts) return <span data-testid={testId}>{error.message}</span>;

  const copy = () => {
    void navigator.clipboard?.writeText(apiErrorDetailText(error)).then(() => {
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    });
  };
  const rows: [string, string | null][] = [
    [t("apiErrors.detail.operation"), facts.operation],
    [t("apiErrors.detail.awsErrorCode"), facts.awsErrorCode],
    [t("apiErrors.detail.requestId"), error.requestId],
    [t("apiErrors.detail.awsRequestId"), facts.awsRequestId],
    [t("apiErrors.detail.time"), error.at.toISOString()],
  ];
  return (
    <div data-testid={testId} style={{ display: "grid", gap: 6 }}>
      <span>{error.title}</span>
      <details data-testid={`${testId}-detail`} style={{ fontSize: 12 }}>
        <summary style={{ cursor: "pointer" }}>
          {t(facts.awsErrorCode ? "apiErrors.detail.summaryAws" : "apiErrors.detail.summary")}
          {error.requestId && <span className="mono" style={{ marginLeft: 8, opacity: 0.75 }}>{error.requestId}</span>}
        </summary>
        <dl style={{ display: "grid", gridTemplateColumns: "max-content 1fr", gap: "2px 12px", margin: "8px 0" }}>
          {rows.filter(([, v]) => v).map(([label, value]) => (
            <div key={label} style={{ display: "contents" }}>
              <dt style={{ opacity: 0.75 }}>{label}</dt>
              <dd className="mono" style={{ margin: 0, wordBreak: "break-all" }}>{value}</dd>
            </div>
          ))}
        </dl>
        {error.rawMessage && (
          <>
            <div style={{ opacity: 0.75 }}>{t("apiErrors.detail.message")}</div>
            <pre data-testid={`${testId}-raw`} className="mono" style={{ margin: "4px 0 8px", whiteSpace: "pre-wrap", wordBreak: "break-word" }}>
              {error.rawMessage}
            </pre>
          </>
        )}
        {facts.masked && <div data-testid={`${testId}-masked`} style={{ marginBottom: 8, opacity: 0.85 }}>{t("apiErrors.detail.masked")}</div>}
        <button type="button" className="v3-btn ghost sm" data-testid={`${testId}-copy`} onClick={copy}>
          {copied ? t("apiErrors.detail.copied") : t("apiErrors.detail.copy")}
        </button>
      </details>
    </div>
  );
}
