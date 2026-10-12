import type { ResultRow } from "../../../v2/results";
import type { TaskStatus, V2Task } from "../../../v2/tasks";
import type { Signal } from "../../ui";

/** A task's status on the V3 signal scale: failed needs you, in flight is live. */
export function taskSignal(status: TaskStatus): Signal {
  switch (status) {
    case "failed":
      return "act";
    case "running":
      return "info";
    case "queued":
    case "pending":
    case "paused":
      return "wait";
    case "completed":
      return "ok";
    default:
      return "off";
  }
}

/** failed first, then what is moving, then the rest — newest first inside each */
const RANK: Record<TaskStatus, number> = { failed: 0, running: 1, queued: 1, pending: 1, paused: 2, completed: 3, stopped: 3 };
export function rankTasks<T extends { status: TaskStatus; createdAt: string | null }>(tasks: T[]): T[] {
  return [...tasks].sort((a, b) => RANK[a.status] - RANK[b.status] || String(b.createdAt ?? "").localeCompare(String(a.createdAt ?? "")));
}

export function outcomeSignal(outcome: ResultRow["outcome"]): Signal {
  return outcome === "passed" ? "ok" : outcome === "failed" ? "act" : "wait";
}

export function scoreSignal(normalized: number | null): Signal | undefined {
  if (normalized == null) return undefined;
  return normalized >= 0.7 ? "ok" : normalized >= 0.4 ? "wait" : "act";
}

export const detailUrl = (task: Pick<V2Task, "kind" | "id">) =>
  `/v3/tasks?view=detail&kind=${task.kind}&id=${encodeURIComponent(task.id)}`;
export const copyUrl = (task: Pick<V2Task, "kind" | "id">) =>
  `/v3/tasks?view=new&from=${encodeURIComponent(`${task.kind}:${task.id}`)}`;
