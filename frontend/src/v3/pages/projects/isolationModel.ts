/**
 * Pure helpers of the "Runtime isolation" section (projects design §10.11–§10.14):
 * which signal each state lights, and which actions the current state allows.
 */
import type {
  IsolationWorkspace,
  MigrationAgentStatus,
  MigrationRun,
  MigrationState,
  PreflightFinding,
  ProjectIsolation,
  ProjectMigration,
  ProvisionState,
} from "../../../lib/api/projects";
import type { Signal } from "../../ui";

export const AGENT_STEPS: Record<MigrationRun["direction"], string[]> = {
  isolate: ["grant-new", "republish", "verify", "switch"],
  revert: ["grant-new", "republish", "verify", "switch"],
  retire: ["retire"],
};

export function stepSignal(status: MigrationAgentStatus | "pending" | undefined): Signal {
  switch (status) {
    case "succeeded":
      return "ok";
    case "running":
      return "info";
    case "awaiting_release":
      return "wait";
    case "failed":
      return "act";
    default:
      return "off";
  }
}

export function provisionSignal(state: ProvisionState): Signal {
  return ({ ready: "ok", partial: "wait", provisioning: "info", failed: "act", not_provisioned: "off" } as const)[state];
}

export function migrationSignal(state: MigrationState): Signal {
  return ({ succeeded: "ok", awaiting_release: "wait", running: "info", failed: "act", not_started: "off" } as const)[state];
}

/** something is moving: keep polling */
export function isBusy(iso: ProjectIsolation | null, mig: ProjectMigration | null): boolean {
  return Boolean(
    iso?.workspaces.some((w) => w.state === "provisioning") || mig?.workspaces.some((w) => w.state === "running"),
  );
}

export function migrationOf(mig: ProjectMigration | null, workspaceId: string) {
  return mig?.workspaces.find((w) => w.workspace_id === workspaceId) ?? null;
}

/** `isolate` may start: isolated, provisioned, nothing running in the workspace */
export function canMigrate(iso: ProjectIsolation, ws: IsolationWorkspace, mig: ProjectMigration | null): boolean {
  const run = migrationOf(mig, ws.workspace_id);
  return iso.mode === "isolated" && (ws.state === "ready" || ws.state === "partial") && run?.state !== "running";
}

/** a run can resume: it failed, or agents wait for their release */
export function canRetry(run: MigrationRun | null): boolean {
  return run !== null && (run.state === "failed" || run.state === "awaiting_release");
}

/** reverse migration: only before retire, and not while something moves */
export function canRevert(iso: ProjectIsolation, mig: ProjectMigration | null): boolean {
  return iso.mode === "isolated" && !iso.legacy_retired && !isBusy(iso, mig);
}

/** retire: every workspace finished moving its agents, none awaiting release */
export function canRetire(iso: ProjectIsolation, mig: ProjectMigration | null): boolean {
  if (iso.mode !== "isolated" || !mig || mig.workspaces.length === 0) return false;
  return mig.workspaces.every(
    (w) => w.run !== null && w.state === "succeeded" && (w.run.direction === "isolate" || w.run.direction === "retire"),
  );
}

/** fill (0–100) and signal of one preflight quota; null when it could not be read */
export function quotaMeter(f: PreflightFinding): { pct: number; s: Signal } | null {
  if (!f.known || f.limit === null || f.used === null || f.limit <= 0) return null;
  const after = f.used + f.needed;
  const pct = Math.min(100, Math.round((after / f.limit) * 100));
  return { pct, s: f.blocked ? "act" : pct >= 80 ? "wait" : "ok" };
}

/** "3/7" style progress of a run: finished (moved or skipped) of all */
export function runProgress(run: MigrationRun): { done: number; total: number } {
  const total = run.agents.length;
  const done = run.agents.filter((a) => a.status === "succeeded" || a.status === "skipped").length;
  return { done, total };
}
