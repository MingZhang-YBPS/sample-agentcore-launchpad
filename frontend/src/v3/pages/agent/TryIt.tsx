import { Play } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";

import { ErrorCallout } from "../../../components/ErrorCallout";
import { api, type AgentInfo } from "../../../lib/api";
import { useFailure } from "../../failure";
import { ms } from "../../format";
import { Reasoned } from "../../reason";
import { agentNotRunning } from "../../reasons";
import { Btn, Panel } from "../../ui";

/** One-shot invoke from the detail page (the full conversation is Chat). */
export function TryIt({ agent }: { agent: AgentInfo }) {
  const { t } = useTranslation();
  // a failed call stays under the form, with the way out (storyline-ux §4)
  const failed = useFailure();
  const [prompt, setPrompt] = useState("");
  const [answer, setAnswer] = useState<{ text: string; latency: number } | null>(null);
  const [busy, setBusy] = useState(false);
  const run = () => {
    if (!prompt.trim() || busy || agent.status !== "active") return;
    setBusy(true);
    failed.clear();
    setAnswer(null);
    api
      .invokeAgent(agent.id, prompt.trim())
      .then((res) => setAnswer({ text: res.text, latency: res.latency_ms }))
      .catch((err: unknown) => failed.fail(err))
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
        <Reasoned disabledReason={agent.status !== "active" ? agentNotRunning(t, agent.id) : null}>
          <input className="v3-input" value={prompt} onChange={(e) => setPrompt(e.target.value)}
            aria-label={t("v3.agent.tryPlaceholder")} placeholder={t("v3.agent.tryPlaceholder")} disabled={busy || agent.status !== "active"} />
        </Reasoned>
        <Btn kind="primary" type="submit" busy={busy} disabled={agent.status !== "active" || !prompt.trim()}
          disabledReason={agent.status !== "active" ? agentNotRunning(t, agent.id) : t("story.reason.typePrompt")}>
          <Play size={14} /> {t(busy ? "v3.ux.running" : "v3.agent.run")}
        </Btn>
      </form>
      {agent.status !== "active" && <p className="v3-hint">{t("v3.ux.invokeUnavailable")}</p>}
      {failed.error != null && (
        <div style={{ marginTop: 12 }}>
          <ErrorCallout error={failed.error} params={{ agent_id: agent.id }} onDismiss={failed.clear} data-testid="v3-try-error" />
        </div>
      )}
      {answer && (
        <pre className="mono" aria-live="polite" style={{ marginTop: 14, whiteSpace: "pre-wrap", color: "var(--v3-text)", background: "var(--v3-ink-0)",
          border: "1px solid var(--v3-line)", borderRadius: 8, padding: 12, maxHeight: 260, overflow: "auto" }}>
          {answer.text}
        </pre>
      )}
    </Panel>
  );
}
