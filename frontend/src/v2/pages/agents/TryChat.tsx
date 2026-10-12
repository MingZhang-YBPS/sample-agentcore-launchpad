import { SendHorizontal } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";

import { api, chatApi, errorMessage, localizedMessage } from "../../../lib/api";
import { sseEvents } from "../../../lib/chat";
import { Alert, Button, Card } from "../../ui";
import "./tryChat.css";

interface Turn {
  role: "user" | "agent" | "error";
  text: string;
}

/** Turns kept on screen (a turn is one user or agent message). */
const MAX_TURNS = 8;

/**
 * Inline try-chat for a freshly active agent. Streams through the same
 * `chatApi.stream` + `sseEvents` transport as the Chat console. Chatting is
 * allowed in prod workspaces, so this deliberately ignores the prod lock.
 */
export function TryChat({ agentId }: { agentId: string }) {
  const { t, i18n } = useTranslation();
  const [turns, setTurns] = useState<Turn[]>([]);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [suggestions, setSuggestions] = useState<string[]>([]);
  const sessionRef = useRef<string | null>(null);
  const bodyRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    let live = true;
    api
      .suggestedQuestions(agentId, i18n.language)
      .then((r) => live && setSuggestions(r.questions))
      .catch(() => undefined); // chips are a nicety; the box works without them
    return () => {
      live = false;
    };
  }, [agentId, i18n.language]);

  useEffect(() => {
    bodyRef.current?.scrollTo({ top: bodyRef.current.scrollHeight });
  }, [turns]);

  const push = (turn: Turn) => setTurns((prev) => [...prev, turn].slice(-MAX_TURNS));
  const appendToAgent = (delta: string) =>
    setTurns((prev) => {
      const last = prev[prev.length - 1];
      if (last?.role === "agent") return [...prev.slice(0, -1), { ...last, text: last.text + delta }];
      return [...prev, { role: "agent" as const, text: delta }].slice(-MAX_TURNS);
    });

  const send = async () => {
    const prompt = text.trim();
    if (!prompt || busy) return;
    setText("");
    setBusy(true);
    push({ role: "user", text: prompt });
    let completed = false;
    try {
      const res = await chatApi.stream(agentId, { prompt, session_id: sessionRef.current });
      if (!res.body) throw new Error(t("chatPage.streamInterrupted"));
      for await (const { event, data } of sseEvents(res)) {
        if (event === "meta" && data.session_id) sessionRef.current = data.session_id;
        else if (event === "delta") appendToAgent(data.text ?? "");
        else if (event === "error") {
          push({ role: "error", text: localizedMessage(data.code ?? "", data.message ?? t("chatPage.sendFailed")) });
        } else if (event === "done") completed = true;
      }
      if (!completed) throw new Error(t("chatPage.streamInterrupted"));
    } catch (err) {
      push({ role: "error", text: errorMessage(err) });
    } finally {
      setBusy(false);
    }
  };

  return (
    <Card title={t("v2.agents.tryChat.title")} sub={t("v2.agents.tryChat.sub")} testId="v2-agent-trychat">
      <div className="v2-trychat-body" ref={bodyRef} aria-live="polite">
        {turns.length === 0 && <p className="v2-trychat-empty">{t("v2.agents.tryChat.empty")}</p>}
        {turns.map((turn, i) =>
          turn.role === "error" ? (
            <Alert key={i} tone="error">
              {turn.text}
            </Alert>
          ) : (
            <div key={i} className={`v2-trychat-msg ${turn.role}`}>
              {turn.text}
            </div>
          ),
        )}
        {busy && turns[turns.length - 1]?.role === "user" && (
          <div className="v2-trychat-msg agent pending">{t("v2.agents.tryChat.thinking")}</div>
        )}
      </div>
      {suggestions.length > 0 && (
        <div className="v2-trychat-chips" data-testid="v2-agent-suggestions">
          {suggestions.map((q) => (
            <button key={q} type="button" className="v2-trychat-chip" onClick={() => setText(q)}>
              {q}
            </button>
          ))}
        </div>
      )}
      <form
        className="v2-trychat-form"
        onSubmit={(e) => {
          e.preventDefault();
          void send();
        }}
      >
        <input
          className="v2-input"
          value={text}
          maxLength={4000}
          placeholder={t("v2.agents.tryChat.placeholder")}
          aria-label={t("v2.agents.tryChat.placeholder")}
          onChange={(e) => setText(e.target.value)}
          data-testid="v2-agent-trychat-input"
        />
        <Button kind="primary" type="submit" disabled={busy || !text.trim()}>
          <SendHorizontal size={14} /> {t("v2.agents.tryChat.send")}
        </Button>
      </form>
      <div className="v2-trychat-foot">
        <Link to={`/v2/chat?agent=${encodeURIComponent(agentId)}`}>{t("v2.agents.tryChat.openChat")}</Link>
      </div>
    </Card>
  );
}
