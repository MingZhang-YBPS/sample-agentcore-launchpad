import { SendHorizontal } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";

import { api, chatApi, errorMessage, localizedMessage } from "../../../lib/api";
import { seconds, sseEvents, STALL_ABORT_MS, turnStopReason, useChatTurn } from "../../../lib/chat";
import { TurnStatusLine } from "../../../components/chat/TurnStatus";
import { Alert, Button, Card } from "../../ui";
import "./tryChat.css";
import { JumpToLatest } from "../../../components/JumpToLatest";
import { useStickToBottom } from "../../../lib/useStickToBottom";

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
  // chat-streaming S2: what the running turn is doing, its timer and its stop
  const turn = useChatTurn();
  // follows the stream only while the reader is at the bottom
  const body = useStickToBottom();

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
    body.toBottom();
    let completed = false;
    let failed = false;
    const signal = turn.begin();
    try {
      const res = await chatApi.stream(agentId, { prompt, session_id: sessionRef.current });
      if (!res.body) throw new Error(t("chatPage.streamInterrupted"));
      for await (const { event, data } of sseEvents(res, { heartbeats: true, signal })) {
        turn.feed(event, data);
        if (event === "meta" && data.session_id) sessionRef.current = data.session_id;
        else if (event === "delta") appendToAgent(data.text ?? "");
        else if (event === "error") {
          failed = true;
          push({ role: "error", text: localizedMessage(data.code ?? "", data.message ?? t("chatPage.sendFailed")) });
        } else if (event === "done") completed = true;
      }
      const reason = turnStopReason(signal);
      if (reason === "stop" && !completed && !failed) push({ role: "error", text: t("v3.chat.status.stopped") });
      else if (reason === "stalled") throw new Error(t("v3.chat.status.lost", { s: seconds(STALL_ABORT_MS) }));
      else if (!completed && !failed) throw new Error(t("chatPage.streamInterrupted"));
    } catch (err) {
      push({ role: "error", text: errorMessage(err) });
    } finally {
      turn.end();
      setBusy(false);
    }
  };

  // Stop: close the stream; a code runtime's session is stopped too (a Harness
  // refuses the stop by name, which is harmless here — closing is all it has).
  const stopTurn = () => {
    turn.stop("stop");
    if (sessionRef.current) void api.stopChatSession(agentId, sessionRef.current).catch(() => undefined);
  };

  return (
    <Card title={t("v2.agents.tryChat.title")} sub={t("v2.agents.tryChat.sub")} testId="v2-agent-trychat">
      <div className="v2-trychat-body" ref={body.ref} aria-live="polite">
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
        {turn.status && <TurnStatusLine status={turn.status} now={turn.now} onStop={stopTurn} testId="v2-agent-trychat-status" />}
        <JumpToLatest show={!body.atBottom} onClick={body.toBottom} />
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
