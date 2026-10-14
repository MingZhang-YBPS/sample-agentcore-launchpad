/**
 * A task's id with a copy button. The run id is what the calibration dialog asks
 * for; it used to be readable only from the address bar, so people pasted the
 * run's name instead.
 */
import { Copy } from "lucide-react";
import type { CSSProperties } from "react";
import { useTranslation } from "react-i18next";

import type { TaskKind } from "../../../v2/tasks";
import { useToast } from "../../hooks";

function copyIdLabels(kind: TaskKind) {
  return kind === "run"
    ? { label: "v3.tasks.runId", copied: "v3.tasks.copied" }
    : { label: "v3.tasks.taskId", copied: "v3.tasks.copiedTaskId" };
}

export function CopyId({ id, kind, labelled = false, quiet = false, style }: {
  id: string;
  kind: TaskKind;
  /** keep the button's room but fade it out (a list row shows it on hover or focus) */
  quiet?: boolean;
  /** prefix the id with "Run ID" / "Task ID" (the detail header; the list row is compact) */
  labelled?: boolean;
  style?: CSSProperties;
}) {
  const { t } = useTranslation();
  const toast = useToast();
  const keys = copyIdLabels(kind);
  return (
    <span style={{ display: "inline-flex", alignItems: "center", gap: 6, ...style }}>
      {labelled && <span>{t(keys.label)}</span>}
      <code className="mono">{id}</code>
      <button
        type="button"
        className="v3-btn ghost sm"
        style={{ padding: "0 4px", minHeight: 0, lineHeight: 1, opacity: quiet ? 0 : 1 }}
        title={t("v3.tasks.copyId")}
        aria-label={t("v3.tasks.copyId")}
        onClick={(e) => {
          // a list row navigates on click; copying must not open the task
          e.stopPropagation();
          void navigator.clipboard?.writeText(id);
          toast("ok", t(keys.copied));
        }}
      >
        <Copy size={12} />
      </button>
    </span>
  );
}
