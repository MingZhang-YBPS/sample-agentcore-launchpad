import { describe, expect, it } from "vitest";

import { estimateValue, metricValueLabel, waiverGapLabel } from "../../lib/dlc";

describe("gate report: metric criteria", () => {
  it("shows a latency in milliseconds against its bound, not as a rate", () => {
    const label = metricValueLabel({ metric_rule: { metric: "latency_p95_ms", op: "<=", value: 30000 }, value: 25671.38, measured: 25671.38 });
    expect(label).toBe("25,671 ms (<= 30,000)");
    expect(label).not.toContain("%");
  });

  it("shows tokens per session as a count", () => {
    expect(metricValueLabel({ metric_rule: { metric: "tokens_per_session", op: "<=", value: 500 }, value: 1098.33 })).toBe("1,098 (<= 500)");
  });

  it("falls back to measured, and to a dash when nothing was measured", () => {
    expect(metricValueLabel({ metric_rule: { metric: "tokens_per_session", op: "<=", value: 500 }, measured: 742 })).toBe("742 (<= 500)");
    expect(metricValueLabel({ metric_rule: { metric: "latency_p95_ms", op: "<=", value: 60000 } })).toBe("—");
  });

  it("prices cost per success in dollars", () => {
    expect(metricValueLabel({ metric_rule: { metric: "cost_per_success_usd", op: "<=", value: 0.01 }, value: 0.00712 })).toBe("$0.0071 (<= $0.0100)");
  });
});

describe("waiver table: measured / required", () => {
  const rows = [
    { key: "K1", kind: "metric", metric_rule: { metric: "tokens_per_session", op: "<=", value: 500 } },
    { key: "S1", kind: "evaluator", metric_rule: null },
  ];

  it("shows a waived metric in its unit against its bound", () => {
    expect(waiverGapLabel({ criterion_key: "K1", actual: 1098.33, threshold: 0.9 }, rows)).toBe("1,098 (<= 500)");
  });

  it("keeps percentages for a rate criterion", () => {
    expect(waiverGapLabel({ criterion_key: "S1", actual: 0.917, threshold: 1 }, rows)).toBe("91.7% / 100%");
  });

  it("never shows a metric value as a percentage, even without the report row", () => {
    expect(waiverGapLabel({ criterion_key: "K1", actual: 1098.33, threshold: 0.9 }, [])).toBe("1,098");
    expect(waiverGapLabel({ criterion_key: "K1", actual: null, threshold: 0.9 }, [])).toBe("—");
  });
});

describe("estimated cost figure", () => {
  it("shows a dash while the estimate is loading, not 'unpriced'", () => {
    expect(estimateValue(null, "未定价")).toBe("—");
    expect(estimateValue(undefined, "未定价")).toBe("—");
  });

  it("says unpriced only when the server could not price the run", () => {
    expect(estimateValue({ total_usd: null }, "未定价")).toBe("未定价");
    expect(estimateValue({ total_usd: 1.125 }, "未定价")).toBe("$1.13");
  });
});
