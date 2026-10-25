import { describe, expect, it } from "vitest";

import { sharedFromOf } from "./sharedFrom";

describe("sharedFromOf", () => {
  it("reads the lending project off a list row", () => {
    expect(sharedFromOf({ id: "d1", shared_from: { project_id: "alpha", name: "Alpha", access: "use" } }))
      .toEqual({ project_id: "alpha", name: "Alpha", access: "use" });
  });

  it("is null for the project's own rows and malformed values", () => {
    expect(sharedFromOf({ id: "d1" })).toBeNull();
    expect(sharedFromOf(null)).toBeNull();
    expect(sharedFromOf({ shared_from: "alpha" })).toBeNull();
    expect(sharedFromOf({ shared_from: { name: "Alpha" } })).toBeNull();
  });

  it("falls back to the project id and to read", () => {
    expect(sharedFromOf({ shared_from: { project_id: "alpha", access: "write" } }))
      .toEqual({ project_id: "alpha", name: "alpha", access: "read" });
  });
});
