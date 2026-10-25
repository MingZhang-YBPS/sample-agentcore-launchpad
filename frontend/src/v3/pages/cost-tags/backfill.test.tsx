import { renderToStaticMarkup } from "react-dom/server";
import { beforeAll, describe, expect, it, vi } from "vitest";

import i18n from "../../../i18n";
import { backfillActive, type BackfillResource, type BackfillRun } from "../../../lib/api/costTags";
import { BackfillRunView } from "./Backfill";
import { coverageSegments, kindRows, resourceLabel, sortOpen } from "./model";

const res = (over: Partial<BackfillResource>): BackfillResource => ({
  key: `${over.kind ?? "runtime"}:${over.arn ?? over.name ?? "x"}`, kind: "runtime", arn: "", name: "",
  project_id: "alpha", agent_id: null, status: "pending", detail: "", ...over,
});

const run = (over: Partial<BackfillRun> = {}): BackfillRun => ({
  id: "job-1", status: "succeeded", error: null, workspace_id: "ws-1", project_id: null, scope: {},
  reason: "admin", actor: "admin", created_at: "2026-10-16T08:00:00Z",
  stages: [
    { name: "plan", status: "succeeded", detail: "6 resource(s) to tag" },
    { name: "tag", status: "succeeded", detail: "3 tagged, 1 unsupported, 1 denied, 1 failed" },
  ],
  coverage: {
    total: 6, taggable: 5, tagged: 3, percent: 60,
    by_status: { pending: 0, tagged: 3, unsupported: 1, denied: 1, missing: 0, failed: 1 },
    by_kind: { runtime: { tagged: 2, denied: 1 }, data_source: { unsupported: 1 }, iam_role: { tagged: 1, failed: 1 } },
  },
  resources: [
    res({ kind: "iam_role", name: "lp-role", status: "failed", detail: "ThrottlingException" }),
    res({ kind: "runtime", arn: "arn:aws:bedrock-agentcore:us-east-1:1:runtime/rt_abc", status: "denied", detail: "AccessDeniedException" }),
  ],
  ...over,
});

describe("back-fill coverage model", () => {
  it("draws the bar in reading order, empty results dropped", () => {
    const segs = coverageSegments(run().coverage);
    expect(segs.map((s) => s.status)).toEqual(["tagged", "denied", "failed", "unsupported"]);
    expect(segs[0].pct).toBeCloseTo(50);
    expect(coverageSegments({ ...run().coverage, total: 0, by_status: {} })).toEqual([]);
  });

  it("lists the kinds with the most still open first", () => {
    expect(kindRows(run().coverage)).toEqual([
      { kind: "runtime", total: 3, tagged: 2, unsupported: 0, open: 1 },
      { kind: "iam_role", total: 2, tagged: 1, unsupported: 0, open: 1 },
      { kind: "data_source", total: 1, tagged: 0, unsupported: 1, open: 0 },
    ]);
  });

  it("puts what an administrator can fix first and names resources by name or ARN", () => {
    const sorted = sortOpen([res({ status: "pending", name: "p" }), res({ status: "missing", name: "m" }), res({ status: "denied", name: "d" })]);
    expect(sorted.map((r) => r.status)).toEqual(["denied", "missing", "pending"]);
    expect(resourceLabel(res({ arn: "arn:aws:bedrock-agentcore:us-east-1:1:runtime/rt_abc" }))).toBe("rt_abc");
    expect(resourceLabel(res({ name: "lp-role", arn: "arn:aws:iam::1:role/lp-role" }))).toBe("lp-role");
  });

  it("polls only a run that will still change", () => {
    expect(backfillActive(run({ status: "queued" }))).toBe(true);
    expect(backfillActive(run({ status: "running" }))).toBe(true);
    expect(backfillActive(run())).toBe(false);
    expect(backfillActive(null)).toBe(false);
  });
});

describe("back-fill run view", () => {
  beforeAll(async () => {
    vi.stubGlobal("document", { documentElement: {} });
    await i18n.changeLanguage("en");
  });

  it("shows the coverage, the stages and the resources it could not tag", () => {
    const html = renderToStaticMarkup(<BackfillRunView run={run()} />);
    expect(html).toContain("60");
    expect(html).toContain("3 of 5 that can carry tags");
    expect(html).toContain("Inventory");
    expect(html).toContain("6 resource(s) to tag");
    expect(html).toContain("2 resources not tagged");
    expect(html).toContain("rt_abc");
    expect(html).toContain("AccessDeniedException");
    expect(html).toContain("iam:TagRole");
    expect(html).toContain("IAM role");
    expect(html).not.toContain('data-live="true"');
  });

  it("marks a running run live and explains a failed one", () => {
    const live = renderToStaticMarkup(<BackfillRunView run={run({ status: "running", coverage: { ...run().coverage, by_status: { pending: 2, tagged: 4 } } })} />);
    expect(live).toContain('data-live="true"');
    const failed = renderToStaticMarkup(<BackfillRunView run={run({ status: "failed", error: "RuntimeError: boom" })} />);
    expect(failed).toContain("RuntimeError: boom");
  });
});
