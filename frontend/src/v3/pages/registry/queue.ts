// Registry approval queue: which records wait, in what order, and the batch runner
// the pending tab approves or rejects many records with.
import { ApiError, type RegistryRecordOut } from "../../../lib/api";
import { parseTimestamp } from "../../../lib/timestamps";

export type RegistryTab = "existing" | "pending";

/** `?tab=pending` opens the queue; anything else is the default tab. */
export function parseTab(raw: string | null): RegistryTab {
  return raw === "pending" ? "pending" : "existing";
}

export const isPending = (r: Pick<RegistryRecordOut, "status">) => r.status === "PENDING_APPROVAL";

/**
 * The queue, longest-waiting first. A record has no submit time of its own;
 * submitting updates it, so `updated_at` stands in for when it was submitted.
 */
export function pendingQueue<T extends Pick<RegistryRecordOut, "status" | "updated_at">>(records: T[]): T[] {
  const at = (r: T) => parseTimestamp(r.updated_at)?.getTime() ?? Number.POSITIVE_INFINITY;
  return records.filter(isPending).sort((a, b) => (at(a) === at(b) ? 0 : at(a) - at(b)));
}

/** When the longest-waiting record was submitted (null for an empty queue). */
export function oldestWait(records: Pick<RegistryRecordOut, "status" | "updated_at">[]): string | null {
  let oldest: { iso: string; at: number } | null = null;
  for (const r of records) {
    if (!isPending(r)) continue;
    const at = parseTimestamp(r.updated_at)?.getTime();
    if (at == null || !r.updated_at) continue;
    if (!oldest || at < oldest.at) oldest = { iso: r.updated_at, at };
  }
  return oldest?.iso ?? null;
}

export const PAGE_SIZE = 20;

export function pageCount(total: number, size = PAGE_SIZE): number {
  return Math.max(1, Math.ceil(total / size));
}

/** A throttled call is worth one more try after a pause; anything else is final. */
export function isThrottled(err: unknown): boolean {
  return err instanceof ApiError && (err.code === "aws.throttled" || err.code === "http.429");
}

export type RowState = { state: "running" } | { state: "ok" } | { state: "failed"; message: string };

export interface BatchOptions {
  /** calls in flight at once; 3 keeps the AgentCore Registry under its rate limit */
  concurrency?: number;
  /** pause before the one retry of a throttled call */
  backoffMs?: number;
  sleep?: (ms: number) => Promise<void>;
  describe?: (err: unknown) => string;
}

/**
 * Run `call` for every id, at most `concurrency` at a time, in the given order.
 * A throttled call is retried once after `backoffMs`; a second failure, or any
 * other error, marks that id failed and the rest carry on. `onState` sees each
 * id go running → ok | failed.
 */
export async function runBatch(
  ids: string[],
  call: (id: string) => Promise<unknown>,
  onState: (id: string, state: RowState) => void,
  { concurrency = 3, backoffMs = 1500, sleep = (ms) => new Promise((r) => setTimeout(r, ms)), describe = String }: BatchOptions = {},
): Promise<{ ok: string[]; failed: string[] }> {
  const ok: string[] = [];
  const failed: string[] = [];
  let next = 0;
  const one = async (id: string) => {
    onState(id, { state: "running" });
    try {
      try {
        await call(id);
      } catch (err) {
        if (!isThrottled(err)) throw err;
        await sleep(backoffMs);
        await call(id);
      }
      ok.push(id);
      onState(id, { state: "ok" });
    } catch (err) {
      failed.push(id);
      onState(id, { state: "failed", message: describe(err) });
    }
  };
  const worker = async () => {
    while (next < ids.length) await one(ids[next++]);
  };
  await Promise.all(Array.from({ length: Math.min(concurrency, ids.length) }, worker));
  return { ok, failed };
}
