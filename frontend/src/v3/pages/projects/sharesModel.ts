/**
 * Pure helpers of the project "Shares" tab (projects design §11.2): how much of a
 * share's life is left and what colour that is, which actions the caller has on a
 * row, and the payload the grant form sends.
 */
import type { ResourceShare, ShareAccess, ShareCreate, ShareKind } from "../../../lib/api/projects";
import type { Signal } from "../../ui";

const DAY_MS = 86_400_000;

/** Whole days until `expires_at` (0 once past); null when the share never expires. */
export function daysLeft(expiresAt: string | null, now: number = Date.now()): number | null {
  if (!expiresAt) return null;
  const at = Date.parse(expiresAt);
  if (Number.isNaN(at)) return null;
  return Math.max(0, Math.ceil((at - now) / DAY_MS));
}

/** The share's remaining life as 0..1 of its granted span (1 when it never expires). */
export function lifeLeft(share: Pick<ResourceShare, "granted_at" | "expires_at">, now: number = Date.now()): number {
  if (!share.expires_at || !share.granted_at) return 1;
  const start = Date.parse(share.granted_at);
  const end = Date.parse(share.expires_at);
  if (Number.isNaN(start) || Number.isNaN(end) || end <= start) return 0;
  return Math.min(1, Math.max(0, (end - now) / (end - start)));
}

/** ok with room to spare · wait inside two weeks · act inside three days or ended. */
export function lifeSignal(share: Pick<ResourceShare, "status" | "expires_at">, now: number = Date.now()): Signal {
  if (share.status === "requested") return "wait";
  if (share.status !== "active") return "off";
  const days = daysLeft(share.expires_at, now);
  if (days === null || days > 14) return "ok";
  return days > 3 ? "wait" : "act";
}

export function statusSignal(status: ResourceShare["status"]): Signal {
  if (status === "active") return "ok";
  if (status === "requested") return "wait";
  return "off";
}

/** Open = still counts or may count (the rows an owner can revoke). */
export function isOpen(share: Pick<ResourceShare, "status">): boolean {
  return share.status === "active" || share.status === "requested";
}

/** The owner side revokes an open share; the grantee side's approver decides a request. */
export function rowActions(
  share: ResourceShare,
  side: "outgoing" | "incoming",
  can: { share: boolean; approve: boolean },
): { revoke: boolean; decide: boolean } {
  return {
    revoke: side === "outgoing" && can.share && isOpen(share),
    decide: side === "incoming" && can.approve && share.status === "requested",
  };
}

/** AWS-only kinds have no ledger list to pick from in every case: their id may be typed. */
export function typedKind(kind: ShareKind): boolean {
  return kind === "knowledge_base" || kind === "evaluator";
}

export interface ShareDraft {
  workspace_id: string;
  kind: ShareKind;
  resource_id: string;
  resource_name: string;
  grantee_project_id: string;
  access: ShareAccess;
  days: string;
  note: string;
}

export type DraftProblem = "resource" | "grantee" | "days";

/** What is still missing or wrong in the grant form (empty = it can be sent). */
export function draftProblems(draft: ShareDraft, maxDays: number): DraftProblem[] {
  const out: DraftProblem[] = [];
  if (!draft.resource_id.trim()) out.push("resource");
  if (!draft.grantee_project_id) out.push("grantee");
  const days = Number(draft.days);
  if (!/^\d+$/.test(draft.days.trim()) || days < 1 || days > maxDays) out.push("days");
  return out;
}

export function draftPayload(draft: ShareDraft): ShareCreate {
  return {
    workspace_id: draft.workspace_id,
    kind: draft.kind,
    resource_id: draft.resource_id.trim(),
    grantee_project_id: draft.grantee_project_id,
    access: draft.access,
    expires_in_days: Number(draft.days),
    ...(draft.resource_name.trim() ? { resource_name: draft.resource_name.trim() } : {}),
    ...(draft.note.trim() ? { note: draft.note.trim() } : {}),
  };
}
