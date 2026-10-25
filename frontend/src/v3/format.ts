// Console V3 formatters.
import { parseTimestamp } from "../lib/timestamps";

export function ago(iso: string | null | undefined, now = Date.now()): string {
  const at = parseTimestamp(iso);
  if (!at) return "—";
  const s = Math.max(0, Math.round((now - at.getTime()) / 1000));
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.round(s / 60)}m`;
  if (s < 86400) return `${Math.round(s / 3600)}h`;
  return `${Math.round(s / 86400)}d`;
}

export function ms(value: number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  return value >= 1000 ? `${(value / 1000).toFixed(value >= 10000 ? 0 : 1)}s` : `${Math.round(value)}ms`;
}

export function pct(value: number | null | undefined, digits = 0): string {
  return value === null || value === undefined ? "—" : `${(value * 100).toFixed(digits)}%`;
}
