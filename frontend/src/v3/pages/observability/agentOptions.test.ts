import { describe, expect, it } from "vitest";

import type { AgentInfo } from "../../../lib/api";
import { agentOptions, byocCodeZipWithoutAdot } from "./agentOptions";

function agent(name: string, over: Partial<AgentInfo> = {}): AgentInfo {
  return {
    id: `id-${name}`,
    name,
    method: "zip_runtime",
    status: "active",
    arn: null,
    resource_id: `${name}-rt`,
    version: null,
    owner: "",
    error: null,
    spec: {},
    created_at: null,
    updated_at: null,
    ...over,
  } as AgentInfo;
}

const codeZip = (adot?: boolean) =>
  agent("legacy", { method: "byoc", spec: { byoc: { artifact_kind: "code_zip", provenance: adot === undefined ? {} : { adot_launcher: adot } } } });

describe("agentOptions", () => {
  it("lists agents with spans first, then the greyed ones", () => {
    const opts = agentOptions(["beta", "alpha"], [agent("alpha"), agent("beta"), agent("quiet")]);
    expect(opts.map((o) => [o.value, o.noSpans])).toEqual([
      ["alpha", null],
      ["beta", null],
      ["quiet", "range"],
    ]);
  });

  it("names a BYOC code zip without the ADOT launcher as the reason", () => {
    expect(agentOptions([], [codeZip()])[0].noSpans).toBe("byocNoAdot");
    expect(agentOptions([], [codeZip(false)])[0].noSpans).toBe("byocNoAdot");
    expect(agentOptions([], [codeZip(true)])[0].noSpans).toBe("range");
    // a self-instrumented zip that does write spans is not greyed
    expect(agentOptions(["legacy"], [codeZip()])[0].noSpans).toBeNull();
  });

  it("keeps span agents that are not in the agent list (raw service names)", () => {
    const opts = agentOptions(["svc.DEFAULT", ""], [agent("alpha")]);
    expect(opts.map((o) => o.value)).toEqual(["svc.DEFAULT", "alpha"]);
  });

  it("lists only the viewer's agents: a hidden one is in neither input", () => {
    // the server filters both the agent list and the span rows for the viewer
    expect(agentOptions([], [agent("mine")]).map((o) => o.value)).toEqual(["mine"]);
  });

  it("skips undeployed agents and MCP servers unless their spans show up", () => {
    const opts = agentOptions([], [
      agent("draft", { status: "draft", resource_id: null }),
      agent("mcp", { spec: { protocol: "mcp" } }),
      agent("gone", { status: "deleted" }),
    ]);
    expect(opts).toEqual([]);
    expect(agentOptions(["mcp"], [agent("mcp", { spec: { protocol: "mcp" } })])[0].noSpans).toBeNull();
  });

  it("labels by display name, filters by ledger name; tolerates a missing list", () => {
    expect(agentOptions([], [agent("a", { display_name: "客服" })])[0]).toEqual({ value: "a", label: "客服", noSpans: "range" });
    expect(agentOptions(["x"], null)).toEqual([{ value: "x", label: "x", noSpans: null }]);
  });
});

describe("byocCodeZipWithoutAdot", () => {
  it("only flags code zips", () => {
    expect(byocCodeZipWithoutAdot(codeZip())).toBe(true);
    expect(byocCodeZipWithoutAdot(agent("img", { method: "byoc", spec: { byoc: { artifact_kind: "container_image" } } }))).toBe(false);
    expect(byocCodeZipWithoutAdot(agent("zip"))).toBe(false);
  });
});
