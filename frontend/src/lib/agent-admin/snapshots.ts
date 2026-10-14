import { useState } from "react";

import { api, type SnapshotDiff, type SpecSnapshotInfo } from "../api";
import { useAdminLoad } from "./load";

/** Pick up to two snapshots to compare: a third pick drops the oldest pick. */
export function togglePick(prev: number[], seq: number): number[] {
  return prev.includes(seq) ? prev.filter((s) => s !== seq) : [...prev, seq].slice(-2);
}

/** The compared pair, older first; null unless exactly two are picked. */
export function comparePair(picked: number[]): [number, number] | null {
  return picked.length === 2 ? [Math.min(...picked), Math.max(...picked)] : null;
}

/**
 * Ledger snapshots per publish (T18): the list, the two-snapshot pick, and the
 * confirmed rollback. `refreshKey` changes whenever the agent (re)publishes, so
 * the list follows.
 */
export function useSnapshots(
  agentId: string,
  refreshKey: string,
  callbacks: { onRolledBack: (seq: number) => void; onError?: (err: unknown) => void },
) {
  const list = useAdminLoad(() => api.agentSnapshots(agentId), `snapshots:${agentId}:${refreshKey}`);
  const [picked, setPicked] = useState<number[]>([]);
  const [diff, setDiff] = useState<[number, number] | null>(null);
  const [rollback, setRollback] = useState<SpecSnapshotInfo | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);

  const confirmRollback = async () => {
    if (!rollback) return;
    setBusy(true);
    setError(null);
    try {
      await api.rollbackSnapshot(agentId, rollback.seq);
      const seq = rollback.seq;
      setRollback(null);
      setPicked([]);
      callbacks.onRolledBack(seq);
    } catch (err) {
      setError(err);
      callbacks.onError?.(err);
    } finally {
      setBusy(false);
    }
  };

  const rows = list.data?.snapshots ?? [];
  return {
    ...list,
    rows,
    /** the newest snapshot is what runs now: no rollback to it */
    currentSeq: rows[0]?.seq ?? null,
    picked,
    toggle: (seq: number) => setPicked((prev) => togglePick(prev, seq)),
    canCompare: comparePair(picked) !== null,
    diff,
    openDiff: () => setDiff(comparePair(picked)),
    closeDiff: () => setDiff(null),
    rollback,
    askRollback: (snap: SpecSnapshotInfo) => {
      setError(null);
      setRollback(snap);
    },
    cancelRollback: () => {
      setRollback(null);
      setError(null);
    },
    confirmRollback,
    busy,
    /** the failed rollback (raw, for the AWS detail); the list's own error is `error` */
    actionError: error,
  };
}

/** The field-level diff between two snapshots. */
export function useSnapshotDiff(agentId: string, pair: [number, number]) {
  const [from, to] = pair;
  return useAdminLoad<SnapshotDiff>(() => api.snapshotDiff(agentId, from, to), `snapdiff:${agentId}:${from}:${to}`);
}

/** A diff value as text: strings as-is, everything else as indented JSON. */
export function showValue(value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (typeof value === "string") return value || "—";
  return JSON.stringify(value, null, 2);
}
