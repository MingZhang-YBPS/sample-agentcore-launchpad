// Local-debug chat drawer: multi-turn conversation against the active
// un-deployed studio code (backend replays history via `--messages`). Clearly
// labeled LOCAL DEBUG to distinguish it from the platform Chat page (deployed
// runtimes). Behavior ported from strands_studio_ui `src/components/chat-modal.tsx`
// (origin/main); launchpad-styled, in-memory session.
import { type KeyboardEvent, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { Loader, MessageSquare, RefreshCw, Send, Sparkles, Square } from "lucide-react";

import { Btn, Chip, useToast } from "../components";
import { AiFixPanel } from "./AiFixPanel";
import { useAiFix } from "./useAiFix";
import {
  createConversationSession,
  getCodegenStatus,
  sendChatMessageStream,
  updateConversationCode,
  type ChatMessage,
  type ConversationSession,
  type DebugApiKeys,
  type FlowData,
} from "./lib/debug-client";
import { JumpToLatest } from "../components/JumpToLatest";
import { useStickToBottom } from "../lib/useStickToBottom";

interface ChatDrawerProps {
  active: boolean;
  code: string;
  flowData: FlowData;
  graphMode: boolean;
  apiKeys: DebugApiKeys;
  onApplyFixedCode: (code: string) => boolean;
}

export function ChatDrawer({
  active,
  code,
  flowData,
  graphMode,
  apiKeys,
  onApplyFixedCode,
}: ChatDrawerProps) {
  const { t } = useTranslation();
  const toast = useToast();
  const [session, setSession] = useState<ConversationSession | null>(null);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState("");
  const [streaming, setStreaming] = useState(false);
  const [streamingContent, setStreamingContent] = useState("");
  const [creating, setCreating] = useState(false);
  const [initError, setInitError] = useState<string | null>(null);
  const [lastChatError, setLastChatError] = useState<{ text: string } | null>(null);
  const [fixNotice, setFixNotice] = useState<string | null>(null);
  const [codegen, setCodegen] = useState<{ available: boolean; reason?: string | null } | null>(
    null,
  );

  const abortRef = useRef<AbortController | null>(null);
  const sessionCodeRef = useRef(code);
  // follows the stream only while the reader is at the bottom
  const thread = useStickToBottom();
  // In-flight guard for session creation. MUST be a ref, not the `creating`
  // state: with `creating` in the effect deps, setCreating(true) re-ran the
  // effect, whose cleanup cancelled its own fetch — every attempt self-cancelled.
  const creatingRef = useRef(false);
  // Latest props for the init effect without churning its dependency list.
  const latest = useRef({ code, flowData, apiKeys });
  latest.current = { code, flowData, apiKeys };

  const aiFix = useAiFix({
    onApplied: (fixed) => {
      const applied = onApplyFixedCode(fixed);
      if (applied && session) {
        // Rewrite the session's agent code in place so the conversation
        // (and its replayed history) continues with the fixed code.
        updateConversationCode(session.session_id, fixed)
          .then(() => {
            sessionCodeRef.current = fixed;
            setFixNotice(t("studio.fix.appliedChat"));
            toast(t("studio.fix.appliedToast"));
          })
          .catch((err) => {
            aiFix.reportFixError(
              t("studio.chat.codeSyncFailed", {
                msg: err instanceof Error ? err.message : String(err),
              }),
            );
          });
      }
      return applied;
    },
  });

  useEffect(() => {
    let cancelled = false;
    getCodegenStatus()
      .then((status) => {
        if (!cancelled) {
          setCodegen({ available: status.available, reason: status.reason });
        }
      })
      .catch(() => {
        if (!cancelled) setCodegen({ available: false });
      });
    return () => {
      cancelled = true;
    };
  }, []);

  // Create a session the first time the tab is shown (kept until New session or
  // page unload — the backend session is in-memory).
  useEffect(() => {
    if (!active || session || creatingRef.current || !latest.current.code.trim()) return;
    // NO cancelled/cleanup gating here: the session must survive StrictMode's
    // mount→cleanup→mount cycle, and every earlier cancellation scheme either
    // self-cancelled (creating in deps) or wedged the guard. The ref dedupes
    // concurrent attempts; a resolve after a real unmount is a harmless no-op
    // setState, leaving at worst one orphaned in-memory backend session.
    creatingRef.current = true;
    setCreating(true);
    setInitError(null);
    createConversationSession({
      generated_code: latest.current.code,
      flow_data: latest.current.flowData,
      ...latest.current.apiKeys,
    })
      .then((s) => {
        setSession(s);
        setMessages([]);
        sessionCodeRef.current = latest.current.code;
      })
      .catch((err) => {
        setInitError(t("studio.chat.sessionErr", { msg: err instanceof Error ? err.message : String(err) }));
      })
      .finally(() => {
        creatingRef.current = false;
        setCreating(false);
      });
  }, [active, session, t]);

  // Abort any in-flight streamed turn when the drawer unmounts (e.g. the debug
  // pane is closed mid-stream) so we don't leak a reader or a backend subprocess.
  useEffect(() => () => abortRef.current?.abort(), []);

  const send = async () => {
    const text = input.trim();
    if (!text || !session || streaming || aiFix.isFixing) return;
    setInput("");
    setLastChatError(null);
    setFixNotice(null);
    aiFix.resetFixState();

    const userMsg: ChatMessage = {
      message_id: `u_${Date.now()}`,
      session_id: session.session_id,
      sender: "user",
      content: text,
      timestamp: new Date().toISOString(),
    };
    setMessages((prev) => [...prev, userMsg]);
    setStreaming(true);
    setStreamingContent("");
    const controller = new AbortController();
    abortRef.current = controller;

    await sendChatMessageStream(session.session_id, text, {
      onChunk: (chunk) => setStreamingContent((prev) => prev + chunk),
      onComplete: (final, messageId) => {
        setStreaming(false);
        setStreamingContent("");
        if (final.trim()) {
          setMessages((prev) => [
            ...prev,
            {
              message_id: messageId || `a_${Date.now()}`,
              session_id: session.session_id,
              sender: "agent",
              content: final,
              timestamp: new Date().toISOString(),
            },
          ]);
        }
      },
      onError: (err, partial) => {
        setStreaming(false);
        setStreamingContent("");
        if (partial.trim()) {
          setMessages((prev) => [
            ...prev,
            {
              message_id: `p_${Date.now()}`,
              session_id: session.session_id,
              sender: "agent",
              content: partial,
              timestamp: new Date().toISOString(),
            },
          ]);
        }
        setMessages((prev) => [
          ...prev,
          {
            message_id: `e_${Date.now()}`,
            session_id: session.session_id,
            sender: "agent",
            content: err,
            timestamp: new Date().toISOString(),
            metadata: { error: true },
          },
        ]);
        setLastChatError({ text: err });
      },
      signal: controller.signal,
    });
  };

  const stop = () => abortRef.current?.abort();

  const onInputKeyDown = (e: KeyboardEvent) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      void send();
    }
  };

  const newSession = () => {
    abortRef.current?.abort();
    setSession(null);
    setMessages([]);
    setStreaming(false);
    setStreamingContent("");
    setInput("");
    setLastChatError(null);
    setInitError(null);
    setFixNotice(null);
    aiFix.resetFixState();
  };

  const handleAiFix = () => {
    if (!codegen?.available || !lastChatError || aiFix.isFixing) return;
    setFixNotice(null);
    aiFix.startFix({
      code,
      error: lastChatError.text,
      flow_data: flowData,
      graph_mode: graphMode,
    });
  };

  const stale = !!session && sessionCodeRef.current !== code;
  const fixEnabled = codegen?.available && !!lastChatError && !aiFix.isFixing;
  const fixTitle =
    codegen === null
      ? t("studio.fix.checking")
      : !codegen.available
        ? (codegen.reason ?? t("studio.fix.unavailable"))
        : !lastChatError
          ? t("studio.fix.needsChatFailure")
          : undefined;
  const showFixPanel =
    aiFix.isFixing ||
    aiFix.fixError !== null ||
    aiFix.fixDiagnosis !== null ||
    aiFix.fixApplied;

  return (
    <div className="studio-chat">
      <div className="studio-chat-bar">
        <MessageSquare size={13} style={{ color: "var(--amber)" }} />
        <span className="studio-chat-title">{t("studio.chat.title")}</span>
        <Chip tone="amber">{t("studio.chat.localBadge")}</Chip>
        {session && (
          <Chip tone="muted" className="mono">
            {session.session_id.slice(0, 8)}…
          </Chip>
        )}
        {streaming && (
          <Chip tone="blue" icon="⚡">
            {t("studio.chat.streaming")}
          </Chip>
        )}
        <div className="studio-chat-actions">
          <Btn onClick={handleAiFix} disabled={!fixEnabled} title={fixTitle}>
            {aiFix.isFixing ? (
              <Loader size={12} className="studio-spin" />
            ) : (
              <Sparkles size={12} />
            )}{" "}
            {codegen && !codegen.available
              ? t("studio.fix.unavailable")
              : t("studio.fix.button")}
          </Btn>
          <Btn onClick={newSession} disabled={creating}>
            <RefreshCw size={12} /> {t("studio.chat.newSession")}
          </Btn>
        </div>
      </div>

      {stale && (
        <div className="studio-chat-stale">
          <span>{t("studio.chat.staleBanner")}</span>
        </div>
      )}
      {fixNotice && (
        <div className="studio-chat-notice">
          <Sparkles size={12} style={{ color: "var(--good)" }} /> {fixNotice}
        </div>
      )}

      <div className="studio-chat-thread" ref={thread.ref}>
        {initError && (
          <div className="note" style={{ borderColor: "var(--crit)" }}>
            <span className="i" style={{ color: "var(--crit)" }}>
              [✕]
            </span>
            <span className="mono">{initError}</span>
          </div>
        )}
        {!initError && messages.length === 0 && !streaming && (
          <div className="studio-chat-empty">
            {creating ? t("studio.chat.creating") : t("studio.chat.empty")}
          </div>
        )}
        {messages.map((msg) =>
          msg.sender === "user" ? (
            <div key={msg.message_id} className="msg user">
              <div className="who">{t("studio.chat.you")}</div>
              <div className="bub">{msg.content}</div>
            </div>
          ) : msg.metadata?.error ? (
            <div key={msg.message_id} className="note" style={{ borderColor: "var(--crit)" }}>
              <span className="i" style={{ color: "var(--crit)" }}>
                [✕]
              </span>
              <span className="mono">{msg.content}</span>
            </div>
          ) : (
            <div key={msg.message_id} className="msg agent">
              <div className="who">{t("studio.chat.agent")}</div>
              <div className="bub">{msg.content}</div>
            </div>
          ),
        )}
        {streaming && (
          <div className="msg agent">
            <div className="who">
              {t("studio.chat.agent")} · {t("studio.chat.streaming")}
            </div>
            <div className="bub">
              {streamingContent}
              <span className="caret" />
            </div>
          </div>
        )}
        <JumpToLatest show={!thread.atBottom} onClick={thread.toBottom} />
      </div>

      {showFixPanel && (
        <div className="studio-chat-fixrow">
          <AiFixPanel
            isFixing={aiFix.isFixing}
            fixEvents={aiFix.fixEvents}
            fixError={aiFix.fixError}
            fixDiagnosis={aiFix.fixDiagnosis}
            fixApplied={aiFix.fixApplied}
            appliedMessage={t("studio.fix.appliedChat")}
            onDismissError={aiFix.resetFixState}
            onDismissDiagnosis={aiFix.dismissDiagnosis}
          />
        </div>
      )}

      <div className="studio-chat-inputbar">
        <input
          className="input"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={onInputKeyDown}
          placeholder={t("studio.chat.placeholder")}
          disabled={!session || streaming || aiFix.isFixing}
        />
        {streaming ? (
          <Btn onClick={stop}>
            <Square size={12} /> {t("studio.chat.stop")}
          </Btn>
        ) : (
          <Btn primary onClick={() => void send()} disabled={!session || aiFix.isFixing}>
            <Send size={12} /> {t("studio.chat.send")}
          </Btn>
        )}
      </div>
    </div>
  );
}
