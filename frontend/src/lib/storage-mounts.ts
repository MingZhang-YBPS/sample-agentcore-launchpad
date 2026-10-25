/**
 * BYO mounts (S3 Files / EFS) in the wizard — design storage-mounts §3 (S1).
 *
 * Every method mounts them now (`backend/app/schemas/agent.py` `BYO_MOUNT_METHODS`),
 * always inside the agent's VPC network profile. Until the storage catalog exists a
 * mount is an inline access point ARN, which only an administrator may add or change
 * (D-2; the backend answers `storage.inline_not_allowed` otherwise): a member sees the
 * rows an administrator set, read-only, and may still re-publish with them.
 */
import type { AgentMethod } from "./agent-spec";

/** The wizard's methods plus Studio (published from the canvas, not the wizard). */
export type MountMethod = AgentMethod | "studio";

/** Methods whose spec accepts BYO mounts — kept equal to the backend set. */
export const BYO_MOUNT_METHODS: readonly MountMethod[] = ["harness", "zip_runtime", "studio", "container", "byoc"];

export const byoMethod = (method: MountMethod) => BYO_MOUNT_METHODS.includes(method);

/** What the storage card shows for the BYO rows. */
export type ByoRowsMode = "hidden" | "edit" | "readonly";

/** Hidden where the method mounts none; editable for an administrator; otherwise
 *  read-only, and only when the agent already has some (nothing to show a member). */
export function byoRowsMode(method: MountMethod, isAdmin: boolean, hasRows: boolean): ByoRowsMode {
  if (!byoMethod(method)) return "hidden";
  if (isAdmin) return "edit";
  return hasRows ? "readonly" : "hidden";
}

/**
 * The network a BYO mount needs (§3.2): a network profile. Only a container agent may
 * still carry the legacy inline subnets / security groups instead (they predate
 * profiles); every other method is told to pick a profile in the network step.
 */
export function mountNeedsProfile(form: {
  method: MountMethod;
  networkProfileId: string;
  hasByo: boolean;
}): boolean {
  return form.hasByo && !form.networkProfileId && form.method !== "container";
}
