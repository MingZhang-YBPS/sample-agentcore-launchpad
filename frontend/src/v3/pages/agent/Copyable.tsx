import { Check, Copy } from "lucide-react";
import { type ReactNode, useState } from "react";
import { useTranslation } from "react-i18next";

import { useToast } from "../../hooks";

/** A value with a one-click copy button (ids, ARNs, resource names). */
export function Copyable({ value, label, children }: { value: string; label: string; children?: ReactNode }) {
  const { t } = useTranslation();
  const toast = useToast();
  const [copied, setCopied] = useState(false);
  const copy = () => {
    navigator.clipboard
      .writeText(value)
      .then(() => {
        setCopied(true);
        toast("ok", t("v3.agent.basic.copied", { label }));
        window.setTimeout(() => setCopied(false), 1500);
      })
      .catch(() => toast("act", t("v3.agent.basic.copyFailed")));
  };
  return (
    <span className="v3-agent-copy">
      <span className="mono">{children ?? value}</span>
      <button type="button" onClick={copy} aria-label={t("v3.agent.basic.copy", { label })} title={t("v3.agent.basic.copy", { label })}>
        {copied ? <Check size={13} /> : <Copy size={13} />}
      </button>
    </span>
  );
}
