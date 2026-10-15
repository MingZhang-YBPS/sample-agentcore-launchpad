/**
 * Pure helpers of a project's steward trace (projects design §11.4): the owners'
 * notices of an organization admin's membership changes and the two-person rule's
 * pending changes, narrowed to one project and summarized per change.
 */
import type { StewardChange, StewardNotice, StewardPending } from "../../../lib/api/steward";

/** This project's notices (unread first, newest first) and pending changes (oldest first). */
export function stewardTraceFor(
  projectId: string,
  notices: readonly StewardNotice[],
  pending: readonly StewardPending[],
): { notices: StewardNotice[]; pending: StewardPending[] } {
  return {
    notices: notices
      .filter((n) => n.project_id === projectId)
      .sort((a, b) => Number(a.read) - Number(b.read) || b.created_at.localeCompare(a.created_at)),
    pending: pending
      .filter((p) => p.project_id === projectId)
      .sort((a, b) => a.created_at.localeCompare(b.created_at)),
  };
}

/** Who a change adds or updates (with the role key) and who it removes, by name. */
export function changeSummary(changes: readonly StewardChange[]): {
  upserts: { name: string; role: string | null }[];
  removes: string[];
} {
  const name = (c: StewardChange) => c.username ?? c.user_id;
  return {
    upserts: changes.filter((c) => c.op === "upsert").map((c) => ({ name: name(c), role: c.role_key ?? null })),
    removes: changes.filter((c) => c.op === "remove").map(name),
  };
}
