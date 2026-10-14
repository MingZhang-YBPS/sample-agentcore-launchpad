import { useTranslation } from "react-i18next";

/** The top bar's language toggle: one button that names the other language. */
export function Lang() {
  const { i18n } = useTranslation();
  const zh = (i18n.resolvedLanguage ?? "en").startsWith("zh");
  return (
    <button
      type="button"
      className="v3-btn ghost sm mono"
      onClick={() => void i18n.changeLanguage(zh ? "en" : "zh-CN")}
      aria-label={zh ? "English" : "中文"}
    >
      {zh ? "EN" : "中"}
    </button>
  );
}
