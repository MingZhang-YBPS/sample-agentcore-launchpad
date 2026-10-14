import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import { ApiError } from "../../../lib/api";
import { isThrottled, oldestWait, pageCount, parseTab, pendingQueue, runBatch, type RowState } from "./queue";

const rec = (id: string, status: string, updated_at: string | null) => ({ record_id: id, status, updated_at });

describe("registry tabs", () => {
  it("opens the queue only for ?tab=pending", () => {
    expect(parseTab("pending")).toBe("pending");
    expect(parseTab(null)).toBe("existing");
    expect(parseTab("bogus")).toBe("existing");
  });

  it("queues only pending records, longest-waiting first, whatever the timestamp shape", () => {
    const queue = pendingQueue([
      rec("new", "PENDING_APPROVAL", "2026-10-14T08:00:00+00:00"),
      rec("approved", "APPROVED", "2026-01-01T00:00:00Z"),
      rec("old", "PENDING_APPROVAL", "2026-10-12T08:00:00"), // naive UTC from the ledger
      rec("mid", "PENDING_APPROVAL", "2026-10-13T08:00:00Z"),
      rec("undated", "PENDING_APPROVAL", null),
    ]);
    expect(queue.map((r) => r.record_id)).toEqual(["old", "mid", "new", "undated"]);
  });

  it("names the longest wait, ignoring records that are not pending", () => {
    expect(oldestWait([
      rec("a", "APPROVED", "2020-01-01T00:00:00Z"),
      rec("b", "PENDING_APPROVAL", "2026-10-13T00:00:00Z"),
      rec("c", "PENDING_APPROVAL", "2026-10-11T00:00:00Z"),
    ])).toBe("2026-10-11T00:00:00Z");
    expect(oldestWait([rec("a", "APPROVED", "2026-10-11T00:00:00Z")])).toBeNull();
  });

  it("pages twenty at a time", () => {
    expect(pageCount(0)).toBe(1);
    expect(pageCount(20)).toBe(1);
    expect(pageCount(37)).toBe(2);
  });
});

describe("batch approval", () => {
  const noSleep = async () => undefined;

  it("keeps at most three calls in flight and processes every id in order", async () => {
    let inFlight = 0;
    let peak = 0;
    const started: string[] = [];
    const ids = Array.from({ length: 10 }, (_, i) => `r${i}`);
    const result = await runBatch(ids, async (id) => {
      started.push(id);
      inFlight += 1;
      peak = Math.max(peak, inFlight);
      await new Promise((r) => setTimeout(r, 2));
      inFlight -= 1;
    }, () => undefined, { sleep: noSleep });
    expect(peak).toBe(3);
    expect(started).toEqual(ids);
    expect(result.ok).toHaveLength(10);
    expect(result.failed).toEqual([]);
  });

  it("retries a throttled call once after backing off, then gives up", async () => {
    const calls: Record<string, number> = {};
    const pauses: number[] = [];
    const states: Record<string, RowState[]> = {};
    const result = await runBatch(["once", "always", "broken"], async (id) => {
      calls[id] = (calls[id] ?? 0) + 1;
      if (id === "once" && calls[id] === 1) throw new ApiError("aws.throttled", "slow down", null);
      if (id === "always") throw new ApiError("http.429", "slow down", null);
      if (id === "broken") throw new ApiError("access.denied", "no", null);
    }, (id, s) => (states[id] ??= []).push(s), { sleep: async (ms) => { pauses.push(ms); }, backoffMs: 1500, describe: (e) => (e as Error).message });
    expect(calls).toEqual({ once: 2, always: 2, broken: 1 });
    expect(pauses).toEqual([1500, 1500]);
    expect(result.ok).toEqual(["once"]);
    expect(result.failed.sort()).toEqual(["always", "broken"]);
    expect(states.broken).toEqual([{ state: "running" }, { state: "failed", message: "no" }]);
    expect(states.once.at(-1)).toEqual({ state: "ok" });
  });

  it("only treats throttling as retryable", () => {
    expect(isThrottled(new ApiError("aws.throttled", "", null))).toBe(true);
    expect(isThrottled(new ApiError("http.429", "", null))).toBe(true);
    expect(isThrottled(new ApiError("registry.unknown_action", "", null))).toBe(false);
    expect(isThrottled(new Error("x"))).toBe(false);
  });

  it("does nothing for an empty selection", async () => {
    expect(await runBatch([], async () => undefined, () => undefined)).toEqual({ ok: [], failed: [] });
  });
});

describe("registry approval copy", () => {
  const here = dirname(fileURLToPath(import.meta.url));
  const registry = (lang: string) =>
    JSON.parse(readFileSync(resolve(here, `../../../locales/${lang}/common.json`), "utf-8")).v3.registry;

  it("has every tab, batch and wait string in both languages", () => {
    const keys = [
      "tabs.existing", "tabs.pending", "waiting", "longestWait", "pendingEmpty", "keysHint", "openPending",
      "batch.selectAll_other", "batch.selected_other", "batch.approve", "batch.reject",
      "batch.confirmTitle_approve_other", "batch.confirmTitle_reject_other",
      "batch.confirmBody_approve_other", "batch.confirmBody_reject_other",
      "batch.summary", "batch.summaryOk", "batch.retryFailed_other", "batch.more_other",
      "batch.approveDone", "batch.rejectDone", "batch.rowRunning", "batch.rowFailed",
    ];
    for (const lang of ["en", "zh-CN"]) {
      const ns = registry(lang);
      for (const key of keys) {
        const value = key.split(".").reduce((o, k) => o?.[k], ns);
        expect(value, `${lang}: ${key}`).toBeTruthy();
      }
    }
  });
});
