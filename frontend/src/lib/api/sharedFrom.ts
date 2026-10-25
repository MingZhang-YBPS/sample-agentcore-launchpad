/**
 * Cross-project shares as the grantee sees them (projects design §11.2, SHARE-2).
 *
 * Lists of knowledge bases, golden sets, datasets and evaluators carry `shared_from`
 * on the rows another project lent this one — `{project_id, name, access}` — and
 * nothing on the project's own rows. `sharedFromOf` reads it off any row without the
 * list types having to know about it.
 *
 * When a share ends (revoked by its owner, or expired) the grantee project's owners
 * get a notice: `GET /api/me/share-notices`, each account reading only its own.
 */
import { getJson, sendJson } from "../api";

const enc = encodeURIComponent;

export type ShareAccess = "read" | "use";

export interface SharedFrom {
  project_id: string;
  name: string;
  access: ShareAccess;
}

/** The lending project of a list row, or null for the project's own row. */
export function sharedFromOf(row: unknown): SharedFrom | null {
  if (!row || typeof row !== "object") return null;
  const value = (row as { shared_from?: unknown }).shared_from;
  if (!value || typeof value !== "object") return null;
  const { project_id, name, access } = value as Partial<SharedFrom>;
  if (typeof project_id !== "string" || !project_id) return null;
  return {
    project_id,
    name: typeof name === "string" && name ? name : project_id,
    access: access === "use" ? "use" : "read",
  };
}

export interface ShareNotice {
  id: string;
  /** how the share ended */
  kind: "revoked" | "expired";
  share_id: string;
  /** the grantee — the caller's project */
  project_id: string;
  project_name: string;
  owner_project_id: string;
  owner_project_name: string;
  resource_kind: string;
  resource_id: string;
  resource_name: string;
  access: ShareAccess;
  workspace_id: string | null;
  /** the grantee's agents that still pin the resource (knowledge bases) */
  pinned_by: string[];
  actor: string;
  created_at: string;
  read: boolean;
}

export const shareNoticesApi = {
  list: () => getJson<{ items: ShareNotice[]; unread: number }>("/api/me/share-notices"),
  acknowledge: (noticeId: string) =>
    sendJson<{ ok: true; notice_id: string }>(`/api/me/share-notices/${enc(noticeId)}/ack`, "POST"),
};

/** One project's unread notices, newest first (the list holds every project's). */
export function unreadNoticesFor(items: readonly ShareNotice[], projectId: string): ShareNotice[] {
  return items
    .filter((n) => n.project_id === projectId && !n.read)
    .sort((a, b) => b.created_at.localeCompare(a.created_at));
}
