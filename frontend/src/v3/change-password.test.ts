import { describe, expect, it } from "vitest";

import { ApiError } from "../lib/api";
import { isSsoPasswordRefusal } from "../lib/api/sso";

describe("isSsoPasswordRefusal", () => {
  it("knows the SSO account's refusal from any other failure", () => {
    expect(isSsoPasswordRefusal(new ApiError("auth.sso_account_no_password", "m", null))).toBe(true);
    expect(isSsoPasswordRefusal(new ApiError("auth.invalid_current_password", "m", null))).toBe(false);
    expect(isSsoPasswordRefusal(new Error("auth.sso_account_no_password"))).toBe(false);
  });
});
