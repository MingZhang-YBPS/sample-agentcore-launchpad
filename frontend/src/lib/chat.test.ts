import { describe, expect, it } from "vitest";

import { asUserChecked, asUserField, isHeartbeat, sseEvents } from "./chat";

describe("invoke as me", () => {
  it("omits as_user until the member toggles it, so the backend picks", () => {
    expect(asUserField(true, null)).toBeUndefined();
    expect(asUserField(true, true)).toBe(true);
    expect(asUserField(true, false)).toBe(false);
  });

  it("never sends as_user for a non-JWT agent", () => {
    expect(asUserField(false, null)).toBeUndefined();
    expect(asUserField(false, true)).toBeUndefined();
    expect(asUserField(false, false)).toBeUndefined();
  });

  it("shows auto as checked only when signed in through the pool", () => {
    expect(asUserChecked(null, true)).toBe(true);
    expect(asUserChecked(null, false)).toBe(false);
    expect(asUserChecked(true, false)).toBe(true);
    expect(asUserChecked(false, true)).toBe(false);
  });
});

describe("sseEvents heartbeats", () => {
  const body = (text: string) => {
    const bytes = new TextEncoder().encode(text);
    // split mid-frame so the parser has to carry a partial frame over
    const cut = Math.floor(bytes.length / 2);
    const stream = new ReadableStream<Uint8Array>({
      start(c) {
        c.enqueue(bytes.slice(0, cut));
        c.enqueue(bytes.slice(cut));
        c.close();
      },
    });
    return new Response(stream);
  };
  const wire =
    'event: meta\ndata: {"session_id":"s1"}\n\n' +
    'event: heartbeat\ndata: {"elapsed_ms":15000}\n\n' +
    ": keep-alive\n\n" +
    'event: delta\ndata: {"text":"hi"}\n\n' +
    'event: done\ndata: {"latency_ms":16000}\n\n';

  const collect = async (res: Response, opts?: { heartbeats?: boolean }) => {
    const out: { event: string; data: unknown }[] = [];
    for await (const frame of sseEvents(res, opts)) out.push(frame);
    return out;
  };

  it("drops named heartbeats and keep-alive comments by default", async () => {
    const frames = await collect(body(wire));
    expect(frames.map((f) => f.event)).toEqual(["meta", "delta", "done"]);
  });

  it("yields heartbeats with their elapsed time when asked", async () => {
    const frames = await collect(body(wire), { heartbeats: true });
    expect(frames.map((f) => f.event)).toEqual(["meta", "heartbeat", "delta", "done"]);
    expect(frames[1].data).toEqual({ elapsed_ms: 15000 });
    expect(isHeartbeat(frames[1].event)).toBe(true);
  });
});
