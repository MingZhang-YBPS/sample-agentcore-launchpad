import type { Signal } from "../../ui";

/** Memory / strategy / resource status → signal (CREATING / UPDATING / DELETING settle on their own). */
export function memSignal(status: string | null | undefined): Signal {
  const s = (status ?? "").toUpperCase();
  if (s === "ACTIVE" || s === "SUCCEEDED" || s === "COMPLETED") return "ok";
  if (s === "FAILED" || s === "ERROR") return "act";
  if (!s) return "off";
  return "wait";
}
