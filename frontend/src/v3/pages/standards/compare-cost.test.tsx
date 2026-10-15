import { renderToStaticMarkup } from "react-dom/server";
import { beforeAll, describe, expect, it, vi } from "vitest";

import i18n from "../../../i18n";
import type { CostDelta } from "../../../lib/dlc";
import { CompareCost } from "./CompareCost";
import { costDeltaRows, costDeltaSignal } from "./costDelta";

beforeAll(async () => {
  vi.stubGlobal("document", { documentElement: {} });
  await i18n.changeLanguage("en");
});

const DELTA: CostDelta = {
  before_usd: 1.8,
  after_usd: 1.93,
  delta_usd: 0.13,
  delta_pct: 7.2,
  parts: { agent_usd: 0.2, runtime_usd: -0.1, judge_usd: 0.03, managed_evaluations_usd: null },
};

describe("costDeltaRows", () => {
  it("keeps the measured parts, scaled to the widest swing", () => {
    const rows = costDeltaRows(DELTA);
    expect(rows.map((r) => r.key)).toEqual(["agent_usd", "runtime_usd", "judge_usd"]);
    expect(rows[0].reach).toBe(1);
    expect(rows[1].reach).toBeCloseTo(0.5);
    expect(costDeltaRows({ ...DELTA, parts: {} })).toEqual([]);
  });

  it("signals a dearer run, a cheaper one, and no change", () => {
    expect(costDeltaSignal(0.13)).toBe("wait");
    expect(costDeltaSignal(-0.2)).toBe("ok");
    expect(costDeltaSignal(0)).toBeUndefined();
    expect(costDeltaSignal(null)).toBeUndefined();
  });
});

describe("CompareCost", () => {
  it("draws first → latest with the signed difference and each part", () => {
    const html = renderToStaticMarkup(<CompareCost delta={DELTA} />);
    expect(html).toContain("Cost change");
    expect(html).toContain("$1.80");
    expect(html).toContain("$1.93");
    expect(html).toContain("+$0.13 · +7.2%");
    expect(html).toContain('data-side="down"');
    expect(html).toContain("−$0.10");
  });

  it("says so when neither run was measured, and draws nothing without a delta", () => {
    const html = renderToStaticMarkup(
      <CompareCost delta={{ before_usd: null, after_usd: null, delta_usd: null, delta_pct: null, parts: {} }} />,
    );
    expect(html).toContain("Neither run has a measured cost yet");
    expect(renderToStaticMarkup(<CompareCost delta={undefined} />)).toBe("");
  });
});
