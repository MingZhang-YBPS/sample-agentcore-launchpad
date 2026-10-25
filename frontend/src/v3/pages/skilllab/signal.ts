import type { SkillLabJobInfo } from "../../../lib/api";
import type { Signal } from "../../ui";

/** A Skill Lab job: queued / running are in flight; failed needs a look; interrupted can resume. */
export function jobSignal(job: Pick<SkillLabJobInfo, "status">): Signal {
  switch (job.status) {
    case "succeeded":
      return "ok";
    case "queued":
    case "running":
      return "wait";
    case "failed":
      return "act";
    case "interrupted":
      return "wait";
    default:
      return "off";
  }
}

/** Jobs that ask for a person: still running (watch / cancel) or stopped badly (resume / read the error). */
export const needsLook = (job: SkillLabJobInfo) =>
  job.status === "queued" || job.status === "running" || job.status === "failed" || job.status === "interrupted";

/** A Skill Lab URL on V3, with V2's `?tab=&view=&id=` states. */
export const slUrl = (params: Record<string, string>) => `/v3/skill-lab?${new URLSearchParams(params).toString()}`;
