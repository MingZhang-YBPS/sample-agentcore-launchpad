/**
 * An agent's structured answer (strands-builder §2.4, SB-1): with an output schema the
 * runtime sends the parsed object as a `structured` SSE event (`{value}`) next to the
 * text answer. Chat shows it under the answer — a two-column table when it is a flat
 * object (every value a scalar), the JSON otherwise.
 */

export type StructuredScalar = string | number | boolean | null;

export type StructuredView =
  | { kind: "table"; rows: { key: string; value: StructuredScalar }[]; json: string }
  | { kind: "json"; json: string };

/** a table stays readable up to here; a wider object reads better as JSON */
export const STRUCTURED_TABLE_MAX_KEYS = 40;

const isScalar = (v: unknown): v is StructuredScalar =>
  v === null || typeof v === "string" || typeof v === "number" || typeof v === "boolean";

const isPlainObject = (v: unknown): v is Record<string, unknown> =>
  typeof v === "object" && v !== null && !Array.isArray(v);

/** How to draw a structured value; null when there is nothing to draw. */
export function structuredView(value: unknown): StructuredView | null {
  if (value === undefined) return null;
  const json = JSON.stringify(value, null, 2) ?? String(value);
  if (isPlainObject(value)) {
    const entries = Object.entries(value);
    if (entries.length > 0 && entries.length <= STRUCTURED_TABLE_MAX_KEYS && entries.every(([, v]) => isScalar(v))) {
      return { kind: "table", rows: entries.map(([key, v]) => ({ key, value: v as StructuredScalar })), json };
    }
  }
  return { kind: "json", json };
}

/** One table cell: strings as they are, everything else as its JSON spelling. */
export const scalarText = (v: StructuredScalar): string => (typeof v === "string" ? v : JSON.stringify(v));
