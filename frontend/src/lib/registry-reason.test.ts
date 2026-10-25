import { describe, expect, it } from "vitest";

import { statusReasonText } from "./registry";

describe("statusReasonText", () => {
  const t = (k: string) => `T:${k}`;
  it("translates the reasons the platform writes", () => {
    expect(statusReasonText("approved via launchpad console", t)).toBe("T:registry.drawer.reason.approved");
    expect(statusReasonText("Rejected via Launchpad console ", t)).toBe("T:registry.drawer.reason.rejected");
  });
  it("passes a reason a person typed through", () => {
    expect(statusReasonText("缺少退款条款引用", t)).toBe("缺少退款条款引用");
  });
});
