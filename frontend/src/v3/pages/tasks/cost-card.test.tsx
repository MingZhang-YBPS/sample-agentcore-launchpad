import { renderToStaticMarkup } from "react-dom/server";
import { beforeAll, describe, expect, it, vi } from "vitest";

import i18n from "../../../i18n";
import en from "../../../locales/en/common.json";
import zh from "../../../locales/zh-CN/common.json";
import { deltaPct, type EvalCostEstimate, estimateParts, partShares, rangePosition, usd } from "../../../lib/api/evalCost";
import { CostCell, EstimateCostCard } from "./CostCard";

const ESTIMATE: EvalCostEstimate = {
  scope: "replay",
  period: "run",
  items: 10,
  repeats: 2,
  sessions: 20,
  agent_usd: 0.4,
  agent_basis: "history_7d",
  runtime_usd: 0.05,
  runtime_seconds: 1600,
  judge_usd: 0.2,
  managed_usd: 0.1,
  judges: [{ evaluator_id: "Builtin.Correctness", billed_by: "agentcore_evaluations", usd: 0.1, judgements: 40, tokens_per_item: 1800, samples: 0 }],
  components: [
    { key: "agent", usd: 0.4, low: 0.3, high: 0.6, basis: "history_7d" },
    { key: "runtime", usd: 0.05, low: 0.025, high: 0.1, basis: "rough" },
    { key: "judge", usd: 0.2, low: 0.1, high: 0.4, basis: "judges" },
    { key: "managed", usd: 0.1, low: 0.05, high: 0.2, basis: "agentcore_evaluations" },
  ],
  total_usd: 0.75,
  range: { low: 0.45, mid: 0.75, high: 1.3 },
  basis: { agent_samples: 0, duration_samples: 0, judge_samples: {}, prices: { source: "AgentCore list prices (us-east-1)", updated_at: "2026-10-15" } },
  unpriced_models: ["example.unpriced-judge"],
  duration_minutes: 20,
  confirm_required: true,
  confirm_usd: 0.5,
  over_limit: false,
  max_usd: null,
  unpriced: false,
};

describe("evaluation cost helpers", () => {
  it("formats sub-cent amounts finely and missing ones as a dash", () => {
    expect(usd(null)).toBe("—");
    expect(usd(0)).toBe("$0.00");
    expect(usd(0.0042)).toBe("$0.0042");
    expect(usd(1.934)).toBe("$1.93");
  });

  it("splits the bar by each part's share and places the mid on the range", () => {
    const shares = partShares(estimateParts(ESTIMATE));
    expect(shares.map((s) => s.key)).toEqual(["agent", "runtime", "judge", "managed"]);
    expect(shares.reduce((a, s) => a + s.share, 0)).toBeCloseTo(1);
    expect(rangePosition({ low: 1, mid: 1.5, high: 3 })).toBeCloseTo(0.25);
    expect(rangePosition(null)).toBe(0.5);
  });

  it("compares the actual cost with the estimate", () => {
    expect(deltaPct(1.93, 1.8)).toBe(7.2);
    expect(deltaPct(1, null)).toBeNull();
  });

  it("falls back to agent + judge parts for an older estimate", () => {
    const parts = estimateParts({ ...ESTIMATE, components: undefined });
    expect(parts.map((p) => p.key)).toEqual(["agent", "judge"]);
  });
});

describe("cost cards", () => {
  beforeAll(async () => {
    vi.stubGlobal("document", { documentElement: {} });
    await i18n.changeLanguage("en");
  });

  it("renders the estimate with its range, parts, unpriced models and the confirm box", () => {
    const html = renderToStaticMarkup(
      <div className="v3"><EstimateCostCard estimate={ESTIMATE} loading={false} confirmed={false} onConfirm={() => undefined} /></div>,
    );
    expect(html).toContain("Estimated cost");
    expect(html).toContain("$0.75");
    expect(html).toContain("low $0.45");
    expect(html).toContain("Runtime compute");
    expect(html).toContain("example.unpriced-judge");
    expect(html).toContain("AgentCore list prices (us-east-1)");
    expect(html).toContain('type="checkbox"');
  });

  it("marks an over-limit estimate and does not offer the confirm box", () => {
    const html = renderToStaticMarkup(
      <EstimateCostCard estimate={{ ...ESTIMATE, over_limit: true, max_usd: 0.5 }} loading={false} onConfirm={() => undefined} />,
    );
    expect(html).toContain('data-signal="act"');
    expect(html).toContain("Over the $0.50 limit");
    expect(html).not.toContain('type="checkbox"');
  });

  it("labels the list cell's estimate and live values", () => {
    expect(renderToStaticMarkup(<CostCell summary={{ usd: 1.2, kind: "estimate", estimate_usd: 1.2 }} />)).toContain("estimate");
    expect(renderToStaticMarkup(<CostCell summary={{ usd: 0.3, kind: "actual", estimate_usd: 1 }} />)).not.toContain("<small>");
    expect(renderToStaticMarkup(<CostCell summary={undefined} />)).toContain("—");
  });
});

describe("v3.evalCost locale", () => {
  const keys = (obj: unknown, prefix = ""): string[] =>
    Object.entries(obj as Record<string, unknown>).flatMap(([k, v]) =>
      v && typeof v === "object" ? keys(v, `${prefix}${k}.`) : [`${prefix}${k}`],
    );

  it("has the same keys in English and Chinese", () => {
    expect(keys(zh.v3.evalCost).sort()).toEqual(keys(en.v3.evalCost).sort());
  });

  it("uses full-width punctuation in Chinese", () => {
    const values = keys(zh.v3.evalCost).map((k) => k.split(".").reduce<unknown>((o, p) => (o as Record<string, unknown>)[p], zh.v3.evalCost) as string);
    for (const value of values) expect(value).not.toMatch(/[一-鿿][,;:!?]|[,;:!?][一-鿿]/);
  });
});
