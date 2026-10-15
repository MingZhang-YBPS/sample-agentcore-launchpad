import { describe, expect, it } from "vitest";

import { hostedGroups } from "../../v3/nav";
import { LIVE_STATUSES, scheduleAt, stepStates, type SystemUpdate } from "./systemUpdate";

const update = (over: Partial<SystemUpdate>): SystemUpdate => ({
  id: "u1", kind: "update", target_sha: "f".repeat(40), short: "fffffff", target_time: null, base_sha: null,
  behind: 1, commits: [], status: "preparing", staged_dir: null, progress: {},
  steps: ["fetch", "worktree", "backend", "frontend"], scheduled_at: null, requested_by: "admin",
  announcement_id: null, error: null, previous_sha: null, finished_at: null, created_at: null, updated_at: null,
  ...over,
});

describe("system update helpers", () => {
  it("reads the prepare progress step by step", () => {
    const states = stepStates(update({ progress: { fetch: "done", worktree: "done", backend: "running" } }));
    expect(states.map((s) => s.state)).toEqual(["done", "done", "running", "pending"]);
  });

  it("marks the step that was running as failed once the prepare failed", () => {
    const states = stepStates(update({ status: "prepare_failed", progress: { fetch: "done", worktree: "running" } }));
    expect(states.map((s) => s.state)).toEqual(["done", "failed", "pending", "pending"]);
  });

  it("polls only while the server is moving", () => {
    expect(LIVE_STATUSES.has("preparing") && LIVE_STATUSES.has("applying")).toBe(true);
    expect(LIVE_STATUSES.has("ready") || LIVE_STATUSES.has("done")).toBe(false);
  });

  it("turns a local datetime into an ISO instant; empty is now", () => {
    expect(scheduleAt("")).toBeNull();
    expect(scheduleAt("nonsense")).toBeNull();
    expect(scheduleAt("2026-10-16T02:00")).toBe(new Date("2026-10-16T02:00").toISOString());
  });

  it("lists the page in Configure for administrators only", () => {
    const tos = (admin: boolean) => hostedGroups(admin).flatMap((g) => g.items.map((i) => i.to));
    expect(hostedGroups(true).find((g) => g.key === "admin")?.items.map((i) => i.to)).toContain("/v3/system-update");
    expect(tos(false)).not.toContain("/v3/system-update");
  });
});
