import { describe, expect, it } from "vitest";

import { ApiError } from "../../../lib/api";
import { readTurn } from "./tryTurn";

const sse = (frames: string[]) => new Response(frames.join(""), { headers: { "Content-Type": "text/event-stream" } });

describe("TryIt readTurn (chat stream, not sync /invoke)", () => {
  it("accumulates deltas, reports each partial, and ignores heartbeats", async () => {
    const partials: string[] = [];
    const text = await readTurn(
      sse([
        'event: meta\ndata: {"session_id": "s1"}\n\n',
        'event: heartbeat\ndata: {"elapsed_ms": 30000}\n\n',
        'event: delta\ndata: {"text": "4"}\nid: 2\n\n',
        'event: delta\ndata: {"text": "2"}\nid: 3\n\n',
        'event: done\ndata: {"latency_ms": 61000}\nid: 4\n\n',
      ]),
      (t) => partials.push(t),
      "failed",
    );
    expect(text).toBe("42");
    expect(partials).toEqual(["4", "42"]);
  });

  it("throws an error event as an ApiError carrying its code", async () => {
    const run = readTurn(
      sse([
        'event: meta\ndata: {"session_id": "s1"}\n\n',
        'event: error\ndata: {"code": "agent.upstream_error", "message": "model failed"}\n\n',
      ]),
      () => undefined,
      "failed",
    );
    await expect(run).rejects.toBeInstanceOf(ApiError);
    await expect(run).rejects.toMatchObject({ code: "agent.upstream_error" });
  });
});
