import { Play } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";

import { ErrorCallout } from "../../../components/ErrorCallout";
import { type AgentInfo } from "../../../lib/api";
import { streamChat } from "../../../lib/chat";
import { useFailure } from "../../failure";
import { ms } from "../../format";
import { Reasoned } from "../../reason";
import { agentNotRunning } from "../../reasons";
import { Btn, Panel } from "../../ui";
import { readTurn } from "./tryTurn";

/** One-shot turn from the detail page (the full conversation is Chat). It runs over
 *  the console chat stream, not the sync `/invoke`: the stream carries heartbeats, so
 *  a cold start or a long answer is not cut off by a proxy's read timeout. */
export function TryIt({ agent }: { agent: AgentInfo }) {
  const { t } = useTranslation();
  // a failed call stays under the form, with the way out (storyline-ux §4)
  const failed = useFailure();
  const [prompt, setPrompt] = useState("");
  const [answer, setAnswer] = useState<{ text: string; latency: number | null } | null>(null);
  const [busy, setBusy] = useState(false);
  const run = async () => {
    if (!prompt.trim() || busy || agent.status !== "active") return;
    setBusy(true);
    failed.clear();
    setAnswer(null);
    const started = performance.now();
    try {
      const res = await streamChat(agent.id, { prompt: prompt.trim(), session_id: null });
      const text = await readTurn(res, (partial) => setAnswer({ text: partial, latency: null }),
        t("v3.chat.failed"));
      setAnswer({ text, latency: Math.round(performance.now() - started) });
    } catch (err: unknown) {
      failed.fail(err);
    } finally {
      setBusy(false);
    }
  };
  return (
    <Panel title={t("v3.agent.try")}
      end={answer?.latency != null ? <span className="mono">{ms(answer.latency)}</span> : undefined}>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          void run();
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
