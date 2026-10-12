import type { DeploymentInfo, StageInfo } from "../../../lib/api";

/** i18n key (under `v2.agents.progress`) for the plain-language line of the stage
 *  now running. Harness has no build, so one line covers every stage. */
export function progressKey(method: string, stage: StageInfo | undefined): string {
  if (method === "harness") return "harness";
  switch (stage?.name) {
    case "package":
      if (method === "zip_runtime") return "zipPackage";
      if (method === "container" || method === "studio") return "containerBuild";
      return "package";
    case "register":
      return "register";
    case "generate":
      return "prepare";
    default:
      return "start";
  }
}

export function runningStage(deployment: DeploymentInfo): StageInfo | undefined {
  return (
    deployment.stages.find((s) => s.status === "running") ??
    deployment.stages.find((s) => s.status === "pending")
  );
}

/** Parse a ledger timestamp; the API emits naive UTC ISO strings. */
export function parseUtc(value: string | null | undefined): number | null {
  if (!value) return null;
  const ms = Date.parse(/(Z|[+-]\d\d:?\d\d)$/.test(value) ? value : `${value}Z`);
  return Number.isNaN(ms) ? null : ms;
}

export function fmtElapsed(ms: number): string {
  const s = Math.max(0, Math.floor(ms / 1000));
  return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, "0")}s`;
}
