import type { Signal } from "../../ui";

/** A normalized 0..1 score as a signal: the Bad Case line (0.7) is the ok boundary. */
export function scoreSignal(value: number | null | undefined): Signal {
  if (value == null) return "off";
  return value >= 0.7 ? "ok" : value >= 0.4 ? "wait" : "act";
}
