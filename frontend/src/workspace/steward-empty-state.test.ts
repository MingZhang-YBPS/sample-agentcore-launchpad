import { describe, expect, it } from "vitest";

import { isSteward } from "../lib/api/steward";
import { isStructurePath, showsStewardEmptyState } from "./steward-paths";

describe("the steward's empty state (projects §11.4)", () => {
  it("knows the structure pages, and what lies below them", () => {
    expect(isStructurePath("/v3/projects")).toBe(true);
    expect(isStructurePath("/v3/projects/shop")).toBe(true);
    expect(isStructurePath("/v3/admins")).toBe(true);
    expect(isStructurePath("/v3/projectsx")).toBe(false);
    expect(isStructurePath("/v3/agents")).toBe(false);
  });

  it("shows only to a steward, only on business pages", () => {
    expect(isSteward("steward")).toBe(true);
    expect(isSteward("admin")).toBe(false);
    expect(showsStewardEmptyState("steward", "/v3/agents")).toBe(true);
    expect(showsStewardEmptyState("steward", "/v3/departments")).toBe(false);
    expect(showsStewardEmptyState("admin", "/v3/agents")).toBe(false);
    expect(showsStewardEmptyState(null, "/v3/agents")).toBe(false);
  });
});
