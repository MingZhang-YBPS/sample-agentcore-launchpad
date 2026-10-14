import { ArrowUp, Plus, ShieldX, Square, ThumbsDown, ThumbsUp, Wrench } from "lucide-react";
import { type KeyboardEvent, useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link, useSearchParams } from "react-router-dom";

import { JumpToLatest } from "../../components/JumpToLatest";
import { Markdown } from "../../components/Markdown";
import {
  api,
  chatApi,
  type ChatSessionInfo,
  errorMessage,
  feedbackApi,
  type FeedbackVerdict,
} from "../../lib/api";
import {
  activityLabel,
  chatEligible,
  chatErrorText,
  isHarnessAgent,
  seconds,
  sseEvents,
  STALL_ABORT_MS,
  TOOL_RUN_GLYPH,
  type ToolRun,
  toolRunById,
  toolRunLabel,
  turnActivity,
  turnElapsedMs,
  type TurnStatus,
  turnStopReason,
  useChatTurn,
} from "../../lib/chat";
import { useStickToBottom } from "../../lib/useStickToBottom";
import { livePolicyDeny, type PolicyDeny, policyDenyHref, restoredPolicyDeny } from "../../lib/policy-deny";
import { useAuth } from "../../auth/auth-context";
import { useWorkspace } from "../../workspace/workspace-context";
import { appendDelta } from "../../v2/pages/chat/messages";
import type { ChatMessage } from "../../v2/pages/chat/messages";
import { agentSignal } from "../signals";
import { Btn, Empty, Lamp, Notice, Skeleton } from "../ui";
import { useLoad, useToast } from "../hooks";
import { ago } from "../format";
import "./chat.css";
import "./chatStatus.css";

/** A thread item; a tool row of a live turn carries its run (status, duration),
 *  tagged with the turn it belongs to. `note`: a neutral notice (the user stopped). */
type Msg = ChatMessage & { run?: ToolRun; turn?: number; note?: boolean };

/**
 * Chat, rebuilt around the conversation. The same SSE invoke chain as V2 and the
 * public /v1 API (one place changes invoke behaviour), drawn as a focused thread:
 * sessions on the left, the conversation in a single readable column, the composer
 * always in reach (⏎ to send, ⇧⏎ for a new line). Attachments and as_user consent
 * are not rebuilt here yet — the thread says so and hands that turn to V2.
 */
/**
 * A Gateway tool call a Cedar policy denied for the signed-in identity: the agent
 * reached the tool, the Gateway refused it. No retry — a deny holds until the
 * rule changes; the link goes to the policies that decide it.
 */
function PolicyDenied({ deny }: { deny: PolicyDeny }) {
  const { t } = useTranslation();
  const { authRequired, username, role } = useAuth();
  return (
    <div className="v3-deny" data-testid="policy-deny-card">
      <div className="head">
        <ShieldX size={15} aria-hidden="true" />
        <b>{t("v2.chat.policy.title")}</b>
        <span className="mono">{deny.tool || "—"}</span>
        <span className="v3-chip" data-s="act">{t("v2.chat.policy.denied")}</span>
      </div>
      <p>{t("v2.chat.policy.body", { tool: deny.tool || "—" })}</p>
      <dl className="v3-kv">
        <dt>{t("v2.chat.policy.reason")}</dt>
        <dd data-testid="policy-deny-reason">{deny.reason || "—"}</dd>
        {deny.policyId && (<><dt>{t("v2.chat.policy.policy")}</dt><dd className="mono">{deny.policyId}</dd></>)}
        {authRequired && username && (<><dt>{t("v2.chat.policy.identity")}</dt><dd><span className="mono">{username}</span>{role ? ` · ${role}` : ""}</dd></>)}
      </dl>
      <Link className="v3-btn sm" to={policyDenyHref(deny)} data-testid="policy-deny-link">
        {t(deny.gatewayId ? "v2.chat.policy.openPolicies" : "v2.chat.policy.openGovernance")}
      </Link>
    </div>
  );
}

export function V3Chat() {
  const { t } = useTranslation();
  const toast = useToast();
  const { current } = useWorkspace();
  const [params, setParams] = useSearchParams();
  const agents = useLoad(() => api.listAgents(), `v3-chat-agents:${current?.id ?? ""}`);
  const eligible = useMemo(() => (agents.data ? chatEligible(agents.data.agents) : []), [agents.data]);
  const agentId = params.get("agent") ?? "";
  const agent = eligible.find((a) => a.id === agentId);
  const [sessionId, setSessionId] = useState<string | undefined>(params.get("session") ?? undefined);
  const [sessions, setSessions] = useState<ChatSessionInfo[]>([]);
  const [messages, setMessages] = useState<Msg[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [restoring, setRestoring] = useState(false);
  const [needsV2, setNeedsV2] = useState(false);
  // chat-streaming S2: what the running turn is doing, its timer and its stop
  const turn = useChatTurn();
  const turnSeq = useRef(0);
  // follows the stream only while the reader is at the bottom
  const thread = useStickToBottom();
  const inputRef = useRef<HTMLTextAreaElement>(null);

  // the first eligible agent is the default, so the page is never empty on arrival
  useEffect(() => {
    if (!agentId && eligible.length) setParams({ agent: eligible[0].id }, { replace: true });
  }, [agentId, eligible, setParams]);

  const loadSessions = (aid: string) =>
    api.listChatSessions(aid).then((r) => setSessions(r.sessions)).catch(() => setSessions([]));

  useEffect(() => {
    if (agentId) void loadSessions(agentId);
  }, [agentId]);

  const open = async (sid: string) => {
    if (!agentId || busy) return;
    setRestoring(true);
    setNeedsV2(false);
    try {
      const rows = (await chatApi.history(agentId, sid)).messages;
      setMessages(
        // a consent ask is not replayed (its URL is single-use): the next turn asks again
        rows.filter((r) => r.role !== "auth").map((r): ChatMessage =>
          r.role === "user"
            ? { kind: "user", text: r.text }
            : r.role === "agent"
              ? { kind: "agent", text: r.text, id: r.id, verdict: r.verdict ?? null, curated: !!r.answered_by }
              : r.role === "tool"
                ? { kind: "tool", text: r.name ?? "tool", name: r.name ?? "tool" }
                : r.role === "policy"
                  ? { kind: "policy", text: r.text, name: r.name ?? "", policy: restoredPolicyDeny(r) }
                  : { kind: "error", text: r.text },
        ),
      );
      setSessionId(sid);
      setParams({ agent: agentId, session: sid }, { replace: true });
      thread.toBottom(); // an opened conversation starts at its latest turn
    } catch (err) {
      toast("act", errorMessage(err));
    } finally {
      setRestoring(false);
    }
  };

  // a deep link with ?session= back-fills the thread once
  const restored = useRef(false);
  useEffect(() => {
    if (!restored.current && agentId && sessionId && messages.length === 0) {
      restored.current = true;
      void open(sessionId);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [agentId, sessionId]);

  const fresh = () => {
    setMessages([]);
    setSessionId(undefined);
    setNeedsV2(false);
    setParams(agentId ? { agent: agentId } : {}, { replace: true });
    inputRef.current?.focus();
  };

  const send = async () => {
    const prompt = input.trim();
    if (!prompt || !agentId || busy) return;
    setInput("");
    setBusy(true);
    setMessages((m) => [...m, { kind: "user", text: prompt }]);
    thread.toBottom(); // the reader's own turn is always in view
    let failed = false;
    let completed = false;
    let active = sessionId;
    const signal = turn.begin();
    const key = ++turnSeq.current;
    // a tool row follows its run in the turn status (ids repeat across turns)
    const syncRuns = (status: TurnStatus | null) =>
      setMessages((m) =>
        m.map((msg) => (msg.turn === key && msg.run ? { ...msg, run: toolRunById(status, msg.run.id) ?? msg.run } : msg)),
      );
    try {
      const res = await chatApi.stream(agentId, { prompt, session_id: sessionId ?? null });
      if (!res.body) throw new Error(t("v3.chat.interrupted"));
      let openBubble = false;
      let curated = false;
      for await (const { event, data } of sseEvents(res, { heartbeats: true, signal })) {
        const status = turn.feed(event, data);
        if (event === "tool_end") {
          syncRuns(status);
        } else if (event === "meta" && data.session_id) {
          active = data.session_id;
          setSessionId(data.session_id);
          setParams({ agent: agentId, session: data.session_id }, { replace: true });
        } else if (event === "rule") {
          curated = true;
        } else if (event === "tool") {
          const run = status?.tools[status.tools.length - 1];
          setMessages((m) => [...m, { kind: "tool", text: data.name ?? "tool", name: data.name, run, turn: key }]);
          openBubble = false;
        } else if (event === "auth_required") {
          // consent cards are V2's for now: say so rather than half-render one
          setNeedsV2(true);
        } else if (event === "policy_denied") {
          // follows its tool row, which already closed the open bubble
          const policy = livePolicyDeny(data);
          setMessages((m) => [...m, { kind: "policy", text: policy.reason, name: policy.tool, policy }]);
        } else if (event === "delta") {
          const wasOpen = openBubble;
          setMessages((m) => appendDelta(m, data.text ?? "", wasOpen, curated));
          openBubble = true;
        } else if (event === "saved") {
          setMessages((m) => {
            const next = [...m];
            for (let i = next.length - 1; i >= 0; i--) {
              if (next[i].kind === "agent") {
                if (next[i].id == null) next[i] = { ...next[i], id: data.message_id };
                break;
              }
            }
            return next;
          });
        } else if (event === "error") {
          failed = true;
          syncRuns(status);
          setMessages((m) => [...m, { kind: "error", text: chatErrorText(data, t("v3.chat.failed")) }]);
        } else if (event === "done") {
          completed = true;
          syncRuns(status);
        }
      }
      const reason = turnStopReason(signal);
      if (reason === "stop" && !completed && !failed) {
        setMessages((m) => [...m, { kind: "error", text: t("v3.chat.status.stopped"), note: true }]);
      } else if (reason === "stalled") {
        throw new Error(t("v3.chat.status.lost", { s: seconds(STALL_ABORT_MS) }));
      } else if (!completed && !failed) {
        throw new Error(t("v3.chat.interrupted"));
      }
    } catch (err) {
      failed = true;
      setMessages((m) => [...m, { kind: "error", text: errorMessage(err) }]);
    } finally {
      if (failed) setInput(prompt);
      // a call the stream never ended stopped with the turn
      if (!completed) syncRuns(turn.feed("error", {}));
      turn.end();
      setMessages((m) => m.map((msg) => (msg.streaming ? { ...msg, streaming: false } : msg)));
      setBusy(false);
      if (active) void loadSessions(agentId);
      inputRef.current?.focus();
    }
  };

  // Stop: close the stream (the server then stops reading the runtime); a code
  // runtime's session is stopped too. A Harness has no stop operation — closing
  // the connection is all there is.
  const stopTurn = () => {
    turn.stop("stop");
    if (agentId && sessionId && !isHarnessAgent(agent)) {
      void api.stopChatSession(agentId, sessionId).catch(() => undefined);
    }
  };
  const activity = turn.status ? turnActivity(turn.status, turn.now) : null;

  const rate = async (index: number, verdict: FeedbackVerdict | "none") => {
    const msg = messages[index];
    if (!msg?.id || !sessionId || !agentId) return;
    const before = msg.verdict ?? null;
    setMessages((m) => m.map((x, i) => (i === index ? { ...x, verdict: verdict === "none" ? null : verdict } : x)));
    try {
      await feedbackApi.rate(agentId, { session_id: sessionId, message_id: msg.id, verdict });
    } catch (err) {
      setMessages((m) => m.map((x, i) => (i === index ? { ...x, verdict: before } : x)));
      toast("act", errorMessage(err));
    }
  };

  const onKey = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      void send();
    }
  };

  return (
    <div className="v3-chat">
      <aside className="v3-chat-side">
        <label className="v3-stat" style={{ display: "block" }}>
          <span className="label">{t("v3.chat.agent")}</span>
          <select
            className="v3-select"
            style={{ marginTop: 8 }}
            value={agentId}
            onChange={(e) => {
              setMessages([]);
              setSessionId(undefined);
              setParams({ agent: e.target.value });
            }}
          >
            {eligible.length === 0 && <option value="">{t("v3.chat.noAgents")}</option>}
            {eligible.map((a) => (
              <option key={a.id} value={a.id}>{a.display_name || a.name}</option>
            ))}
          </select>
        </label>
        <Btn onClick={fresh} disabled={!agentId}>
          <Plus size={14} /> {t("v3.chat.new")}
        </Btn>
        <div className="v3-rail-label" style={{ padding: "6px 2px 0" }}>{t("v3.chat.sessions")}</div>
        <div className="v3-chat-sessions">
          {sessions.length === 0 && <span style={{ color: "var(--v3-text-3)", fontSize: 12 }}>{t("v3.chat.noSessions")}</span>}
          {sessions.map((s) => (
            <button key={s.session_id} type="button" className={`v3-chat-session${s.session_id === sessionId ? " on" : ""}`}
              onClick={() => void open(s.session_id)}>
              <span className="preview">{s.preview || s.session_id.slice(0, 12)}</span>
              <span className="meta mono">{s.turns} · {ago(s.last_at)}</span>
            </button>
          ))}
        </div>
      </aside>

      <section className="v3-chat-main">
        <header className="v3-chat-head">
          {agent ? (
            <>
              <Lamp s={agentSignal(agent)} live={agent.status === "active"} />
              <b>{agent.display_name || agent.name}</b>
              <span className="mono" style={{ color: "var(--v3-text-3)" }}>{sessionId ? sessionId.slice(0, 16) : t("v3.chat.newSession")}</span>
              <Link to={`/v3/agents?id=${agent.id}`} className="v3-btn ghost sm" style={{ marginLeft: "auto" }}>
                {t("v3.chat.agentPage")}
              </Link>
            </>
          ) : (
            <span style={{ color: "var(--v3-text-3)" }}>{t("v3.chat.pick")}</span>
          )}
        </header>

        <div className="v3-chat-thread" ref={thread.ref}>
          {restoring ? (
            <Skeleton rows={5} />
          ) : messages.length === 0 ? (
            <Empty title={t("v3.chat.emptyTitle")}>{t("v3.chat.emptySub")}</Empty>
          ) : (
            messages.map((m, i) => {
              if (m.kind === "user") return <div key={i} className="v3-msg user"><div className="bubble">{m.text}</div></div>;
              if (m.kind === "tool")
                return (
                  <div key={i} className="v3-msg tool" data-run={m.run?.status} data-testid="chat-tool">
                    {m.run ? (
                      <span className="v3-tool-glyph" aria-hidden="true">{TOOL_RUN_GLYPH[m.run.status]}</span>
                    ) : (
                      <Wrench size={12} aria-hidden="true" />
                    )}{" "}
                    <span className="mono">{m.name ?? m.text}</span>
                    {m.run && <span className="v3-tool-state">{toolRunLabel(t, m.run, turn.now)}</span>}
                  </div>
                );
              if (m.kind === "error") return <div key={i} className="v3-msg"><Notice s={m.note ? "wait" : "act"}>{m.text}</Notice></div>;
              if (m.kind === "policy" && m.policy) return <div key={i} className="v3-msg"><PolicyDenied deny={m.policy} /></div>;
              if (m.kind !== "agent") return null;
              return (
                <div key={i} className={`v3-msg agent${m.streaming ? " streaming" : ""}`}>
                  <div className="bubble">
                    <Markdown text={m.text} />
                    {m.streaming && <span className="v3-caret" aria-hidden="true" />}
                  </div>
                  {m.id != null && !m.streaming && (
                    <div className="v3-msg-actions">
                      {m.curated && <span className="v3-chip" data-s="info">{t("v3.chat.curated")}</span>}
                      <button type="button" aria-pressed={m.verdict === "up"} aria-label={t("v3.chat.up")}
                        onClick={() => void rate(i, m.verdict === "up" ? "none" : "up")}>
                        <ThumbsUp size={13} />
                      </button>
                      <button type="button" aria-pressed={m.verdict === "down"} aria-label={t("v3.chat.down")}
                        onClick={() => void rate(i, m.verdict === "down" ? "none" : "down")}>
                        <ThumbsDown size={13} />
                      </button>
                    </div>
                  )}
                </div>
              );
            })
          )}
          {turn.status && activity && (
            <div className="v3-msg agent v3-working" data-kind={activity.kind} data-testid="chat-working" role="status">
              <div className="bubble">
                <span className="v3-working-dot" aria-hidden="true" />
                <span>{activityLabel(t, activity)}</span>
                <span className="mono v3-working-time">
                  {t("v3.chat.status.elapsed", { s: seconds(turnElapsedMs(turn.status, turn.now)) })}
                </span>
              </div>
            </div>
          )}
          {needsV2 && (
            <div className="v3-msg">
              <Notice s="wait">
                {t("v3.chat.consentInV2")}{" "}
                <Link to={`/v2/chat?agent=${agentId}${sessionId ? `&session=${sessionId}` : ""}&full=1`} style={{ textDecoration: "underline" }}>
                  {t("v3.chat.openInV2")}
                </Link>
              </Notice>
            </div>
          )}
          <JumpToLatest show={!thread.atBottom} onClick={thread.toBottom} />
        </div>

        <form
          className="v3-chat-composer"
          onSubmit={(e) => {
            e.preventDefault();
            void send();
          }}
        >
          <textarea
            ref={inputRef}
            rows={1}
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={onKey}
            placeholder={agent ? t("v3.chat.placeholder", { name: agent.display_name || agent.name }) : t("v3.chat.pick")}
            disabled={!agent || busy}
            aria-label={t("v3.chat.placeholder", { name: agent?.name ?? "" })}
          />
          {busy ? (
            <button type="button" className="v3-btn" onClick={stopTurn} aria-label={t("v3.chat.status.stop")}
              title={t("v3.chat.status.stop")} data-testid="chat-stop">
              <Square size={14} />
            </button>
          ) : (
            <button type="submit" className="v3-btn primary" disabled={!agent || !input.trim()} aria-label={t("v3.chat.send")}>
              <ArrowUp size={16} />
            </button>
          )}
        </form>
      </section>
    </div>
  );
}
