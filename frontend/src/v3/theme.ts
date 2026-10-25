import { useCallback, useEffect, useState } from "react";

/** Console V3 colour theme: the choice in the top bar and the theme it resolves to.
 *
 * The palette lives in v3.css (`[data-theme="light"] .v3`); this module only picks
 * which one is on. The attribute sits on <html> so the tiny script in index.html
 * can set it before first paint — keep that script's logic in step with
 * `resolveTheme` (theme.test.ts runs it against this module). Light is the
 * default until someone chooses; "system" follows `prefers-color-scheme`. */

export type ThemeChoice = "light" | "dark" | "system";
export type Theme = "light" | "dark";

export const THEME_KEY = "launchpad_v3_theme";
export const THEME_CHOICES: readonly ThemeChoice[] = ["light", "dark", "system"];
const LIGHT_QUERY = "(prefers-color-scheme: light)";

export function parseChoice(raw: string | null | undefined): ThemeChoice {
  return raw === "light" || raw === "dark" || raw === "system" ? raw : "light";
}

export function resolveTheme(choice: ThemeChoice, systemLight: boolean): Theme {
  if (choice === "system") return systemLight ? "light" : "dark";
  return choice;
}

export function readChoice(storage: Pick<Storage, "getItem"> | undefined = safeStorage()): ThemeChoice {
  try {
    return parseChoice(storage?.getItem(THEME_KEY));
  } catch {
    return "light";
  }
}

function safeStorage(): Storage | undefined {
  try {
    return typeof localStorage === "undefined" ? undefined : localStorage;
  } catch {
    return undefined;
  }
}

function systemPrefersLight(): boolean {
  return typeof window !== "undefined" && typeof window.matchMedia === "function" && window.matchMedia(LIGHT_QUERY).matches;
}

function reducedMotion(): boolean {
  return typeof window !== "undefined" && typeof window.matchMedia === "function"
    && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

let switchTimer: ReturnType<typeof setTimeout> | undefined;

/** Put `theme` on <html>. `animate` crossfades the palette once (skipped under reduced motion). */
export function applyTheme(theme: Theme, root: HTMLElement = document.documentElement, animate = false): void {
  if (root.dataset.theme === theme) return;
  if (animate && !reducedMotion()) {
    root.setAttribute("data-theme-switching", "");
    clearTimeout(switchTimer);
    switchTimer = setTimeout(() => root.removeAttribute("data-theme-switching"), 320);
  }
  root.dataset.theme = theme;
}

/** The top bar's switch: the stored choice, the theme in effect, and a setter that persists. */
export function useTheme(): { choice: ThemeChoice; theme: Theme; setChoice: (next: ThemeChoice) => void } {
  const [choice, setChoiceState] = useState<ThemeChoice>(() => readChoice());
  const [systemLight, setSystemLight] = useState(systemPrefersLight);
  const theme = resolveTheme(choice, systemLight);

  useEffect(() => {
    if (typeof window.matchMedia !== "function") return;
    const mq = window.matchMedia(LIGHT_QUERY);
    const onChange = () => setSystemLight(mq.matches);
    mq.addEventListener?.("change", onChange);
    return () => mq.removeEventListener?.("change", onChange);
  }, []);

  // another tab changed the choice
  useEffect(() => {
    const onStorage = (e: StorageEvent) => {
      if (e.key === THEME_KEY) setChoiceState(parseChoice(e.newValue));
    };
    window.addEventListener("storage", onStorage);
    return () => window.removeEventListener("storage", onStorage);
  }, []);

  useEffect(() => applyTheme(theme, document.documentElement, true), [theme]);

  const setChoice = useCallback((next: ThemeChoice) => {
    try {
      localStorage.setItem(THEME_KEY, next);
    } catch {
      /* private mode: the choice lasts for this page only */
    }
    setChoiceState(next);
  }, []);

  return { choice, theme, setChoice };
}
