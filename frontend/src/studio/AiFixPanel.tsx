// Presentational AI Fix UI (progress log, applied notice, diagnosis card),
// used by ChatDrawer. Launchpad-styled; behavior ported
// from strands_studio_ui `src/components/ai-fix-progress.tsx` (origin/main).
import { useTranslation } from "react-i18next";
import { AlertTriangle, Loader, Sparkles, Wrench, XCircle } from "lucide-react";

import { Chip } from "../components";
import type { FixProgressEvent } from "./useAiFix";
import type { FixDiagnosis, FixSuggestion } from "./lib/debug-client";
import { useStickToBottom } from "../lib/useStickToBottom";

interface AiFixPanelProps {
  isFixing: boolean;
  fixEvents: FixProgressEvent[];
  fixError: string | null;
  fixDiagnosis: FixDiagnosis | null;
  fixApplied: boolean;
  appliedMessage?: string;
  onDismissError: () => void;
  onDismissDiagnosis: () => void;
}

function suggestionText(
  s: FixSuggestion,
  t: ReturnType<typeof useTranslation>["t"],
): string {
  if (s.node_label && s.property) {
    return t("studio.fix.suggNodeProp", {
      label: s.node_label,
      property: s.property,
      action: s.action,
    });
  }
  if (s.node_label) {
    return t("studio.fix.suggNode", { label: s.node_label, action: s.action });
  }
  return s.action;
}

export function AiFixPanel({
  isFixing,
  fixEvents,
  fixError,
  fixDiagnosis,
  fixApplied,
  appliedMessage,
  onDismissError,
  onDismissDiagnosis,
}: AiFixPanelProps) {
  const { t } = useTranslation();
  // follows new events only while the reader is at the bottom
  const events = useStickToBottom();

  return (
    <>
      {(isFixing || fixError) && (
        <div className="studio-fix">
          <div className="studio-fix-head">
            {isFixing ? (
              <Loader size={13} className="studio-spin" style={{ color: "var(--amber)" }} />
            ) : (
              <AlertTriangle size={13} style={{ color: "var(--crit-text)" }} />
            )}
            <span>{isFixing ? t("studio.fix.fixing") : t("studio.fix.failed")}</span>
            {!isFixing && fixError && (
              <button className="studio-fix-dismiss" onClick={onDismissError}>
                {t("studio.fix.dismiss")}
              </button>
            )}
          </div>
          <div className="studio-fix-events" ref={events.ref}>
            {fixEvents.map((event) => (
              <div key={event.id} className={`studio-fix-ev k-${event.kind}`}>
                {event.text}
              </div>
            ))}
          </div>
        </div>
      )}

      {fixApplied && (
        <div className="studio-fix-applied">
          <Sparkles size={14} style={{ color: "var(--good)", flex: "none", marginTop: 1 }} />
          <span>{appliedMessage ?? t("studio.fix.applied")}</span>
        </div>
      )}

      {fixDiagnosis && (
        <div className="studio-fix-diag">
          <div className="studio-fix-diag-head">
            <span className="studio-fix-diag-lbl">{t("studio.fix.diagnosis")}</span>
            {fixDiagnosis.category === "code" ? (
              <Chip tone="blue" icon={<Wrench size={11} />}>
                {t("studio.fix.catCode")}
              </Chip>
            ) : fixDiagnosis.category === "config" ? (
              <Chip tone="warn" icon={<AlertTriangle size={11} />}>
                {t("studio.fix.catConfig")}
              </Chip>
            ) : (
              <Chip tone="crit" icon={<XCircle size={11} />}>
                {t("studio.fix.catEnv")}
              </Chip>
            )}
            <button
              className="studio-fix-dismiss"
              style={{ marginLeft: "auto" }}
              onClick={onDismissDiagnosis}
              title={t("studio.fix.dismiss")}
            >
              ✕
            </button>
          </div>
          <p className="studio-fix-diag-sum">{fixDiagnosis.summary}</p>
          {fixDiagnosis.suggestions.length > 0 && (
            <ul className="studio-fix-diag-list">
              {fixDiagnosis.suggestions.map((s, i) => (
                <li key={i}>{suggestionText(s, t)}</li>
              ))}
            </ul>
          )}
        </div>
      )}
    </>
  );
}
