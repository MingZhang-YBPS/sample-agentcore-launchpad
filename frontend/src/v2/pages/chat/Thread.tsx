import { Database, MessagesSquare, RotateCcw, ThumbsDown, ThumbsUp, Wrench } from "lucide-react";
import { useTranslation } from "react-i18next";

import { MessageAttachments } from "../../../components/chat/AttachmentViews";
import { JumpToLatest } from "../../../components/JumpToLatest";
import { Markdown } from "../../../components/Markdown";
import type { FeedbackVerdict } from "../../../lib/api";
import { TOOL_RUN_GLYPH, toolRunLabel } from "../../../lib/chat";
import { useFollowOwnTurns, useStickToBottom } from "../../../lib/useStickToBottom";
import { Alert, Button, Spin, Tag } from "../../ui";
import { AuthCard } from "./AuthCard";
import { PolicyDenyCard } from "./PolicyDenyCard";
import { type ChatMessage, latestRetryTarget, retryPromptFor } from "./messages";

export type { ChatMessage } from "./messages";

/** The conversation: user / agent bubbles, tool calls, consent and policy-deny
 *  cards, memory writes and errors. */
export function Thread({
  messages,
  userLabel,
  agentLabel,
  restoring,
  onRate,
  onRetry,
  retryDisabled = false,
  onRetryTurn,
  now,
}: {
  messages: ChatMessage[];
  userLabel: string;
  agentLabel: string;
  restoring: boolean;
  /** thumbs handler (T15); omitted ⇒ no controls. `none` withdraws a verdict. */
  onRate?: (index: number, verdict: FeedbackVerdict | "none") => void;
  /** re-send a prompt (an auth card's retry) */
  onRetry?: (prompt: string) => void;
  retryDisabled?: boolean;
  /** retry the latest failed turn (chat-streaming §4.1): a Retry button inside its
   *  error item — or, when that turn carried files, the note that it is not retried */
  onRetryTurn?: () => void;
  /** the running turn's clock (a running tool row's elapsed time); omitted ⇒ render time */
  now?: number;
}) {
  const { t } = useTranslation();
  // follows the stream only while the reader is at the bottom
  const thread = useStickToBottom();
  useFollowOwnTurns(messages.length, messages[messages.length - 1]?.kind === "user", thread.toBottom);
  const retryTarget = onRetryTurn ? latestRetryTarget(messages) : null;

  return (
    <div className="v2-chat-thread" ref={thread.ref} data-testid="thread">
      {restoring && messages.length === 0 && <Spin />}
      {!restoring && messages.length === 0 && (
        <div className="v2-chat-empty">
          <MessagesSquare size={36} aria-hidden="true" />
          <div>{t("v2.chat.emptyThread")}</div>
        </div>
      )}
      {messages.map((msg, i) =>
        msg.kind === "user" ? (
          <div key={i} className="v2-chat-msg user">
            <div className="v2-chat-who">{userLabel}</div>
            <div className="v2-chat-bub">
              {msg.text && <div className="v2-chat-text">{msg.text}</div>}
              <MessageAttachments files={msg.attachments} />
            </div>
          </div>
        ) : msg.kind === "agent" ? (
          <div key={i} className="v2-chat-msg agent">
            <div className="v2-chat-who">
              {agentLabel}
              {msg.curated && (
                <Tag tone="orange" title={t("v2.chat.curatedHint")}>
                  {t("v2.chat.curated")}
                </Tag>
              )}
              {msg.streaming && (
                <Tag tone="blue" dot>
                  {t("v2.chat.streaming")}
                </Tag>
              )}
            </div>
            <div className="v2-chat-bub">
              <Markdown text={msg.text} />
              {msg.streaming && <span className="v2-chat-caret" />}
            </div>
            {onRate && msg.id != null && !msg.streaming && (
              <div className="v2-chat-thumbs" role="group" aria-label={t("v2.chat.rateAnswer")}>
                {(["up", "down"] as const).map((verdict) => {
                  const Icon = verdict === "up" ? ThumbsUp : ThumbsDown;
                  const active = msg.verdict === verdict;
                  return (
                    <button
                      key={verdict}
                      type="button"
                      className={active ? `v2-chat-thumb on ${verdict}` : "v2-chat-thumb"}
                      aria-pressed={active}
                      title={t(verdict === "up" ? "v2.chat.thumbUp" : "v2.chat.thumbDown")}
                      onClick={() => onRate(i, active ? "none" : verdict)}
                      data-testid={`thumb-${verdict}`}
                    >
                      <Icon size={14} aria-hidden="true" />
                    </button>
                  );
                })}
              </div>
            )}
          </div>
        ) : msg.kind === "tool" ? (
          <div key={i} className="v2-chat-tool" data-run={msg.run?.status} data-testid="tool-call">
            {msg.run ? (
              <span className="mono" aria-hidden="true">{TOOL_RUN_GLYPH[msg.run.status]}</span>
            ) : (
              <Wrench size={14} aria-hidden="true" />
            )}
            <span className="mono">{msg.name}</span>
            {msg.run ? (
              <span className="v2-muted">{toolRunLabel(t, msg.run, now ?? Date.now())}</span>
            ) : (
              <Tag tone="green">{t("v2.chat.toolCalled")}</Tag>
            )}
          </div>
        ) : msg.kind === "auth" && msg.auth ? (
          <AuthCard
            key={i}
            ask={msg.auth}
            retryPrompt={retryPromptFor(messages, i)}
            retryDisabled={retryDisabled || !onRetry}
            onRetry={(prompt) => onRetry?.(prompt)}
          />
        ) : msg.kind === "policy" && msg.policy ? (
          <PolicyDenyCard key={i} deny={msg.policy} />
        ) : msg.kind === "memory" ? (
          <div key={i} className="v2-chat-memline">
            <Database size={13} aria-hidden="true" />
            <span>{t("v2.chat.memorySaved")}</span>
            <code>memory.create_event</code>
          </div>
        ) : (
          <div key={i} className="v2-chat-error">
            <Alert tone="error">
              {msg.text}
              {retryTarget?.index === i &&
                (retryTarget.attachments ? (
                  <div className="v2-muted" style={{ marginTop: 6 }} data-testid="chat-retry-attachments">
                    {t("v3.chat.retry.attachments")}
                  </div>
                ) : (
                  <div style={{ marginTop: 6 }}>
                    <Button size="sm" onClick={onRetryTurn} testId="chat-retry">
                      <RotateCcw size={12} aria-hidden="true" />
                      {t("v3.chat.retry.button")}
                    </Button>
                  </div>
                ))}
            </Alert>
          </div>
        ),
      )}
      <JumpToLatest show={!thread.atBottom} onClick={thread.toBottom} />
    </div>
  );
}
