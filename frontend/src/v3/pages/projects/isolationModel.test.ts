import { describe, expect, it } from "vitest";

import type { MigrationRun, ProjectIsolation, ProjectMigration } from "../../../lib/api/projects";
import { canMigrate, canRetire, canRetry, canRevert, isBusy, quotaMeter, runProgress, stepSignal } from "./isolationModel";

const ws = { workspace_id: "dev", state: "ready" as const, job_id: "j", job_status: "succeeded", last_error: null, skipped_stages: [], resources: {}, updated_at: null };
const iso = (over: Partial<ProjectIsolation> = {}): ProjectIsolation => ({
  project_id: "risk", mode: "isolated", enabled: true, gateway: "shared", updated_by: null, updated_at: null,
  legacy_retired: false, workspaces: [ws], ...over,
});
const run = (over: Partial<MigrationRun> = {}): MigrationRun => ({
  id: "r1", direction: "isolate", status: "succeeded", state: "succeeded", error: null, actor: "admin", stages: [], preflight: [],
  agents: [
    { agent_id: "a", name: "a", method: "zip_runtime", endpoint_mode: "default", status: "succeeded", step: null, detail: "", deploy_job_id: null, steps: {}, updated_at: "" },
    { agent_id: "b", name: "b", method: "zip_runtime", endpoint_mode: "live", status: "awaiting_release", step: "switch", detail: "", deploy_job_id: null, steps: {}, updated_at: "" },
  ],
  counts: { pending: 0, running: 0, succeeded: 1, skipped: 0, failed: 0, awaiting_release: 1 }, created_at: null, updated_at: null, ...over,
});
const mig = (r: MigrationRun | null, state = r?.state ?? "not_started"): ProjectMigration => ({
  project_id: "risk", mode: "isolated", legacy_retired: false, workspaces: [{ workspace_id: "dev", state, run: r }],
});

describe("isolation model", () => {
  it("lights the step signals", () => {
    expect(stepSignal("succeeded")).toBe("ok");
    expect(stepSignal("awaiting_release")).toBe("wait");
    expect(stepSignal("failed")).toBe("act");
    expect(stepSignal(undefined)).toBe("off");
  });

  it("allows migrate only on a provisioned isolated workspace with nothing running", () => {
    expect(canMigrate(iso(), ws, null)).toBe(true);
    expect(canMigrate(iso({ mode: "shared" }), ws, null)).toBe(false);
    expect(canMigrate(iso(), { ...ws, state: "provisioning" }, null)).toBe(false);
    expect(canMigrate(iso(), ws, mig(run({ state: "running" })))).toBe(false);
  });

  it("retries failed or waiting runs, retires only when every workspace finished", () => {
    expect(canRetry(run({ state: "awaiting_release" }))).toBe(true);
    expect(canRetry(run())).toBe(false);
    expect(canRetire(iso(), mig(run()))).toBe(true);
    expect(canRetire(iso(), mig(run({ state: "awaiting_release" })))).toBe(false);
    expect(canRetire(iso(), mig(null))).toBe(false);
    expect(canRetire(iso(), mig(run({ direction: "revert" })))).toBe(false);
  });

  it("refuses revert after retire or while busy", () => {
    expect(canRevert(iso(), null)).toBe(true);
    expect(canRevert(iso({ legacy_retired: true }), null)).toBe(false);
    expect(isBusy(iso({ workspaces: [{ ...ws, state: "provisioning" }] }), null)).toBe(true);
    expect(canRevert(iso(), mig(run({ state: "running" })))).toBe(false);
  });

  it("measures quotas and progress", () => {
    const f = { key: "iam.roles", label: "IAM roles", needed: 1, limit: 1000, used: 799, note: "", known: true, blocked: false };
    expect(quotaMeter(f)).toEqual({ pct: 80, s: "wait" });
    expect(quotaMeter({ ...f, known: false })).toBeNull();
    expect(quotaMeter({ ...f, used: 1000, blocked: true })?.s).toBe("act");
    expect(runProgress(run())).toEqual({ done: 1, total: 2 });
  });
});
