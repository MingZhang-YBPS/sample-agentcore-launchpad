import { useState } from "react";

import { buildSnippet, type SnippetLang, type SnippetMode } from "../snippets";

/** Integration (T17): copy-ready curl / Python / JavaScript for this agent's public `/v1` API. */
export function useIntegration(agentId: string, baseUrl: string = window.location.origin) {
  const [lang, setLang] = useState<SnippetLang>("curl");
  const [mode, setMode] = useState<SnippetMode>("sync");
  const snippet = buildSnippet({ baseUrl, agentId }, lang, mode);
  /** resolves true once the snippet is on the clipboard */
  const copy = (): Promise<boolean> =>
    navigator.clipboard
      .writeText(snippet)
      .then(() => true)
      .catch(() => false);
  return { lang, setLang, mode, setMode, snippet, copy };
}
