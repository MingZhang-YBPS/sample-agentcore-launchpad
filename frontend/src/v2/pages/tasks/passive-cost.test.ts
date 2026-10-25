import { describe, expect, it } from "vitest";

import { isPassiveSource, passiveEstimateBody, type PricedDraft } from "./passiveCost";

const draft = (over: Partial<PricedDraft> = {}): PricedDraft => ({
  target: "agent", agentId: "a1", mode: "evaluators", source: "window", strategy: "history",
  lookbackHours: 24, sessionIds: ["s1", "s2"], logSessionIds: ["l1"], sampling: 10, evaluators: ["Builtin.Correctness"], ...over,
});

describe("V2 wizard passive / online estimate", () => {
  it("prices a look-back window, picked sessions and picked log streams", () => {
    expect(passiveEstimateBody(draft())).toEqual({ agent_id: "a1", evaluators: ["Builtin.Correctness"], scope: "passive", lookback_hours: 24 });
    expect(passiveEstimateBody(draft({ source: "sessions" }))).toMatchObject({ scope: "passive", sessions: 2 });
    expect(passiveEstimateBody(draft({ source: "logs", target: "cloudwatch" }))).toEqual({ evaluators: ["Builtin.Correctness"], scope: "passive", sessions: 1 });
  });

  it("prices a continuous config per day", () => {
    expect(passiveEstimateBody(draft({ strategy: "continuous", sampling: 25 }))).toMatchObject({ scope: "online", sampling_percentage: 25 });
  });

  it("leaves out insights, datasets and an unfinished draft", () => {
    expect(passiveEstimateBody(draft({ mode: "insights" }))).toBeNull();
    expect(passiveEstimateBody(draft({ source: "dataset" }))).toBeNull();
    expect(passiveEstimateBody(draft({ agentId: "" }))).toBeNull();
    expect(passiveEstimateBody(draft({ evaluators: [] }))).toBeNull();
    expect(passiveEstimateBody(null)).toBeNull();
  });

  it("names the passive sources", () => {
    expect(["window", "sessions", "logs", "dataset"].map((s) => isPassiveSource(s as PricedDraft["source"]))).toEqual([true, true, true, false]);
  });
});
