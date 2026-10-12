import { SendHorizontal } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import { ApiError, errorMessage, type FeedbackVerdict, localizedMessage } from "../lib/api";
import { sseEvents } from "../lib/chat";
import { type ShareInfo, shareApi } from "../lib/share";
import { V2Lang } from "../v2/Lang";
import { type ChatMessage, Thread } from "../v2/pages/chat/Thread";
import "../v2/v2.css";
import "../v2/pages/chat/chat.css";
import "./share.css";

/**
 * The account-free chat page a share link opens (T14). Standalone on purpose: it
 * is rendered from `main.tsx` before `AuthGate`, `App` and the V2 shell exist, so
 * it has no console navigation, no session and no workspace selection — the link
 * in the URL is the only thing it knows, and the backend decides the rest.
 */
export function SharePage({ token }: { token: string }) {
  const { t } = useTranslation();
  // `/s/<token>?embed=1` (T30): the same page for an <iframe> — no header, no language
  // switcher, edge-to-edge. Decided from the URL alone so the loading and "gone"
  // states are chromeless too. It changes rendering only; the link's limits are the same.
  const embed = new URLSearchParams(window.location.search).get("embed") === "1";
  const pageClass = embed ? "v2 share-page share-embed" : "v2 share-page";
  const [info, setInfo] = useState<ShareInfo | null>(null);
  const [gone, setGone] = useState(false);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const sessionRef = useRef<string | null>(null);
  const [sessionId, setSessionId] = useState<string | null>(null);
  // the answer a thumbs-down comment box is open for
  const [commenting, setCommenting] = useState<number | null>(null);
  const [comment, setComment] = useState("");

  useEffect(() => {
    document.body.classList.add("v2-body");
    return () => document.body.classList.remove("v2-body");
  }, []);

  useEffect(() => {
    let live = true;
    shareApi
      .info(token)
      .then((result) => live && setInfo(result))
      .catch(() => live && setGone(true));
    return () => {
      live = false;
    };
  }, [token]);

  useEffect(() => {
    if (info) document.title = info.agent.display_name;
  }, [info]);

  const send = async () => {
    const prompt = input.trim();
    if (!prompt || busy) return;
    setInput("");
    setBusy(true);
    setMessages((m) => [...m, { kind: "user", text: prompt }]);
    let agentOpen = false;
    try {
      const res = await shareApi.chat(token, { prompt, session_id: sessionRef.current });
      for await (const { event, data } of sseEvents(res)) {
        if (event === "meta" && data.session_id) {
          sessionRef.current = data.session_id;
          setSessionId(data.session_id);
        } else if (event === "delta") {
          const open = agentOpen;
          setMessages((m) => {
            const next = [...m];
            const last = next[next.length - 1];
            if (open && last?.kind === "agent") {
              next[next.length - 1] = { ...last, text: last.text + (data.text ?? "") };
            } else {
              next.push({ kind: "agent", text: data.text ?? "", streaming: true });
            }
            return next;
          });
          agentOpen = true;
        } else if (event === "saved") {
          const messageId = data.message_id;
          setMessages((m) => {
            const next = [...m];
            for (let i = next.length - 1; i >= 0; i--) {
              if (next[i].kind === "agent") {
                if (next[i].id == null) next[i] = { ...next[i], id: messageId };
                break;
              }
            }
            return next;
          });
        } else if (event === "error") {
          const message = localizedMessage(data.code ?? "", data.message ?? "");
          setMessages((m) => [...m, { kind: "error", text: message }]);
        }
      }
    } catch (err) {
      if (err instanceof ApiError && err.code === "share.not_found") setGone(true);
      else setMessages((m) => [...m, { kind: "error", text: errorMessage(err) }]);
    } finally {
      setMessages((m) => m.map((msg) => (msg.streaming ? { ...msg, streaming: false } : msg)));
      setBusy(false);
    }
  };

  const rate = async (index: number, verdict: FeedbackVerdict | "none", note?: string) => {
    const target = messages[index];
    if (!sessionId || target?.kind !== "agent" || target.id == null) return;
    const previous = target.verdict ?? null;
    const apply = (value: FeedbackVerdict | null) =>
      setMessages((m) => m.map((msg, i) => (i === index ? { ...msg, verdict: value } : msg)));
    apply(verdict === "none" ? null : verdict);
    setCommenting(verdict === "down" && note === undefined ? index : null);
    try {
      await shareApi.rate(token, {
        session_id: sessionId,
        message_id: target.id,
        verdict,
        ...(note ? { comment: note } : {}),
      });
    } catch (err) {
      apply(previous);
      setCommenting(null);
      setMessages((m) => [...m, { kind: "error", text: `${t("sharePage.rateFailed")}: ${errorMessage(err)}` }]);
    }
  };

  const sendComment = async () => {
    if (commenting == null) return;
    const index = commenting;
    const note = comment.trim();
    setComment("");
    if (note) await rate(index, "down", note);
    setCommenting(null);
  };

  if (gone) {
    return (
      <div className={`${pageClass} share-gone`} data-testid="share-gone">
        <h1>{t("sharePage.notFoundTitle")}</h1>
        <p>{t("sharePage.notFoundBody")}</p>
      </div>
    );
  }
  if (!info) {
    return (
      <div className={`${pageClass} share-gone`} role="status">
        {t("sharePage.loading")}
      </div>
    );
  }

  const name = info.agent.display_name;
  return (
    <div className={pageClass} data-testid="share-page">
      {!embed && (
        <header className="share-head">
          <h1>{name}</h1>
          <div className="share-head-end">
            {info.expires_at && (
              <span className="share-expiry">
                {t("sharePage.expiresOn", { date: new Date(info.expires_at).toLocaleDateString() })}
              </span>
            )}
            <V2Lang />
          </div>
        </header>
      )}
      <main className="share-body">
        <Thread
          messages={messages}
          userLabel={t("sharePage.you")}
          agentLabel={name}
          restoring={false}
          onRate={(index, verdict) => void rate(index, verdict)}
        />
        {commenting != null && (
          <form
            className="share-comment"
            onSubmit={(e) => {
              e.preventDefault();
              void sendComment();
            }}
          >
            <input
              className="v2-input"
              value={comment}
              maxLength={1000}
              onChange={(e) => setComment(e.target.value)}
              placeholder={t("sharePage.commentPlaceholder")}
              aria-label={t("sharePage.commentPlaceholder")}
            />
            <button type="submit" className="v2-btn">
              {t("sharePage.commentSend")}
            </button>
          </form>
        )}
      </main>
      <form
        className="share-composer"
        onSubmit={(e) => {
          e.preventDefault();
          void send();
        }}
      >
        <textarea
          className="v2-textarea"
          rows={2}
          value={input}
          maxLength={8000}
          disabled={busy}
          placeholder={t("sharePage.placeholder")}
          aria-label={t("sharePage.placeholder")}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
              e.preventDefault();
              void send();
            }
          }}
          data-testid="share-input"
        />
        <button type="submit" className="v2-btn primary" disabled={busy || !input.trim()} data-testid="share-send">
          <SendHorizontal size={14} aria-hidden="true" />
          {t("sharePage.send")}
        </button>
      </form>
    </div>
  );
}
