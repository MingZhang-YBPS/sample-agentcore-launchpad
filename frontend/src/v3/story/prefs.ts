// Per-browser memory of the storyline surfaces' layout (the journey strip folded or
// not). Not authorization or user data; blocked storage falls back to a tab-local copy.
import { useSyncExternalStore } from "react";

const JOURNEY_KEY = "launchpad_v3_journey_collapsed";

const memory = new Map<string, string>();
const listeners = new Set<() => void>();

function get(key: string): string | null {
  try {
    return localStorage.getItem(key) ?? memory.get(key) ?? null;
  } catch {
    return memory.get(key) ?? null;
  }
}

function put(key: string, value: string | null) {
  if (value === null) memory.delete(key);
  else memory.set(key, value);
  try {
    if (value === null) localStorage.removeItem(key);
    else localStorage.setItem(key, value);
  } catch {
    // the in-memory copy carries the choice for this tab
  }
  listeners.forEach((notify) => notify());
}

function subscribe(notify: () => void) {
  listeners.add(notify);
  return () => listeners.delete(notify);
}

/** D-2: open by default, folded once the user folds it — remembered across agents. */
export function useJourneyCollapsed(): [boolean, (collapsed: boolean) => void] {
  const collapsed = useSyncExternalStore(subscribe, () => get(JOURNEY_KEY) === "1", () => false);
  return [collapsed, (next: boolean) => put(JOURNEY_KEY, next ? "1" : null)];
}
