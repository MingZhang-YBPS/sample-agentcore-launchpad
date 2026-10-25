import { useSyncExternalStore } from "react";

/**
 * Which slice of the console the sidebar shows (roadmap T11).
 *
 * The V2 sidebar carries ~20 entries across six groups, most of them expert
 * surfaces (evaluation, experiments, governance, Skill Lab). A business member who
 * only wants an agent answering questions cannot tell which of those is for them,
 * so the sidebar has two modes:
 *
 * - `business` — the build → run essentials plus Learn. The default for a member.
 * - `expert`   — every entry, as before. The default for an administrator, whose
 *                job is precisely the expert surfaces.
 *
 * It is a **view filter, not authorization**: every route stays reachable by URL
 * and the backend's route policy is untouched. Stored per browser like
 * `ui-version`, so a member who switches to expert mode stays there; an empty or
 * blocked storage falls back to the role default, which is why `getNavMode`
 * takes it as an argument rather than baking one in.
 */
export type NavMode = "business" | "expert";

const KEY = "launchpad_nav_mode";

const listeners = new Set<() => void>();
let stored: NavMode | null = read();

function read(): NavMode | null {
  try {
    const value = localStorage.getItem(KEY);
    return value === "business" || value === "expert" ? value : null;
  } catch {
    return null;
  }
}

/** The stored choice, or the role default when the operator never chose. */
export function getNavMode(isAdmin: boolean): NavMode {
  return stored ?? (isAdmin ? "expert" : "business");
}

export function setNavMode(mode: NavMode): void {
  try {
    localStorage.setItem(KEY, mode);
  } catch {
    // storage unavailable (private window, blocked site data): the switch still
    // applies to this tab, it just is not remembered
  }
  if (mode === stored) return;
  stored = mode;
  listeners.forEach((notify) => notify());
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/** Reactive `[mode, setMode]`, defaulting on the caller's role. */
export function useNavMode(isAdmin: boolean): [NavMode, (mode: NavMode) => void] {
  const snapshot = useSyncExternalStore(
    subscribe,
    () => stored,
    () => stored,
  );
  return [snapshot ?? (isAdmin ? "expert" : "business"), setNavMode];
}
