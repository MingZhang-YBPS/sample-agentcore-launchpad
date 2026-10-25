/**
 * Typed client of the steward trace (projects design §11.4; `routers/users.py`
 * `/api/steward/*`): the notices a project's owners get when a steward changes
 * its members, the two-person rule's pending changes, and its switch.
 *
 * Hub-global. Notices and pending changes are any account's to read (each answers
 * only what is the caller's); the switch is read by administrators and stewards and
 * flipped by administrators only.
 */
import { getJson, sendJson } from "../api";

const enc = encodeURIComponent;

/** The steward global role — structure only, no project data ("组织管理员"). */
export const STEWARD_ROLE = "steward";

/** Whether an account (or the signed-in caller) is a steward. */
export function isSteward(role: string | null | undefined): boolean {
  return role === STEWARD_ROLE;
}

export interface StewardChange {
  op: "upsert" | "remove";
  user_id: string;
  username: string | null;
  role_key?: string;
}

export interface StewardNotice {
  id: string;
  /** `changed`: applied; `pending`: waiting for a confirmation (two-person rule) */
  kind: "changed" | "pending";
  project_id: string;
  project_name: string;
  actor: string;
  actor_user_id: string | null;
  changes: StewardChange[];
  pending_id: string | null;
  created_at: string;
  read: boolean;
}

export interface StewardPending {
  id: string;
  project_id: string;
  project_name: string;
  changes: StewardChange[];
  requested_by: string;
  requested_by_user_id: string | null;
  created_at: string;
  /** the caller asked for it (may withdraw, never confirm) */
  mine: boolean;
  can_confirm: boolean;
}

export interface StewardSettings {
  steward_two_person: boolean;
}

export interface MemberChangeResult {
  added: string[];
  updated: string[];
  removed: string[];
  /** staged by the two-person rule instead of applied */
  pending?: string[];
}

export const stewardApi = {
  notices: () => getJson<{ items: StewardNotice[]; unread: number }>("/api/steward/notices"),
  acknowledge: (noticeId: string) =>
    sendJson<{ ok: true; notice_id: string }>(`/api/steward/notices/${enc(noticeId)}/ack`, "POST"),
  pending: () => getJson<{ items: StewardPending[] }>("/api/steward/pending"),
  confirm: (pendingId: string) =>
    sendJson<MemberChangeResult & { pending_id: string; project_id: string }>(
      `/api/steward/pending/${enc(pendingId)}/confirm`, "POST",
    ),
  reject: (pendingId: string) =>
    sendJson<{ pending_id: string; project_id: string; rejected: true }>(
      `/api/steward/pending/${enc(pendingId)}/reject`, "POST",
    ),
  settings: () => getJson<StewardSettings>("/api/steward/settings"),
  updateSettings: (body: StewardSettings) => sendJson<StewardSettings>("/api/steward/settings", "PUT", body),
};
