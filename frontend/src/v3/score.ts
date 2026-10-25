// Command palette ranking — pure, unit-tested.

/** Subsequence match with a preference for word starts — "shcs" finds "shop-cs". */
export function score(query: string, text: string): number {
  const q = query.trim().toLowerCase();
  if (!q) return 1;
  const t = text.toLowerCase();
  const at = t.indexOf(q);
  if (at >= 0) {
    // a match that starts a word ("cs" in "shop-cs") is what was meant, over one
    // buried inside a word ("cs" in "discs")
    const wordStart = at === 0 || /[\s\-_./·]/.test(t[at - 1]);
    return 100 - at + (wordStart ? 50 : 0);
  }
  let ti = 0;
  let hits = 0;
  for (const ch of q) {
    const found = t.indexOf(ch, ti);
    if (found < 0) return 0;
    if (found === 0 || /[\s\-_./·]/.test(t[found - 1])) hits += 2;
    hits += 1;
    ti = found + 1;
  }
  return hits;
}
