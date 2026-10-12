// Pieces the observability views share: ids you can copy, a trace status chip,
// and the polarity-aware score cell. Formatting comes from the V2 helpers (pure).
import { Copy } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";

import { fmtScore, normalizedScore, scoreTone } from "../../../v2/format";
import { copyText, shortId, TIMEOUT_MS } from "../../../v2/pages/observability/common";
import { useToast } from "../../hooks";
import { Chip } from "../../ui";

export function CopyId({ id, chars = 16 }: { id: string; chars?: number }) {
  const { t } = useTranslation();
  const toast = useToast();
  return (
    <span className="v3-obs-id" title={id}>
      {shortId(id, chars)}
      <button
        type="button"
        aria-label={t("v3.obs.copyId")}
        onClick={(e) => {
          e.stopPropagation();
          copyText(id).then(
            () => toast("ok", t("v3.obs.copied")),
            () => toast("act", t("v3.obs.copyFailed")),
          );
        }}
      >
        <Copy size={12} aria-hidden="true" />
      </button>
    </span>
  );
}

/** ok · error · timeout — a root span past the timeout reads as a timeout. */
export function TraceStatus({ status, durationMs }: { status: "ok" | "error"; durationMs: number }) {
  const { t } = useTranslation();
  if (status === "ok") return <Chip s="ok">{t("v3.obs.status.ok")}</Chip>;
  return <Chip s="act">{durationMs > TIMEOUT_MS ? t("v3.obs.status.timeout") : t("v3.obs.status.error")}</Chip>;
}

export function Score({ value, evaluatorId }: { value: number | null; evaluatorId: string }) {
  if (value == null) return <span style={{ color: "var(--v3-text-3)" }}>—</span>;
  const tone = scoreTone(normalizedScore(value, evaluatorId));
  return (
    <span className="v3-obs-score mono" data-s={tone === "good" ? "ok" : tone === "mid" ? "wait" : "act"}>
      {fmtScore(value)}
    </span>
  );
}

/** Two lines until clicked. */
export function Explanation({ text }: { text: string | null }) {
  const [open, setOpen] = useState(false);
  if (!text) return <span style={{ color: "var(--v3-text-3)" }}>—</span>;
  return (
    <button type="button" className={open ? "v3-obs-expl open" : "v3-obs-expl"} onClick={() => setOpen((v) => !v)}>
      {text}
    </button>
  );
}
