import { describe, expect, it } from "vitest";

import {
  type ChatStreamFrame,
  reduceTurnStatus,
  sseEvents,
  STALL_ABORT_MS,
  STALL_WARN_MS,
  startTurnStatus,
  toolSeconds,
  turnActivity,
  turnElapsedMs,
  type TurnStatus,
  turnStalled,
  turnStopReason,
} from "./chat";

/** Fold `[event, data, at]` frames into a status started at t = 0. */
function fold(frames: [string, ChatStreamFrame, number][]): TurnStatus {
  return frames.reduce((s, [event, data, at]) => reduceTurnStatus(s, event, data, at), startTurnStatus(0));
}

describe("turn status (chat-streaming S2)", () => {
  it("says connecting until the runtime answers, then thinking", () => {
    let s = startTurnStatus(0);
    expect(turnActivity(s, 10)).toEqual({ kind: "connecting" });
    s = reduceTurnStatus(s, "meta", { session_id: "s" }, 5);
    s = reduceTurnStatus(s, "phase", { phase: "starting" }, 6);
    expect(turnActivity(s, 10)).toEqual({ kind: "connecting" });
    s = reduceTurnStatus(s, "phase", { phase: "waiting_model" }, 900);
    expect(turnActivity(s, 1000)).toEqual({ kind: "thinking" });
  });

  it("goes by meta alone on a stream without status events", () => {
    const s = reduceTurnStatus(startTurnStatus(0), "meta", { session_id: "s" }, 5);
    expect(turnActivity(s, 10)).toEqual({ kind: "thinking" });
  });

  it("shows reasoning, then the running tool with its own timer, then nothing once text streams", () => {
    let s = fold([
      ["phase", { phase: "waiting_model" }, 100],
      ["thinking", {}, 200],
    ]);
    expect(turnActivity(s, 300)).toEqual({ kind: "reasoning" });
    s = reduceTurnStatus(s, "tool", { name: "search_kb", id: "t1" }, 1000);
    expect(turnActivity(s, 9000)).toEqual({ kind: "tool", tool: "search_kb", ms: 8000 });
    s = reduceTurnStatus(s, "tool_end", { id: "t1", status: "success", duration_ms: 2400 }, 3400);
    // back to waiting on the model after the tool, not "reasoning" from before it
    expect(turnActivity(s, 3500)).toEqual({ kind: "thinking" });
    expect(s.tools).toEqual([
      { id: "t1", name: "search_kb", status: "success", startedAt: 1000, durationMs: 2400 },
    ]);
    s = reduceTurnStatus(s, "delta", { text: "Hi" }, 3600);
    expect(s.phase).toBe("streaming");
    expect(turnActivity(s, 3700)).toBeNull();
    // a tool call after text reopens the placeholder
    s = reduceTurnStatus(s, "tool", { name: "fetch", id: "t2" }, 4000);
    expect(turnActivity(s, 4500)).toEqual({ kind: "tool", tool: "fetch", ms: 500 });
  });

  it("keeps a failed or denied call's status and only ends the matching running call", () => {
    const s = fold([
      ["tool", { name: "a", id: "x" }, 0],
      ["tool", { name: "b", id: "y" }, 0],
      ["tool_end", { id: "y", status: "denied", duration_ms: 10 }, 10],
      ["tool_end", { id: "y", status: "success", duration_ms: 99 }, 20], // already ended
    ]);
    expect(s.tools.map((r) => [r.id, r.status, r.durationMs])).toEqual([
      ["x", "running", undefined],
      ["y", "denied", 10],
    ]);
  });

  it("times a tool_end without a duration locally and names an id-less call", () => {
    const s = fold([
      ["tool", { name: "calc", id: null }, 100],
      ["tool_end", { id: "tool-1", status: "error", duration_ms: null }, 600],
    ]);
    expect(s.tools[0]).toMatchObject({ id: "tool-1", status: "error", durationMs: 500 });
  });

  it("ends calls the stream never ended with the turn", () => {
    const done = fold([["tool", { name: "a", id: "x" }, 0], ["done", {}, 50]]);
    expect(done.tools[0]).toMatchObject({ status: "success", durationMs: 50 });
    const failed = fold([["tool", { name: "a", id: "x" }, 0], ["error", {}, 70]]);
    expect(failed.tools[0]).toMatchObject({ status: "error", durationMs: 70 });
  });

  it("re-bases the timer on every heartbeat", () => {
    let s = startTurnStatus(1_000);
    expect(turnElapsedMs(s, 6_000)).toBe(5_000);
    // the tab slept: the local clock says 5 s, the server says 30 s
    s = reduceTurnStatus(s, "heartbeat", { elapsed_ms: 30_000 }, 6_000);
    expect(turnElapsedMs(s, 7_000)).toBe(31_000);
  });

  it("warns after 30 s without a frame and gives up after 45 s; a heartbeat counts", () => {
    let s = fold([["phase", { phase: "waiting_model" }, 0]]);
    expect(turnActivity(s, STALL_WARN_MS - 1)).toEqual({ kind: "thinking" });
    expect(turnActivity(s, STALL_WARN_MS)).toEqual({ kind: "stalled" });
    expect(turnStalled(s, STALL_ABORT_MS - 1)).toBe(false);
    expect(turnStalled(s, STALL_ABORT_MS)).toBe(true);
    s = reduceTurnStatus(s, "heartbeat", { elapsed_ms: 40_000 }, 40_000);
    expect(turnActivity(s, 50_000)).toEqual({ kind: "thinking" });
    expect(turnStalled(s, 50_000)).toBe(false);
  });

  it("formats tool durations with one decimal under a minute", () => {
    expect(toolSeconds(2400)).toBe("2.4");
    expect(toolSeconds(61_400)).toBe("61");
  });
});

describe("stopping a turn", () => {
  it("names why the page aborted", () => {
    const stop = new AbortController();
    expect(turnStopReason(stop.signal)).toBeNull();
    stop.abort("stop");
    expect(turnStopReason(stop.signal)).toBe("stop");
    const stalled = new AbortController();
    stalled.abort("stalled");
    expect(turnStopReason(stalled.signal)).toBe("stalled");
  });

  it("ends the frames and cancels the body when the signal aborts", async () => {
    let cancelled = false;
    const encoder = new TextEncoder();
    const body = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(encoder.encode('event: meta\ndata: {"session_id":"s"}\n\n'));
      },
      cancel() {
        cancelled = true;
      },
    });
    const controller = new AbortController();
    const seen: string[] = [];
    for await (const { event } of sseEvents(new Response(body), { signal: controller.signal })) {
      seen.push(event);
      controller.abort("stop"); // the stream would otherwise wait forever
    }
    expect(seen).toEqual(["meta"]);
    expect(cancelled).toBe(true);
  });
});
