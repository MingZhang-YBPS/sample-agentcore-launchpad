/**
 * Storage mounts in the Agent-DLC (storage-mounts §8): the release gate's mount diff
 * (`record.storage`, also `gate_report.storage`), the sign-off a read-write change
 * needs, the confirmation a gate evaluation that writes shared-state storage needs,
 * and the storage an architect proposal mounts. The two release calls are the
 * `/release/evaluate` and `/release/sign` routes with the storage flags added.
 */
import { sendJson } from "../api";
import type { ReleaseState } from "../dlc";
import type { StorageDataClass, StorageMode } from "./storage";

const enc = encodeURIComponent;

export interface StorageMountView {
  storage_id: string | null;
  mount_path: string;
  mode: StorageMode;
  kind?: string | null;
  name?: string | null;
  data_class?: StorageDataClass | null;
}

export type StorageChangeKind = "added" | "removed" | "mode" | "storage" | "data_class";

export interface StorageChange {
  change: StorageChangeKind;
  requires_ack: boolean;
  mount_path: string;
  storage_id: string | null;
  name: string | null;
  mode: StorageMode | null;
  data_class: StorageDataClass | null;
  from_storage_id: string | null;
  from_mode: StorageMode | null;
  from_data_class: StorageDataClass | null;
}

export interface ReleaseStorageEvidence {
  candidate: StorageMountView[];
  /** null: the live version's spec is not known, every mount reads as added */
  live: StorageMountView[] | null;
  live_known: boolean;
  changes: StorageChange[];
  requires_ack: boolean;
  shared_state_writes: StorageMountView[];
  acknowledged_by?: string;
  acknowledged_at?: string;
  write_confirmation?: { confirmed_by: string; confirmed_at: string; storage: string[] };
}

/** The evidence of a release record or gate report (null when none was computed). */
export function releaseStorage(source: unknown): ReleaseStorageEvidence | null {
  const value = (source as { storage?: unknown } | null | undefined)?.storage;
  if (!value || typeof value !== "object" || !Array.isArray((value as ReleaseStorageEvidence).changes)) return null;
  return value as ReleaseStorageEvidence;
}

/** Worth a panel: the candidate mounts storage, or live did and the candidate dropped it. */
export function hasStorage(ev: ReleaseStorageEvidence | null): ev is ReleaseStorageEvidence {
  return Boolean(ev && (ev.candidate.length > 0 || ev.changes.length > 0));
}

export const mountLabel = (m: { storage_id: string | null; name?: string | null; mount_path: string }): string =>
  m.storage_id ? (m.name && m.name !== m.storage_id ? `${m.name} (${m.storage_id})` : m.storage_id) : m.mount_path;

/** `ro → rw`, `datasets → state`, `reference → shared_state` for one change ('' for add / remove). */
export function changeDelta(c: StorageChange): string {
  if (c.change === "mode") return `${c.from_mode ?? "—"} → ${c.mode ?? "—"}`;
  if (c.change === "storage") return `${c.from_storage_id ?? "—"} → ${c.storage_id ?? "—"}`;
  if (c.change === "data_class") return `${c.from_data_class ?? "—"} → ${c.data_class ?? "—"}`;
  return "";
}

export interface ProposalStorageChoice {
  storage_id: string;
  mode: StorageMode;
}

/** A proposal content's `storage` member, tolerant of older or malformed revisions. */
export function proposalStorage(content: unknown): ProposalStorageChoice[] {
  const raw = (content as { storage?: unknown } | null | undefined)?.storage;
  if (!Array.isArray(raw)) return [];
  return raw
    .filter((s): s is { storage_id: unknown; mode?: unknown } => Boolean(s) && typeof s === "object")
    .map((s): ProposalStorageChoice => ({ storage_id: String(s.storage_id ?? ""), mode: s.mode === "rw" ? "rw" : "ro" }))
    .filter((s) => s.storage_id);
}

export const storageDlcApi = {
  /** `/release/evaluate`; `confirm_storage` lets the replay write shared-state storage */
  evaluateRelease: (agentId: string, body: { repeats?: number; confirm_cost?: boolean; confirm_storage?: boolean }) =>
    sendJson<ReleaseState>(`/api/agents/${enc(agentId)}/release/evaluate`, "POST", body),
  /** `/release/sign`; `storage_ack` acknowledges the read-write storage changes */
  signRelease: (agentId: string, note: string, storageAck: boolean) =>
    sendJson<ReleaseState>(`/api/agents/${enc(agentId)}/release/sign`, "POST", { note, storage_ack: storageAck }),
};
