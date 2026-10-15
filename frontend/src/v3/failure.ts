// An action's failure stays where it happened (storyline-ux §4, guided-console §3.4):
// V3 pages hold it in `useFailure()` and render it with `<ErrorCallout>` next to the
// control that caused it, instead of an error toast that disappears. The callout reads
// the envelope's `next` / `needs`; the toast channel (hooks.ts) only carries success.
import { useCallback, useState } from "react";

import { ApiError } from "../lib/api";

/**
 * `err` under the page's own headline ("Sync failed: …"), keeping the envelope —
 * code, request id, `next`, `needs` — so the callout still says where to fix it. A
 * non-API error becomes the headline alone.
 */
export function failedWith(text: string, err?: unknown): unknown {
  if (!(err instanceof ApiError)) return text;
  const copy = new ApiError(err.code, err.message, err.detail, {
    rawMessage: err.rawMessage, title: text, requestId: err.requestId, next: err.next, needs: err.needs,
  });
  copy.at = err.at;
  return copy;
}

export interface Failure {
  /** what the last action failed with; null when none (or dismissed) */
  error: unknown;
  /** record a failure — with `text`, under that headline */
  fail: (err: unknown, text?: string) => void;
  /** dismiss it; call when the user acts again */
  clear: () => void;
}

export function useFailure(): Failure {
  const [error, setError] = useState<unknown>(null);
  const fail = useCallback((err: unknown, text?: string) => setError(text ? failedWith(text, err) : err), []);
  const clear = useCallback(() => setError(null), []);
  return { error, fail, clear };
}
