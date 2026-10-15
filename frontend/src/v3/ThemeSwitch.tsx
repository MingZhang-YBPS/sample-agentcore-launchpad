import { Monitor, Moon, Sun } from "lucide-react";
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import { THEME_CHOICES, type ThemeChoice, useTheme } from "./theme";

const THEME_ICON: Record<ThemeChoice, typeof Sun> = { light: Sun, dark: Moon, system: Monitor };

/** Light / Dark / System in the top bar; each option previews its palette as a swatch. */
export function ThemeSwitch() {
  const { t } = useTranslation();
  const { choice, theme, setChoice } = useTheme();
  const [open, setOpen] = useState(false);
  useEffect(() => {
    if (!open) return;
    const close = () => setOpen(false);
    const onKey = (e: globalThis.KeyboardEvent) => e.key === "Escape" && setOpen(false);
    window.addEventListener("click", close);
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("click", close);
      window.removeEventListener("keydown", onKey);
    };
  }, [open]);
  const Icon = THEME_ICON[choice];
  const current = t("v3.theme.current", { theme: t(`v3.theme.${choice}`) });
  return (
    <div className="v3-help v3-theme" onClick={(e) => e.stopPropagation()}>
      <button type="button" className="v3-btn ghost sm" aria-haspopup="menu" aria-expanded={open}
        aria-label={current} title={current} data-testid="v3-theme-switch" onClick={() => setOpen((v) => !v)}>
        <Icon size={15} aria-hidden="true" />
      </button>
      {open && (
        <div className="v3-help-menu v3-theme-menu" role="menu" aria-label={t("v3.theme.menu")}>
          <div className="v3-theme-menu-head">{t("v3.theme.label")}</div>
          {THEME_CHOICES.map((c) => {
            const ItemIcon = THEME_ICON[c];
            const hint = c === "system" ? t("v3.theme.systemHint", { theme: t(`v3.theme.${theme}`) }) : t(`v3.theme.${c}Hint`);
            return (
              <button key={c} type="button" role="menuitemradio" aria-checked={choice === c} data-choice={c}
                onClick={() => { setChoice(c); setOpen(false); }}>
                <span className="swatch" data-swatch={c} aria-hidden="true" />
                <span className="text">
                  <b>{t(`v3.theme.${c}`)}</b>
                  <small>{hint}</small>
                </span>
                <ItemIcon size={14} className="ico" aria-hidden="true" />
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}
