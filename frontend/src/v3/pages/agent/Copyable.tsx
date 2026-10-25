import { Check, Copy, X } from "lucide-react";
import { type ReactNode, useState } from "react";
import { useTranslation } from "react-i18next";

import { useToast } from "../../hooks";

/** A value with a one-click copy button (ids, ARNs, resource names). */
export function Copyable({ value, label, children }: { value: string; label: string; children?: ReactNode }) {
  const { t } = useTranslation();
  const toast = useToast();
  // a failed copy says so on the button itself, where the user is looking
  const [copied, setCopied] = useState<"ok" | "failed" | null>(null);
  const copy = () => {
    navigator.clipboard
      .writeText(value)
      .then(() => {
        setCopied("ok");
        toast("ok", t("v3.agent.basic.copied", { label }));
        window.setTimeout(() => setCopied(null), 1500);
      })
      .catch(() => {
        setCopied("failed");
        window.setTimeout(() => setCopied(null), 2400);
      });
  };
  return (
    <span className="v3-agent-copy">
      <span className="mono">{children ?? value}</span>
      <button type="button" onClick={copy} aria-label={t("v3.agent.basic.copy", { label })}
        title={copied === "failed" ? t("v3.agent.basic.copyFailed") : t("v3.agent.basic.copy", { label })}
        data-copy={copied ?? undefined}>
        {copied === "ok" ? <Check size={13} /> : copied === "failed" ? <X size={13} /> : <Copy size={13} />}
      </button>
      {copied === "failed" && <span className="v3-agent-copy-failed" role="status">{t("v3.agent.basic.copyFailed")}</span>}
    </span>
  );
}
