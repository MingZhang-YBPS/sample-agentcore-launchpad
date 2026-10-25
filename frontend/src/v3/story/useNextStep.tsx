import { useCallback, useState } from "react";
import { useLocation } from "react-router-dom";

import { type NextStepNote, noteVisible } from "./flow";
import { NextStepCard } from "./NextStepCard";

/**
 * A page's next-step card: `show()` it from an action's success path; it stays at that
 * spot until dismissed or the user navigates (another path or query).
 */
export function useNextStep() {
  const location = useLocation();
  const here = `${location.pathname}${location.search}`;
  const [note, setNote] = useState<(NextStepNote & { at: string; seq: number }) | null>(null);
  const show = useCallback((n: NextStepNote) => setNote((prev) => ({ ...n, at: here, seq: (prev?.seq ?? 0) + 1 })), [here]);
  const clear = useCallback(() => setNote(null), []);
  const card = note && noteVisible(note.at, here)
    ? <NextStepCard key={`${note.after}:${note.seq}`} note={note} onDismiss={clear} />
    : null;
  return { show, clear, card };
}
