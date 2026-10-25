import { describe, expect, it } from "vitest";

import type { AgentInfo } from "../../lib/api";
import { launchSteps } from "./steps";

const base = {
  workspace: { bootstrap_status: "ready" as const },
  agents: [{ id: "a1", status: "active" }] as Pick<AgentInfo, "id" | "status">[],
  conversed: false,
  evaluated: false,
  releases: {},
};

describe("launchSteps", () => {
  it("ticks each step from what exists, not from clicks", () => {
    const steps = launchSteps(base);
    expect(steps.map((s) => [s.key, s.done])).toEqual([
      ["workspace", true],
      ["agent", true],
      ["chat", false],
      ["evaluate", false],
      ["gate", false],
    ]);
  });

  it("an agent counts only once it is live, and a gate only on the live endpoint", () => {
    const steps = launchSteps({
      ...base,
      agents: [{ id: "a1", status: "deploying" }] as Pick<AgentInfo, "id" | "status">[],
      releases: { a1: { state: { endpoint_mode: "default" } as never } },
    });
    expect(steps.find((s) => s.key === "agent")?.done).toBe(false);
    expect(steps.find((s) => s.key === "gate")?.done).toBe(false);
    const gated = launchSteps({ ...base, releases: { a1: { state: { endpoint_mode: "live" } as never } } });
    expect(gated.find((s) => s.key === "gate")?.done).toBe(true);
  });

  it("points the next actions at the first live agent", () => {
    const steps = launchSteps(base);
    expect(steps.find((s) => s.key === "chat")?.to).toBe("/v3/chat?agent=a1");
    expect(steps.find((s) => s.key === "evaluate")?.to).toBe("/v3/tasks?view=new&agent=a1");
    const none = launchSteps({ ...base, agents: [] });
    expect(none.find((s) => s.key === "chat")?.to).toBe("/v3/chat");
  });
});
