import { useSyncExternalStore } from "react";

// Which console experience the operator chose: the redesigned console V3 (the
// default, under /v3), console V2, or the original console (v1). All ship side by
// side and render the same modules: in V2 / V3 the classic routes (/chat, /agents,
// …) keep their URLs but render inside that console's shell. The choice is a
// per-browser convenience: only an explicit "back to V2" (a stored "v2") or "back
// to classic" (a stored "v1") opts out, so a blocked or empty storage means V3.
export type UiVersion = "v1" | "v2" | "v3";

const KEY = "launchpad_ui_version";

const listeners = new Set<() => void>();
// the in-memory copy keeps the switch working when storage is unavailable
let current: UiVersion = read();

function read(): UiVersion {
  try {
    const stored = localStorage.getItem(KEY);
    // V3 is the default: only an explicit choice of V2 or classic stays there
    return stored === "v1" ? "v1" : stored === "v2" ? "v2" : "v3";
  } catch {
    return "v3";
  }
}

export function getUiVersion(): UiVersion {
  return current;
}

export function setUiVersion(version: UiVersion): void {
  try {
    localStorage.setItem(KEY, version);
  } catch {
    // storage unavailable (private window, blocked site data): the switch
    // still applies to this tab, it just is not remembered
  }
  if (version === current) return;
  current = version;
  listeners.forEach((notify) => notify());
}

function subscribe(notify: () => void): () => void {
  listeners.add(notify);
  return () => listeners.delete(notify);
}

/** The current choice; re-renders the caller when either console switches. */
export function useUiVersion(): UiVersion {
  return useSyncExternalStore(subscribe, getUiVersion);
}
