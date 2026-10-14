import { Play } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";

import { api, errorMessage, type AgentInfo } from "../../../lib/api";
import { ms } from "../../format";
import { useToast } from "../../hooks";
import { Btn, Notice, Panel } from "../../ui";

/** One-shot invoke from the detail page (the full conversation is Chat). */
export function TryIt({ agent }: { agent: AgentInfo }) {
  const { t } = useTranslation();
  const toast = useToast();
  const [prompt, setPrompt] = useState("");
  const [answer, setAnswer] = useState<{ text: string; latency: number } | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const run = () => {
    if (!prompt.trim() || busy || agent.status !== "active") return;
    setBusy(true);
    setError(null);
    setAnswer(null);
    api
      .invokeAgent(agent.id, prompt.trim())
      .then((res) => setAnswer({ text: res.text, latency: res.latency_ms }))
      .catch((err: unknown) => { setError(errorMessage(err)); toast("act", errorMessage(err)); })
      .finally(() => setBusy(false));
  };
  return (
    <Panel title={t("v3.agent.try")} end={answer ? <span className="mono">{ms(answer.latency)}</span> : undefined}>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          run();
        }}
        style={{ display: "flex", gap: 8 }}
      >
        <input className="v3-input" value={prompt} onChange={(e) => setPrompt(e.target.value)}
          aria-label={t("v3.agent.tryPlaceholder")} placeholder={t("v3.agent.tryPlaceholder")} disabled={busy || agent.status !== "active"} />
        <Btn kind="primary" type="submit" disabled={busy || agent.status !== "active" || !prompt.trim()}>
          <Play size={14} /> {t(busy ? "v3.ux.running" : "v3.agent.run")}
        </Btn>
      </form>
      {agent.status !== "active" && <p className="v3-hint">{t("v3.ux.invokeUnavailable")}</p>}
      {error && <Notice s="act">{error}</Notice>}
      {answer && (
        <pre className="mono" aria-live="polite" style={{ marginTop: 14, whiteSpace: "pre-wrap", color: "var(--v3-text)", background: "var(--v3-ink-0)",
          border: "1px solid var(--v3-line)", borderRadius: 8, padding: 12, maxHeight: 260, overflow: "auto" }}>
          {answer.text}
        </pre>
      )}
    </Panel>
  );
}
