// Why a project control is off (storyline-ux §4): an archived project takes no
// changes at all; otherwise the caller's ability decides, with the reason that
// names who can. Every settings section asks the same two questions in this order.
import type { DisabledReason } from "../../reasons";

type T = (key: string, opts?: Record<string, unknown>) => string;

/** null when the control is free; the archived reason first, then `denied`. */
export function projectLock(
  t: T,
  project: { status: string },
  allowed: boolean,
  denied: DisabledReason,
): DisabledReason | null {
  if (project.status !== "active") return t("story.reason.projectArchived");
  return allowed ? null : denied;
}
