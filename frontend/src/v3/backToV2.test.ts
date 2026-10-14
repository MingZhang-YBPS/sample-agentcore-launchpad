import { describe, expect, it } from "vitest";

import { V2_NAV } from "../v2/nav";
import { v2CounterpartOf } from "./backToV2";
import { hostedGroups, v3TwinOf } from "./nav";

describe("v2CounterpartOf", () => {
  it("leaves a hosted V2 page where it is", () => {
    expect(v2CounterpartOf("/v2/registry", "?view=register")).toBeNull();
    expect(v2CounterpartOf("/v3x", "")).toBeNull();
  });

  it("keeps the native pages' own mapping", () => {
    expect(v2CounterpartOf("/v3", "")).toBe("/v2");
    expect(v2CounterpartOf("/v3/", "")).toBe("/v2");
    expect(v2CounterpartOf("/v3/chat", "?agent=a")).toBe("/v2/chat?agent=a");
    expect(v2CounterpartOf("/v3/agents", "?id=a 1")).toBe("/v2/agents?view=detail&id=a%201");
    expect(v2CounterpartOf("/v3/agents", "")).toBe("/v2/agents");
    expect(v2CounterpartOf("/v3/create", "")).toBe("/v2/agents?view=new");
    expect(v2CounterpartOf("/v3/gate", "")).toBe("/v2/eval/standards");
  });

  it("maps a rebuilt module page to its V2 module instead of /v2", () => {
    expect(v2CounterpartOf("/v3/registry", "")).toBe("/v2/registry");
    expect(v2CounterpartOf("/v3/knowledge", "")).toBe("/v2/knowledge-bases");
    expect(v2CounterpartOf("/v3/tasks", "")).toBe("/v2/eval/tasks");
    expect(v2CounterpartOf("/v3/identity", "")).toBe("/v2/connections");
    expect(v2CounterpartOf("/v3/releases", "")).toBe("/v2/promotions");
  });

  it("carries the record and the tab across", () => {
    expect(v2CounterpartOf("/v3/registry", "?id=r1")).toBe("/v2/registry?view=detail&id=r1");
    expect(v2CounterpartOf("/v3/releases", "?id=p1")).toBe("/v2/promotions?view=detail&id=p1");
    expect(v2CounterpartOf("/v3/governance", "?tab=tools")).toBe("/v2/governance?tab=tools");
    expect(v2CounterpartOf("/v3/tasks", "?view=detail&id=t1")).toBe("/v2/eval/tasks?view=detail&id=t1");
  });

  it("sends a V3-only page to the V2 overview", () => {
    expect(v2CounterpartOf("/v3/models", "")).toBe("/v2");
  });

  it("round-trips every rebuilt rail entry", () => {
    const pages = hostedGroups(true).flatMap((g) => g.items).map((i) => i.to.split("?")[0]).filter((to) => to.startsWith("/v3/"));
    const v2Paths = new Set(V2_NAV.flatMap((g) => g.items).map((i) => i.to.split("?")[0]));
    for (const page of pages) {
      const back = v2CounterpartOf(page, "");
      if (page === "/v3/models" || page === "/v3/create") continue;
      expect(v2Paths.has(back ?? ""), `${page} → ${back}`).toBe(true);
      expect(v3TwinOf(back ?? "", "")?.split("?")[0]).toBe(page);
    }
  });
});
