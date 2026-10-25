// Console V3 onboarding — per-browser memory of what a newcomer has already seen.
//
// None of this is authorization or user data: it only decides whether the tour
// starts on its own and whether the launch sequence is folded away. Storage can
// be blocked (private windows); every read then falls back to "not seen", and a
// tab-local copy keeps a dismissal working for the session.
import { useSyncExternalStore } from "react";

const TOUR_KEY = "launchpad_v3_tour_done";
const LAUNCH_KEY = "launchpad_v3_launch_hidden";

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

/* ── the guided tour ───────────────────────────────────────────────────── */

export const tourDone = () => get(TOUR_KEY) === "1";
export const markTourDone = () => put(TOUR_KEY, "1");

/** A request to open the tour now (from the help menu or ⌘K), whatever was seen. */
let tourRequest = 0;
export function requestTour() {
  tourRequest += 1;
  listeners.forEach((notify) => notify());
}
export function useTourRequest(): number {
  return useSyncExternalStore(subscribe, () => tourRequest);
}

/* ── the launch sequence ───────────────────────────────────────────────── */

/** Hidden per workspace: a new workspace starts its own sequence. */
function launchKey(workspaceId: string) {
  return `${LAUNCH_KEY}:${workspaceId}`;
}
export function useLaunchHidden(workspaceId: string): [boolean, (hidden: boolean) => void] {
  const hidden = useSyncExternalStore(subscribe, () => get(launchKey(workspaceId)) === "1");
  return [hidden, (next: boolean) => put(launchKey(workspaceId), next ? "1" : null)];
}

/* ── which glossary dialog is open (one at a time, from anywhere) ──────── */

let glossaryTerm: string | null = null;
export function openGlossary(term: string | "all") {
  glossaryTerm = term;
  listeners.forEach((notify) => notify());
}
export function closeGlossary() {
  glossaryTerm = null;
  listeners.forEach((notify) => notify());
}
export function useGlossaryOpen(): string | null {
  return useSyncExternalStore(subscribe, () => glossaryTerm);
}
