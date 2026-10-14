import { isImported } from "../../../lib/agent-admin/actions";
import type { AgentPermissions, EditBlocked } from "../../../lib/agent-admin/permissions";
import type { AgentInfo } from "../../../lib/api";
import { isMcpServer } from "../agent/tabs";

type AgentLike = Pick<AgentInfo, "method" | "status" | "system" | "spec">;

export interface RowEntry {
  key: "chat" | "edit" | "convert" | "delete" | "remove";
  disabled: boolean;
  /** why it is disabled: an edit rule, or `lock` (the prod-lock title explains) */
  why: EditBlocked | null;
  danger?: boolean;
}

/**
 * A list row's "⋯" menu (v3-agent-management §3): chat (never an MCP server),
 * edit, convert, delete — "remove" for an imported runtime. The rules are the
 * detail page's (§2.4): without the permission an entry is left out; the prod
 * lock, a preset for a non-admin or a deploy in flight disable it with the reason.
 */
export function rowEntries(agent: AgentLike, perms: AgentPermissions): RowEntry[] {
  const lock = perms.locked ? ("locked" as const) : null;
  const out: RowEntry[] = [];
  if (!isMcpServer(agent)) out.push({ key: "chat", disabled: false, why: null });
  const edit = perms.editState(agent);
  if (edit.visible) out.push({ key: "edit", disabled: edit.blocked !== null, why: edit.blocked });
  if (perms.canConvert(agent)) out.push({ key: "convert", disabled: perms.locked, why: lock });
  if (perms.canDelete(agent)) {
    out.push({ key: isImported(agent) ? "remove" : "delete", disabled: perms.locked, why: lock, danger: true });
  }
  return out;
}

/**
 * Whether a row can join a bulk delete: the delete permission (never a system
 * preset), not mid-deploy, and not on a locked prod workspace.
 */
export function bulkDeletable(agent: AgentLike, perms: AgentPermissions): boolean {
  return perms.canDelete(agent) && agent.status !== "deploying" && !perms.locked;
}

/** The selected ids still eligible among the rows on screen (a filter or a reload may drop some). */
export function liveSelection(selected: ReadonlySet<string>, rows: AgentInfo[], perms: AgentPermissions): AgentInfo[] {
  return rows.filter((a) => selected.has(a.id) && bulkDeletable(a, perms));
}

/** Toggle one id, or — with `ids` — all of them (on when any is off, else off). */
export function toggleSelection(selected: ReadonlySet<string>, ids: string | string[]): Set<string> {
  const next = new Set(selected);
  if (typeof ids === "string") {
    if (next.has(ids)) next.delete(ids);
    else next.add(ids);
    return next;
  }
  const on = ids.some((id) => !next.has(id));
  for (const id of ids) {
    if (on) next.add(id);
    else next.delete(id);
  }
  return next;
}

export interface BulkResult {
  deleted: AgentInfo[];
  failed: { agent: AgentInfo; error: unknown }[];
}

/**
 * Delete the agents one after another (each delete tears down AgentCore
 * resources; running them in parallel only competes for the same API limits).
 * A failure does not stop the rest — the caller reports which ones remain.
 */
export async function deleteMany(agents: AgentInfo[], del: (id: string) => Promise<unknown>): Promise<BulkResult> {
  const out: BulkResult = { deleted: [], failed: [] };
  for (const agent of agents) {
    try {
      await del(agent.id);
      out.deleted.push(agent);
    } catch (error) {
      out.failed.push({ agent, error });
    }
  }
  return out;
}
