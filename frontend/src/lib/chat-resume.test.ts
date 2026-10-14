import { afterEach, describe, expect, it, vi } from "vitest";

import {
  ChatHttpError,
  chatHistory,
  isStoppedRow,
  isTurnGone,
  RESUME_DELAYS_MS,
  restoredFailure,
  resumeChat,
  resumeDelay,
  rowsBeforeTurn,
  sseEvents,
  stopChatTurn,
  STOPPED_ROW,
} from "./chat";

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

describe("chat resume: the wire", () => {
  it("reads each event's seq from its SSE id; heartbeats carry none", async () => {
    const wire =
      'event: meta\ndata: {"session_id":"s1","turn_id":"t1"}\nid: 1\n\n' +
      'event: heartbeat\ndata: {"elapsed_ms":15000}\n\n' +
      'id: 2\nevent: delta\ndata: {"text":"hi"}\n\n'; // any field order
    const frames = [];
    for await (const frame of sseEvents(new Response(wire), { heartbeats: true })) frames.push(frame);
    expect(frames).toEqual([
      { event: "meta", data: { session_id: "s1", turn_id: "t1" }, seq: 1 },
      { event: "heartbeat", data: { elapsed_ms: 15000 } },
      { event: "delta", data: { text: "hi" }, seq: 2 },
    ]);
  });
});

describe("chat resume: reconnecting", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("tries three times, 2 / 4 / 8 s apart", () => {
    expect(RESUME_DELAYS_MS).toEqual([2000, 4000, 8000]);
    expect([0, 1, 2, 3].map(resumeDelay)).toEqual([2000, 4000, 8000, null]);
  });

  it("resumes after the last seq and tells a gone turn by its 404", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      json({ code: "chat.turn_not_found", message: "chat turn not found", detail: null }, 404),
    );
    vi.stubGlobal("fetch", fetchMock);
    const err = await resumeChat("a 1", "t1", 7).catch((e: unknown) => e);
    expect(fetchMock.mock.calls[0][0]).toBe("/api/chat/a%201/turns/t1/events?after=7");
    expect(err).toBeInstanceOf(ChatHttpError);
    expect(isTurnGone(err)).toBe(true);
    expect(isTurnGone(new ChatHttpError(500, "x"))).toBe(false);
    expect(isTurnGone(new TypeError("Failed to fetch"))).toBe(false);
  });

  it("stops the turn on the server", async () => {
    const fetchMock = vi.fn().mockResolvedValue(json({ turn_id: "t1", stopped: true }));
    vi.stubGlobal("fetch", fetchMock);
    await expect(stopChatTurn("a1", "t1")).resolves.toEqual({ turn_id: "t1", stopped: true });
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/chat/a1/turns/t1/stop");
    expect(init.method).toBe("POST");
  });
});

describe("chat resume: a reload", () => {
  afterEach(() => vi.unstubAllGlobals());

  const rows = [
    { id: 1, role: "user", text: "one", name: null },
    { id: 2, role: "agent", text: "first", name: null },
    { id: 3, role: "user", text: "two", name: null },
    { id: 4, role: "tool", text: "", name: "search" },
  ];

  it("shows the thread up to the running turn's user message", () => {
    expect(rowsBeforeTurn(rows, { turn_id: "t", user_message_id: 3 }).map((r) => r.id)).toEqual([1, 2, 3]);
    expect(rowsBeforeTurn(rows, null)).toBe(rows);
    // a turn whose user row is not in the page: nothing is dropped
    expect(rowsBeforeTurn(rows, { turn_id: "t", user_message_id: 99 })).toBe(rows);
  });

  it("reads the running turn from the history", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(json({ messages: rows, running_turn: { turn_id: "t", user_message_id: 3 } })));
    const history = await chatHistory("a1", "s1");
    expect(history.running_turn).toEqual({ turn_id: "t", user_message_id: 3 });
  });

  it("restores a stopped turn as stopped and a turn past its limit as a timeout", () => {
    expect(isStoppedRow(STOPPED_ROW)).toBe(true);
    expect(isStoppedRow("interrupted: client disconnected")).toBe(false);
    expect(restoredFailure("interrupted: the turn ran past its 660 s limit")).toMatchObject({
      category: "timeout",
      manual: false,
    });
    expect(restoredFailure("interrupted: client disconnected")).toMatchObject({ category: "connection" });
  });
});
