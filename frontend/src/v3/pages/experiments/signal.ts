// Experiment and canary states on the V3 signal vocabulary. A run moving on its
// own is info; one that has reached a verdict and waits on an operator's call is
// wait; promoted / completed is ok; failed / rolled back needs you.
import type { RuntimeCanaryInfo } from "../../../lib/api";
import type { ExperimentInfo } from "../../../lib/experiments";
import type { Signal } from "../../ui";

export function experimentSignal(e: Pick<ExperimentInfo, "status" | "running_action">): Signal {
  if (e.running_action) return "info";
  switch (e.status) {
    case "running":
      return "info";
    case "ready":
      return "wait";
    case "promoted":
      return "ok";
    case "failed":
      return "act";
    default:
      return "off";
  }
}

export function canarySignal(c: Pick<RuntimeCanaryInfo, "status" | "stage" | "running_action">): Signal {
  if (c.running_action) return "info";
  switch (c.status) {
    case "running":
      // a verdict on the table is a ramp / complete / rollback decision for someone
      return c.stage === "verdict" ? "wait" : "info";
    case "completed":
      return "ok";
    case "rolled_back":
      return "act";
    default:
      return "off";
  }
}
