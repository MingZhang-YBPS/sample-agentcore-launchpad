import type { TFunction } from "i18next";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  ChatHttpError,
  connectionFailure,
  errorEventFailure,
  failureText,
  isOutputEvent,
  lastUserIndex,
  latestTurnRetryable,
  requestFailure,
  restoredFailure,
  shouldAutoRetry,
  streamChat,
  waitForRetry,
} from "./chat";

const t = ((key: string, opts?: Record<string, unknown>) =>
  opts ? `${key}:${JSON.stringify(opts)}` : key) as unknown as TFunction;

describe("chat retry: which failures retry", () => {
  it("takes the backend's hint on an error event", () => {
    const failure = errorEventFailure(
      { code: "x", message: "m", retry: { category: "busy", manual: true, auto: true } },
      "detail",
    );
    expect(failure).toEqual({ category: "busy", manual: true, auto: true, detail: "detail" });
  });

  it("never retries by itself without a hint (an older backend)", () => {
    expect(errorEventFailure({ code: "agent.denied" }, "d")).toMatchObject({ category: "config", manual: false, auto: false });
    expect(errorEventFailure({ message: "boom" }, "d")).toMatchObject({ category: "other", manual: true, auto: false });
  });

  it("retries a dropped connection by itself only when nothing was shown", () => {
    expect(connectionFailure(false, "")).toMatchObject({ category: "connection", auto: true, manual: true });
    expect(connectionFailure(true, "")).toMatchObject({ auto: false, manual: true });
  });

  it("classifies a failed request by its status", () => {
    expect(requestFailure(new TypeError("Failed to fetch"), false)).toMatchObject({ category: "connection", auto: true });
    expect(requestFailure(new ChatHttpError(503, "down"), false)).toMatchObject({ category: "busy", auto: true });
    expect(requestFailure(new ChatHttpError(429, "slow"), false)).toMatchObject({ category: "busy", auto: true });
    expect(requestFailure(new ChatHttpError(403, "no"), false)).toEqual({
      category: "config", manual: false, auto: false, detail: "no",
    });
    expect(requestFailure(new Error("odd"), false)).toMatchObject({ category: "other", manual: true, auto: false });
  });

  it("retries automatically once (D-3)", () => {
    const failure = connectionFailure(false, "");
    expect(shouldAutoRetry(failure, 0)).toBe(true);
    expect(shouldAutoRetry(failure, 1)).toBe(false);
    expect(shouldAutoRetry(null, 0)).toBe(false);
  });

  it("counts text, tools, consent asks and denials as output", () => {
    for (const event of ["delta", "tool", "auth_required", "policy_denied"]) expect(isOutputEvent(event)).toBe(true);
    for (const event of ["meta", "phase", "heartbeat", "thinking", "tool_end", "usage"]) expect(isOutputEvent(event)).toBe(false);
  });

  it("restores an interrupted row as a dropped connection", () => {
    expect(restoredFailure("interrupted: client disconnected")).toMatchObject({ category: "connection", detail: "" });
    expect(restoredFailure("RuntimeError: boom")).toMatchObject({ category: "other", manual: true, detail: "RuntimeError: boom" });
  });
});

describe("chat retry: the words (§4.3)", () => {
  it("gives the cause and next step, then the error's own text", () => {
    expect(failureText(t, connectionFailure(false, ""))).toBe("v3.chat.retry.connection");
    expect(failureText(t, { category: "busy", manual: true, auto: true, detail: "ThrottlingException" })).toBe(
      "v3.chat.retry.busy\nThrottlingException",
    );
    expect(failureText(t, { category: "timeout", manual: false, auto: false, timeout_s: 600, detail: "" })).toBe(
      'v3.chat.retry.timeout:{"s":600}',
    );
    expect(failureText(t, { category: "timeout", manual: false, auto: false, detail: "" })).toBe("v3.chat.retry.timeoutUnknown");
  });

  it("shows a configuration error as itself", () => {
    expect(failureText(t, { category: "config", manual: false, auto: false, detail: "Access denied" })).toBe("Access denied");
  });
});

describe("chat retry: the latest turn", () => {
  it("finds where the latest turn starts", () => {
    expect(lastUserIndex([{ kind: "user" }, { kind: "agent" }, { kind: "user" }, { kind: "error" }])).toBe(2);
    expect(lastUserIndex([{ kind: "agent" }])).toBe(-1);
  });

  it("offers a retry for a failed or empty latest turn only", () => {
    expect(latestTurnRetryable([{ kind: "user" }, { kind: "agent" }, { kind: "error" }])).toBe(true);
    expect(latestTurnRetryable([{ kind: "user" }, { kind: "tool" }])).toBe(true);
    expect(latestTurnRetryable([{ kind: "user" }, { kind: "error" }, { kind: "user" }, { kind: "agent" }])).toBe(false);
    expect(latestTurnRetryable([])).toBe(false);
  });
});

describe("chat retry: the request", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("sends retry_of and keeps a refusal's status", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ code: "chat.retry_not_allowed", message: "no", detail: null }), {
        status: 409,
        headers: { "Content-Type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const err = await streamChat("a1", { prompt: "hi", session_id: "s1", retry_of: 7 }).catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ChatHttpError);
    expect((err as ChatHttpError).status).toBe(409);
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/chat/a1");
    expect(JSON.parse(init.body)).toEqual({ prompt: "hi", session_id: "s1", retry_of: 7 });
  });

  it("waits before a retry unless stopped", async () => {
    vi.useFakeTimers();
    try {
      const go = waitForRetry(new AbortController().signal, 2000);
      vi.advanceTimersByTime(2000);
      await expect(go).resolves.toBe(true);
      const stop = new AbortController();
      const stopped = waitForRetry(stop.signal, 2000);
      stop.abort();
      await expect(stopped).resolves.toBe(false);
    } finally {
      vi.useRealTimers();
    }
  });
});
