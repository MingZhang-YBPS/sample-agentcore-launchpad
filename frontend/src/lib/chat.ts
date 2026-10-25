// Chat helpers shared by the V3 and V2 chat pages and the agent try-chat.
import type { TFunction } from "i18next";
import { useCallback, useEffect, useRef, useState } from "react";

import {
  type AgentInfo,
  type ChatHistoryMessage,
  type ChatRequest,
  type ChatStreamPayload,
  errorMessage,
  getJson,
  localizedMessage,
  responseMessage,
  sendJson,
} from "./api";

/**
 * A chat stream frame's payload; a `heartbeat` carries the turn's elapsed time.
 * The working-status fields (chat-streaming §3.1) ride on `meta` / `tool`
 * (`started_at`, epoch ms), `phase` (`phase`) and `tool_end`
 * (`id`, `status`, `duration_ms`).
 */
export type ChatStreamFrame = ChatStreamPayload & {
  elapsed_ms?: number;
  started_at?: number;
  phase?: ChatPhase;
  id?: string | null;
  status?: ToolRunStatus;
  duration_ms?: number | null;
  /** `meta`: the ledger id of this turn's user message — what a retry names */
  user_message_id?: number;
  /** `error`: how the console may retry the turn (chat-streaming §4.2) */
  retry?: ChatRetryHint;
  /** `meta`: the background turn this stream follows — what a resume names (§5.2) */
  turn_id?: string;
  /** `gap`: the first seq the turn's buffer still holds */
  first_seq?: number;
  /** `structured` (SB-1): the agent's parsed answer under its output schema */
  value?: unknown;
};

/**
 * The console chat stream sends a named `heartbeat` (`{elapsed_ms}`) whenever the
 * agent is quiet for a while, so the connection survives long reasoning and slow
 * tools. It is transport, not content: no page renders it as a message.
 */
export function isHeartbeat(event: string): boolean {
  return event === "heartbeat";
}

/**
 * Parse a `text/event-stream` body into `{event, data}` frames. Heartbeats are
 * dropped unless `heartbeats` is set (for a caller that times the turn or watches
 * the connection); SSE comments (`: keep-alive`, the /v1 form) never yield.
 * Aborting `signal` (the stop button, a stalled turn) cancels the body — which
 * closes the connection — and ends the frames. A console turn runs on in the
 * background after that (chat-streaming §5.2): the stop button also calls
 * `stopChatTurn`. `seq` is the frame's SSE `id` (the console chat numbers every
 * event but heartbeats), what `resumeChat` continues after. `T` is the data shape
 * the caller expects — Chat's by default; the Context Layer playground names its own.
 */
export async function* sseEvents<T = ChatStreamFrame>(
  res: Response,
  { heartbeats = false, signal }: { heartbeats?: boolean; signal?: AbortSignal } = {},
): AsyncGenerator<{ event: string; data: T; seq?: number }> {
  const reader = res.body!.getReader();
  const cancel = () => void reader.cancel().catch(() => undefined);
  if (signal?.aborted) cancel();
  signal?.addEventListener("abort", cancel, { once: true });
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    while (!signal?.aborted) {
      let chunk: ReadableStreamReadResult<Uint8Array>;
      try {
        chunk = await reader.read();
      } catch (err) {
        // the same signal on the fetch rejects the pending read: that is a stop, not a failure
        if (signal?.aborted) break;
        throw err;
      }
      const { done, value } = chunk;
      if (done || signal?.aborted) break;
      buffer += decoder.decode(value, { stream: true });
      const frames = buffer.split("\n\n");
      buffer = frames.pop() ?? "";
      for (const frame of frames) {
        if (signal?.aborted) return;
        let event = "message";
        let data = "";
        let seq: number | undefined;
        for (const line of frame.split("\n")) {
          if (line.startsWith("event:")) event = line.slice(6).trim();
          if (line.startsWith("data:")) data += line.slice(5).trim();
          if (line.startsWith("id:")) {
            const id = Number(line.slice(3).trim());
            if (Number.isInteger(id)) seq = id;
          }
        }
        if (!data || (!heartbeats && isHeartbeat(event))) continue;
        const parsed = { event, data: JSON.parse(data) as T };
        yield seq === undefined ? parsed : { ...parsed, seq };
      }
    }
  } finally {
    signal?.removeEventListener("abort", cancel);
  }
}

/** A managed Harness (deployed or imported) has no session-stop operation. */
export function isHarnessAgent(agent: AgentInfo | undefined): boolean {
  if (!agent) return false;
  if (agent.method === "harness") return true;
  const discovery = agent.spec.discovery as { resource_type?: string } | undefined;
  return agent.method === "discovered_runtime" && discovery?.resource_type === "harness";
}

/**
 * What the console can truthfully say about an agent's AgentCore Memory.
 *
 * The console never writes memory itself — the deployed agent does, driven by
 * `spec.memory` for the methods Launchpad renders or configures. BYOC code and
 * discovered agents decide on their own, so their spec says nothing reliable.
 */
export type AgentMemoryState = "on" | "off" | "agent-defined";

export function agentMemoryState(agent: AgentInfo | undefined): AgentMemoryState {
  if (!agent || agent.method === "byoc" || agent.method === "discovered_runtime") {
    return "agent-defined";
  }
  const memory = agent.spec.memory as { short_term?: boolean; long_term?: boolean } | undefined;
  if (!memory) return "agent-defined";
  return memory.short_term || memory.long_term ? "on" : "off";
}

/** Agents the chat console can talk to: active and invoke-eligible. */
export function chatEligible(agents: AgentInfo[]): AgentInfo[] {
  return agents.filter((a) => a.status === "active" && a.invoke_capability.eligible);
}

/**
 * "Invoke as me" on a JWT-inbound agent. `null` = auto: the field is omitted
 * and the backend presents the member's pool JWT when there is one, else the
 * workspace M2M token. Only an explicit toggle sends `as_user` — an explicit
 * `true` without a pool sign-in is refused (409 `chat.as_user_unavailable`)
 * rather than silently becoming the machine identity.
 */
export type AsUserChoice = boolean | null;

/** The `as_user` request field for a choice (undefined ⇒ omitted, auto). */
export function asUserField(jwtAgent: boolean, choice: AsUserChoice): boolean | undefined {
  return jwtAgent && choice !== null ? choice : undefined;
}

/** What the checkbox shows: the explicit choice, else what auto resolves to
 *  (the member's own JWT exactly when signed in through the pool). */
export function asUserChecked(choice: AsUserChoice, signedIn: boolean): boolean {
  return choice ?? signedIn;
}

/**
 * The text of a chat `error` event: the console copy for its code, and — for a
 * reply the console could not read (`runtime.unrecognized_reply`) — the start of
 * what the runtime actually sent, so the operator sees the format instead of a
 * blank turn.
 */
export function chatErrorText(data: ChatStreamPayload, fallback: string): string {
  const text = localizedMessage(data.code ?? "", data.message ?? fallback);
  const sample = data.detail?.sample;
  return typeof sample === "string" && sample ? `${text}\n${sample}` : text;
}

// ---------------------------------------------------------------------------
// Working status (chat-streaming S2): what the agent is doing while a turn runs.

/** `starting`: invoking the runtime; `waiting_model`: it answered, no text yet;
 *  `streaming`: text is arriving. */
export type ChatPhase = "starting" | "waiting_model" | "streaming";

export type ToolRunStatus = "running" | "success" | "error" | "denied";

/** One tool call of the running turn. `startedAt` is on the local clock. */
export interface ToolRun {
  id: string;
  name: string;
  status: ToolRunStatus;
  startedAt: number;
  durationMs?: number | null;
}

export interface TurnStatus {
  /** null until the stream's first `phase` (a stream without status events —
   *  an older backend — never sends one; then `meta` is all there is to go by) */
  phase: ChatPhase | null;
  /** `meta` arrived: the console reached the agent's turn */
  connected: boolean;
  /** the model is reasoning (a `thinking` since the last text or tool event) */
  reasoning: boolean;
  /** a text bubble is open: the placeholder has become the answer */
  textOpen: boolean;
  tools: ToolRun[];
  /** local clock (ms) at which the turn's elapsed time was 0 — re-based on every
   *  heartbeat's `elapsed_ms`, so a throttled background tab does not drift */
  base: number;
  /** local clock (ms) of the last frame of any kind, heartbeats included */
  lastEventAt: number;
}

/** No frame for this long: the placeholder warns the connection may be gone. */
export const STALL_WARN_MS = 30_000;
/** No frame for this long: the page aborts the turn itself (before CloudFront's
 *  60 s cut), so it ends with a definite state instead of hanging. */
export const STALL_ABORT_MS = 45_000;

export function startTurnStatus(now: number): TurnStatus {
  return {
    phase: null,
    connected: false,
    reasoning: false,
    textOpen: false,
    tools: [],
    base: now,
    lastEventAt: now,
  };
}

/** Fold one stream frame into the turn's status (pure: `now` is the local clock). */
export function reduceTurnStatus(
  status: TurnStatus,
  event: string,
  data: ChatStreamFrame,
  now: number,
): TurnStatus {
  const next: TurnStatus = { ...status, lastEventAt: now };
  switch (event) {
    case "meta":
      next.connected = true;
      return next;
    case "heartbeat":
      if (typeof data.elapsed_ms === "number") next.base = now - data.elapsed_ms;
      return next;
    case "phase":
      if (data.phase) next.phase = data.phase;
      return next;
    case "thinking":
      next.reasoning = true;
      return next;
    case "tool":
      next.reasoning = false;
      next.textOpen = false;
      next.tools = [
        ...status.tools,
        {
          id: data.id ?? `tool-${status.tools.length + 1}`,
          name: data.name ?? "tool",
          status: "running",
          startedAt: now,
        },
      ];
      return next;
    case "tool_end":
      next.tools = status.tools.map((run) =>
        run.id === data.id && run.status === "running"
          ? {
              ...run,
              status: data.status && data.status !== "running" ? data.status : "success",
              durationMs: data.duration_ms ?? now - run.startedAt,
            }
          : run,
      );
      return next;
    case "delta":
      next.reasoning = false;
      next.textOpen = true;
      if (next.phase !== null) next.phase = "streaming";
      return next;
    case "done":
    case "error":
      // a call the stream never ended is over with the turn
      next.tools = status.tools.map((run) =>
        run.status === "running"
          ? { ...run, status: event === "error" ? "error" : "success", durationMs: now - run.startedAt }
          : run,
      );
      return next;
    default:
      return next;
  }
}

export function turnElapsedMs(status: TurnStatus, now: number): number {
  return Math.max(0, now - status.base);
}

/** What the placeholder says. `null`: nothing to show (the answer is streaming). */
export type TurnActivity =
  | { kind: "connecting" | "thinking" | "reasoning" | "stalled" }
  | { kind: "tool"; tool: string; ms: number };

export function turnActivity(status: TurnStatus, now: number): TurnActivity | null {
  if (now - status.lastEventAt >= STALL_WARN_MS) return { kind: "stalled" };
  const running = status.tools.filter((run) => run.status === "running");
  if (running.length) {
    const run = running[running.length - 1];
    return { kind: "tool", tool: run.name, ms: now - run.startedAt };
  }
  if (status.textOpen) return null;
  if (status.phase === "starting" || (status.phase === null && !status.connected)) {
    return { kind: "connecting" };
  }
  return { kind: status.reasoning ? "reasoning" : "thinking" };
}

/** True when the turn has been silent long enough to give up on it. */
export function turnStalled(status: TurnStatus, now: number): boolean {
  return now - status.lastEventAt >= STALL_ABORT_MS;
}

/** A tool row's run in the turn status, by the `tool` event's id. */
export function toolRunById(status: TurnStatus | null, id: string | undefined): ToolRun | undefined {
  return id ? status?.tools.find((run) => run.id === id) : undefined;
}

/** Whole seconds, for the timers. */
export function seconds(ms: number): number {
  return Math.floor(ms / 1000);
}

/** One decimal under a minute (`2.4`), whole seconds after. */
export function toolSeconds(ms: number): string {
  return ms < 60_000 ? (ms / 1000).toFixed(1) : String(Math.round(ms / 1000));
}

/** The placeholder's words for what the agent is doing (chat-streaming §3.2). */
export function activityLabel(t: TFunction, activity: TurnActivity): string {
  if (activity.kind === "tool") return t("v3.chat.status.tool", { tool: activity.tool });
  return t(`v3.chat.status.${activity.kind}`);
}

/** A tool row's state: `running 8s`, `2.4s`, `failed`, `denied by policy`. */
export function toolRunLabel(t: TFunction, run: ToolRun, now: number): string {
  if (run.status === "running") return t("v3.chat.tool.running", { s: seconds(now - run.startedAt) });
  if (run.status === "error") return t("v3.chat.tool.failed");
  if (run.status === "denied") return t("v3.chat.tool.denied");
  return run.durationMs == null ? "" : t("v3.chat.tool.done", { s: toolSeconds(run.durationMs) });
}

/** The glyph in front of a tool row: ⟳ running, ✓ done, ✗ failed, ⛔ denied. */
export const TOOL_RUN_GLYPH: Record<ToolRun["status"], string> = {
  running: "⟳",
  success: "✓",
  error: "✗",
  denied: "⛔",
};

/** Re-render once a second while `active`, returning the local clock. */
export function useNow(active: boolean, everyMs = 1000): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!active) return;
    setNow(Date.now());
    const timer = window.setInterval(() => setNow(Date.now()), everyMs);
    return () => window.clearInterval(timer);
  }, [active, everyMs]);
  return now;
}

/** Why a turn was aborted from the page: the stop button, or a silent stream. */
export type TurnStopReason = "stop" | "stalled";

export function turnStopReason(signal: AbortSignal): TurnStopReason | null {
  if (!signal.aborted) return null;
  return signal.reason === "stalled" ? "stalled" : "stop";
}

/**
 * One chat turn's working status, shared by the three chat surfaces. `begin()`
 * returns the signal to read the stream with (`sseEvents(res, {heartbeats: true,
 * signal})`); `feed()` folds each frame in and returns the new status; `stop()`
 * aborts the turn (the stop button); `end()` clears it. A turn silent for
 * `STALL_ABORT_MS` aborts itself with reason `stalled`.
 */
export function useChatTurn() {
  const [status, setStatus] = useState<TurnStatus | null>(null);
  const current = useRef<TurnStatus | null>(null);
  const controller = useRef<AbortController | null>(null);
  const now = useNow(status !== null);

  const begin = useCallback((): AbortSignal => {
    controller.current = new AbortController();
    current.current = startTurnStatus(Date.now());
    setStatus(current.current);
    return controller.current.signal;
  }, []);
  const feed = useCallback((event: string, data: ChatStreamFrame): TurnStatus | null => {
    if (!current.current) return null;
    current.current = reduceTurnStatus(current.current, event, data, Date.now());
    setStatus(current.current);
    return current.current;
  }, []);
  const stop = useCallback((reason: TurnStopReason = "stop") => {
    controller.current?.abort(reason);
  }, []);
  // a new connection to the same turn (chat-streaming §5.2): its status stays, the
  // silence clock restarts, and the new signal is the one `stop()` aborts
  const reconnect = useCallback((): AbortSignal => {
    controller.current = new AbortController();
    if (current.current) {
      current.current = { ...current.current, lastEventAt: Date.now() };
      setStatus(current.current);
    }
    return controller.current.signal;
  }, []);
  const end = useCallback(() => {
    controller.current = null;
    current.current = null;
    setStatus(null);
  }, []);

  useEffect(() => {
    if (status && turnStalled(status, now)) stop("stalled");
  }, [status, now, stop]);

  return { status, now, begin, feed, stop, reconnect, end };
}

// ---------------------------------------------------------------------------
// Retry (chat-streaming S3): a failed turn offers a retry button, and a transient
// failure of a turn that produced nothing is retried once by itself.

/** `connection`: the link to the agent dropped; `timeout`: the agent used its whole
 *  budget; `busy`: throttling / 5xx / an upstream hiccup; `config`: a permission,
 *  parameter or limit error a replay cannot fix; `other`: anything unclassified. */
export type RetryCategory = "connection" | "timeout" | "busy" | "config" | "other";

/** The `retry` hint on a console `error` event (set by the backend). */
export interface ChatRetryHint {
  category: RetryCategory;
  /** the retry button is offered */
  manual: boolean;
  /** retried once without asking: nothing was shown and the failure is transient */
  auto: boolean;
  /** `timeout`: the agent's execution budget */
  timeout_s?: number;
}

/** A failed turn as the thread shows it: why, the raw detail, and what it offers. */
export interface TurnFailure extends ChatRetryHint {
  /** the error's own text (the agent's or the request's), shown under the advice */
  detail: string;
}

/** `POST /api/chat/{agent_id}` with `retry_of`: this turn retries a failed one. */
export type ChatTurnRequest = ChatRequest & { retry_of?: number };

/** Automatic retries per turn (chat-streaming D-3). */
export const AUTO_RETRIES = 1;
/** The pause before an automatic retry. */
export const RETRY_DELAY_MS = 2_000;

/** Events after which a replay could repeat what the user saw or a tool's side
 *  effect — the backend's `OUTPUT_EVENTS`. */
export function isOutputEvent(event: string): boolean {
  return (
    event === "delta" || event === "tool" || event === "auth_required" || event === "policy_denied" ||
    event === "approval_required"
  );
}

/** A non-2xx answer to the chat request, with its HTTP status. */
export class ChatHttpError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
  }
}

/**
 * Open a console chat turn and return the raw SSE response (read it with
 * `sseEvents`). Like `chatApi.stream`, but a refusal keeps its HTTP status, so a
 * 429 / 5xx can be retried and a 4xx cannot.
 */
export async function streamChat(agentId: string, body: ChatTurnRequest, signal?: AbortSignal): Promise<Response> {
  const res = await fetch(`/api/chat/${encodeURIComponent(agentId)}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal,
  });
  if (!res.ok) throw new ChatHttpError(res.status, await responseMessage(res));
  return res;
}

/** An `error` event's failure. An older backend sends no hint: the user may retry,
 *  nothing retries by itself. */
export function errorEventFailure(data: ChatStreamFrame, detail: string): TurnFailure {
  const hint = data.retry ?? { category: data.code ? "config" : "other", manual: !data.code, auto: false };
  return { ...hint, detail };
}

/** The stream dropped (network, no `done`, a silent turn the page gave up on). */
export function connectionFailure(produced: boolean, detail: string): TurnFailure {
  return { category: "connection", manual: true, auto: !produced, detail };
}

/** The request itself failed: a network error, or a refusal by status (429 / 5xx
 *  are transient, any other status is final). */
export function requestFailure(err: unknown, produced: boolean): TurnFailure {
  const detail = errorMessage(err);
  if (err instanceof TypeError) return connectionFailure(produced, detail);
  if (err instanceof ChatHttpError) {
    const transient = err.status === 429 || err.status >= 500;
    return transient
      ? { category: "busy", manual: true, auto: !produced, detail }
      : { category: "config", manual: false, auto: false, detail };
  }
  return { category: "other", manual: true, auto: false, detail };
}

/** A restored error row: an `interrupted: …` row is a dropped connection (or a turn
 *  past its time limit); any other row is offered as a retry (the backend still
 *  refuses a turn it cannot retry). A turn the user stopped: `isStoppedRow`. */
export function restoredFailure(text: string): TurnFailure {
  if (text.startsWith(LIMIT_ROW_PREFIX)) return { category: "timeout", manual: false, auto: false, detail: "" };
  return text.startsWith("interrupted: ")
    ? { category: "connection", manual: true, auto: false, detail: "" }
    : { category: "other", manual: true, auto: false, detail: text };
}

/** The ledger row of a turn the stop button ended (chat-streaming §5.2). */
export const STOPPED_ROW = "interrupted: stopped by the user";
/** The start of the ledger row of a turn that ran past its time limit. */
const LIMIT_ROW_PREFIX = "interrupted: the turn ran past";

export function isStoppedRow(text: string): boolean {
  return text === STOPPED_ROW;
}

/** The thread's words for a failure (chat-streaming §4.3): the cause and the next
 *  step, then the error's own text; a `config` / `other` error is its own text. */
export function failureText(t: TFunction, failure: TurnFailure): string {
  const advice =
    failure.category === "connection"
      ? t("v3.chat.retry.connection")
      : failure.category === "busy"
        ? t("v3.chat.retry.busy")
        : failure.category === "timeout"
          ? failure.timeout_s
            ? t("v3.chat.retry.timeout", { s: failure.timeout_s })
            : t("v3.chat.retry.timeoutUnknown")
          : "";
  if (!advice) return failure.detail;
  return failure.detail ? `${advice}\n${failure.detail}` : advice;
}

/** May this failure be retried by itself now (`attempt` retries already made)? */
export function shouldAutoRetry(failure: TurnFailure | null, attempt: number): boolean {
  return !!failure && failure.auto && attempt < AUTO_RETRIES;
}

/** Wait before an automatic retry; false when `signal` aborted the wait (Stop). */
export function waitForRetry(signal: AbortSignal, ms = RETRY_DELAY_MS): Promise<boolean> {
  return new Promise((resolve) => {
    if (signal.aborted) return resolve(false);
    const timer = setTimeout(() => {
      signal.removeEventListener("abort", onAbort);
      resolve(true);
    }, ms);
    const onAbort = () => {
      clearTimeout(timer);
      resolve(false);
    };
    signal.addEventListener("abort", onAbort, { once: true });
  });
}

/** Index of the last user message (where the latest turn starts), or -1. */
export function lastUserIndex(messages: { kind: string }[]): number {
  for (let i = messages.length - 1; i >= 0; i -= 1) if (messages[i].kind === "user") return i;
  return -1;
}

/**
 * Whether the thread's latest turn can be retried: it failed (an error item after
 * its user message) or answered nothing at all. Only the latest turn — the backend
 * refuses any other (409 `chat.retry_not_allowed`).
 */
export function latestTurnRetryable(messages: { kind: string }[]): boolean {
  const start = lastUserIndex(messages);
  if (start < 0) return false;
  const rest = messages.slice(start + 1);
  return rest.some((m) => m.kind === "error") || !rest.some((m) => m.kind === "agent");
}

// ---------------------------------------------------------------------------
// Resume (chat-streaming S4, §5.2): a console turn runs on in the background when the
// connection drops; the page follows it again from the last event it received.

/** Pauses before each reconnect attempt; past the last one the turn is given up
 *  on and the S3 retry takes over. */
export const RESUME_DELAYS_MS = [2_000, 4_000, 8_000] as const;

/** The pause before reconnect attempt `attempt` (0-based), or null: no more tries. */
export function resumeDelay(attempt: number): number | null {
  return attempt >= 0 && attempt < RESUME_DELAYS_MS.length ? RESUME_DELAYS_MS[attempt] : null;
}

/** A session's turn still answering, as `GET …/history` names it. */
export interface RunningTurn {
  turn_id: string;
  /** the turn's user row: the thread is shown up to it, the rest is replayed */
  user_message_id: number;
}

export interface ChatHistory {
  messages: ChatHistoryMessage[];
  /** absent from an older backend */
  running_turn?: RunningTurn | null;
}

const enc = encodeURIComponent;

/** `GET /api/chat/{agent_id}/history`, with the session's running turn. */
export function chatHistory(agentId: string, sessionId: string): Promise<ChatHistory> {
  return getJson<ChatHistory>(`/api/chat/${enc(agentId)}/history?session_id=${enc(sessionId)}`);
}

/**
 * Follow a running turn again: the events after `after` (the last `seq` received;
 * 0 = the whole turn), then the live ones. Read it with `sseEvents`. A refusal keeps
 * its HTTP status: 404 (`isTurnGone`) means the turn is no longer held — it ended a
 * while ago, or the server restarted — and the ledger has what was saved.
 */
export async function resumeChat(agentId: string, turnId: string, after: number, signal?: AbortSignal): Promise<Response> {
  const res = await fetch(`/api/chat/${enc(agentId)}/turns/${enc(turnId)}/events?after=${after}`, { signal });
  if (!res.ok) throw new ChatHttpError(res.status, await responseMessage(res));
  return res;
}

export function isTurnGone(err: unknown): boolean {
  return err instanceof ChatHttpError && err.status === 404;
}

/** The stop button: end the background turn itself, not just this page's connection. */
export function stopChatTurn(agentId: string, turnId: string): Promise<{ turn_id: string; stopped: boolean }> {
  return sendJson(`/api/chat/${enc(agentId)}/turns/${enc(turnId)}/stop`, "POST");
}

/** The history rows a page shows while it replays the running turn: everything up to
 *  and including the turn's user row (the rest arrives again from the replay). */
export function rowsBeforeTurn<T extends { id?: number }>(rows: T[], turn: RunningTurn | null | undefined): T[] {
  if (!turn) return rows;
  const at = rows.findIndex((row) => row.id === turn.user_message_id);
  return at < 0 ? rows : rows.slice(0, at + 1);
}
