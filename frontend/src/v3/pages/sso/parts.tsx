import { Check, Copy } from "lucide-react";
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

/** A provider's monogram plate: its preset's initials, keyed by the preset's tone. */
export function PresetMark({ preset, size = "md" }: { preset: string; size?: "md" | "lg" }) {
  const { t } = useTranslation();
  return (
    <span className="v3-sso-mark" data-preset={preset} data-size={size} aria-hidden="true">
      {t(`auth.sso.presetShort.${preset}`, { defaultValue: "ID" })}
    </span>
  );
}

/** A value to paste on the IdP side, with a one-click copy. */
export function CopyPlate({ label, value, testId }: { label: string; value: string; testId?: string }) {
  const { t } = useTranslation();
  const [copied, setCopied] = useState(false);
  useEffect(() => {
    if (!copied) return;
    const timer = window.setTimeout(() => setCopied(false), 1600);
    return () => window.clearTimeout(timer);
  }, [copied]);
  return (
    <div className="v3-sso-plate" data-testid={testId}>
      <span className="lbl">{label}</span>
      <code>{value}</code>
      <button
        type="button"
        className="v3-btn ghost sm"
        onClick={() => {
          void navigator.clipboard?.writeText(value);
          setCopied(true);
        }}
        aria-label={t("v3.sso.copy")}
        title={t("v3.sso.copy")}
      >
        {copied ? <Check size={13} aria-hidden="true" /> : <Copy size={13} aria-hidden="true" />}
        <span className="sr-only">{t("v3.sso.copy")}</span>
      </button>
    </div>
  );
}
