import { useAuth } from "../../auth/auth-context";
import { useProdLock } from "../../workspace/useProdLock";
import type { AgentInfo } from "../api";

type AgentLike = Pick<AgentInfo, "method" | "status" | "system">;

/** Why an edit control is shown but disabled (i18n key suffix under `v3.agent.editBlocked`). */
export type EditBlocked = "presetAdminOnly" | "deploying" | "locked";

export interface AgentPermissionInput {
  canDeploy: boolean;
  canDelete: boolean;
  canConvert: boolean;
  isAdmin: boolean;
  /** prod workspace (T05): controls stay visible but disabled */
  locked: boolean;
  lockTitle: string | undefined;
}

export interface AgentPermissions {
  locked: boolean;
  lockTitle: string | undefined;
  /** editing re-publishes; a system preset is edited by administrators only */
  canEdit: (a: AgentLike) => boolean;
  /** a system preset is never deleted here */
  canDelete: (a: AgentLike) => boolean;
  /** Harness → code, active Harness agents only, never a preset */
  canConvert: (a: AgentLike) => boolean;
  /**
   * The edit control for the V3 detail page: hidden without the permission (or
   * for an imported runtime, which has no editor), else shown — disabled with the
   * reason while a deploy runs, for a preset when not an administrator, and on a
   * locked prod workspace.
   */
  editState: (a: AgentLike) => { visible: boolean; blocked: EditBlocked | null };
}

/** The permission rules as plain predicates (unit-tested; V2 and V3 share them). */
export function agentPermissions(input: AgentPermissionInput): AgentPermissions {
  const { canDeploy, canDelete, canConvert, isAdmin, locked, lockTitle } = input;
  return {
    locked,
    lockTitle,
    canEdit: (a) => a.method !== "discovered_runtime" && canDeploy && a.status !== "deploying" && (!a.system || isAdmin),
    canDelete: (a) => canDelete && !a.system,
    canConvert: (a) => a.method === "harness" && a.status === "active" && canConvert && !a.system,
    editState: (a) => {
      if (a.method === "discovered_runtime" || !canDeploy) return { visible: false, blocked: null };
      if (a.system && !isAdmin) return { visible: true, blocked: "presetAdminOnly" };
      if (a.status === "deploying") return { visible: true, blocked: "deploying" };
      if (locked) return { visible: true, blocked: "locked" };
      return { visible: true, blocked: null };
    },
  };
}

/** Row / page permissions from the signed-in user and the workspace's prod lock. */
export function useAgentPermissions(): AgentPermissions {
  const { can, isAdmin } = useAuth();
  const { locked, title } = useProdLock();
  return agentPermissions({
    canDeploy: can("agents.deploy"),
    canDelete: can("agents.delete"),
    canConvert: can("agents.convert"),
    isAdmin,
    locked,
    lockTitle: title,
  });
}
