import { describe, expect, it } from "vitest";

import { projectLock } from "./locks";

const t = (key: string) => key;

describe("projectLock", () => {
  it("names the archive before the missing ability", () => {
    expect(projectLock(t, { status: "archived" }, false, "denied")).toBe("story.reason.projectArchived");
    expect(projectLock(t, { status: "archived" }, true, "denied")).toBe("story.reason.projectArchived");
  });

  it("gives the denied reason only when the ability is missing", () => {
    expect(projectLock(t, { status: "active" }, false, "denied")).toBe("denied");
    expect(projectLock(t, { status: "active" }, true, "denied")).toBeNull();
  });
});
