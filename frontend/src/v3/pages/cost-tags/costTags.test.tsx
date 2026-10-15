import { renderToStaticMarkup } from "react-dom/server";
import { MemoryRouter } from "react-router-dom";
import { beforeAll, describe, expect, it, vi } from "vitest";

import i18n from "../../../i18n";
import { type ActivationStatus, activationSummary, type BillingReport, type CostTagsView, rowErrors, sameTags, tagsOf } from "../../../lib/api/costTags";
import { V3_AFTER } from "../../nav";
import { Activation, Ledger } from "./CostTags";
import { activatable, gap, pointTotals, stageOf } from "./model";

const NOW = Date.parse("2026-10-15T12:00:00Z");

const status = (over: Partial<ActivationStatus> = {}): ActivationStatus => ({
  account_id: "111122223333",
  account_type: "standalone",
  can_activate: true,
  management_account_id: null,
  project_tag_active: false,
  tags: [
    { key: "launchpad:project", custom: false, status: "inactive", last_updated: null, last_used: null },
    { key: "launchpad:workspace", custom: false, status: "active", last_updated: null, last_used: null },
    { key: "launchpad:agent-id", custom: false, status: "not_found", last_updated: null, last_used: null },
    { key: "CostCenter", custom: true, status: "inactive", last_updated: null, last_used: null },
  ],
  error: null,
  checked_at: "2026-10-15T11:00:00Z",
  activation_delay_hours: 24,
  ...over,
});

const render = (node: JSX.Element) => renderToStaticMarkup(<MemoryRouter>{node}</MemoryRouter>);

describe("custom cost tag rows", () => {
  it("names each row's problem like the backend", () => {
    expect(rowErrors([
      { key: "", value: "x" },
      { key: "aws:x", value: "1" },
      { key: "Launchpad:project", value: "1" },
      { key: "bad#", value: "1" },
      { key: "k", value: "v*" },
      { key: "Cost", value: "1" },
      { key: "cost", value: "2" },
      { key: "k".repeat(129), value: "" },
      { key: "团队", value: "a/b@c" },
    ])).toEqual(["empty", "reserved", "reserved", "chars", "chars", "duplicate", "duplicate", "long", null]);
  });

  it("builds the body only when every row is valid and within the limit", () => {
    expect(tagsOf([{ key: " CostCenter ", value: " 1234 " }])).toEqual({ CostCenter: "1234" });
    expect(tagsOf([{ key: "aws:x", value: "1" }])).toBeNull();
    expect(tagsOf(Array.from({ length: 11 }, (_, i) => ({ key: `k${i}`, value: "v" })))).toBeNull();
    expect(sameTags({ a: "1" }, { a: "1" })).toBe(true);
    expect(sameTags({ a: "1" }, { a: "2" })).toBe(false);
    expect(sameTags({}, { a: "1" })).toBe(false);
  });
});

describe("activation model", () => {
  it("counts the keys and lists what a click activates", () => {
    expect(activationSummary(status())).toEqual({ total: 4, active: 1, inactive: 2, notFound: 1 });
    expect(activatable(status())).toEqual(["launchpad:project", "CostCenter"]);
  });

  it("places the workspace on the path to billed cost", () => {
    expect(stageOf(status(), NOW)).toBe("inactive");
    expect(stageOf(status({ error: { code: "AccessDeniedException", message: "no" } }), NOW)).toBe("unreadable");
    const active = (when: string) => status({ tags: [{ key: "launchpad:project", custom: false, status: "active", last_updated: when, last_used: null }] });
    expect(stageOf(active("2026-10-15T06:00:00Z"), NOW)).toBe("settling");
    expect(stageOf(active("2026-10-01T06:00:00Z"), NOW)).toBe("billing");
  });
});

describe("billing model", () => {
  it("sums each period and compares billed with estimated", () => {
    const report = {
      series: [
        { start: "a", end: "b", estimated: false, by_project: { alpha: 1, beta: 2 }, shared_usd: 0.5, untagged_usd: 1 },
        { start: "b", end: "c", estimated: true, by_project: {}, shared_usd: null, untagged_usd: null },
      ],
    } as unknown as BillingReport;
    expect(pointTotals(report)).toEqual([4.5, 0]);
    expect(gap(12, 10)).toBeCloseTo(0.2);
    expect(gap(5, 0)).toBeNull();
    expect(gap(5, null)).toBeNull();
  });
});

describe("cost tags page", () => {
  beforeAll(async () => {
    vi.stubGlobal("document", { documentElement: {} });
    await i18n.changeLanguage("en");
  });

  it("offers one-click activation in a standalone account", () => {
    const html = render(<Activation status={status()} onActivated={() => {}} />);
    expect(html).toContain("Activate 2 keys");
    expect(html).toContain("of 4 keys active");
  });

  it("gives an organization member account the steps instead", () => {
    const html = render(<Activation status={status({ account_type: "member", can_activate: false, management_account_id: "999988887777" })} onActivated={() => {}} />);
    expect(html).not.toContain("Activate 2 keys");
    expect(html).toContain("999988887777");
    expect(html).toContain("Cost allocation tags");
  });

  it("explains an unreadable status", () => {
    const html = render(<Activation status={status({ error: { code: "AccessDeniedException", message: "denied" }, can_activate: false })} onActivated={() => {}} />);
    expect(html).toContain("ce:ListCostAllocationTags");
  });

  it("lists the tag set with each key's state", () => {
    const view: CostTagsView = {
      tag_set: [{ key: "launchpad:project", value: "project id", role: "primary" }, { key: "launchpad:workspace", value: "workspace id", role: "dimension" }],
      shared_value: "shared", custom_max: 10, reserved_prefixes: ["aws:", "launchpad:"], activation: status(),
    };
    const html = render(<Ledger view={view} />);
    expect(html).toContain("the project id");
    expect(html).toContain("primary");
    expect(html).toContain("custom");
    expect(html).toContain("Not seen yet");
    expect(html).toContain("launchpad:project=shared");
  });

  it("is a Configure entry for administrators", () => {
    const entry = V3_AFTER["/v2/workspaces"].find((e) => e.to === "/v3/cost-tags");
    expect(entry?.admin).toBe(true);
    expect(i18n.t(entry!.labelKey)).toBe("Cost tags");
  });
});
