import type { AgentPermissions } from "../../../lib/agent-admin/permissions";
import type { AgentInfo } from "../../../lib/api";

type AgentLike = Pick<AgentInfo, "method" | "status" | "system">;

export interface MoreEntry {
  key: "convert" | "rollback" | "delete" | "remove";
  disabled: boolean;
  /** `lock` ⇒ the prod-lock title explains why it is disabled */
  why: "lock" | null;
  danger?: boolean;
}

/**
 * "⋯ More": convert (active Harness only), roll back (re-publishes, so the edit
 * rule; never an import), delete — "remove" for an imported runtime. An entry
 * without the permission is left out; the prod lock disables, it does not hide.
 */
export function moreEntries(agent: AgentLike, perms: AgentPermissions): MoreEntry[] {
  const imported = agent.method === "discovered_runtime";
  const lock = perms.locked ? ("lock" as const) : null;
  const out: MoreEntry[] = [];
  if (perms.canConvert(agent)) out.push({ key: "convert", disabled: perms.locked, why: lock });
  if (!imported && perms.canEdit(agent)) out.push({ key: "rollback", disabled: perms.locked, why: lock });
  if (perms.canDelete(agent)) out.push({ key: imported ? "remove" : "delete", disabled: perms.locked, why: lock, danger: true });
  return out;
}
