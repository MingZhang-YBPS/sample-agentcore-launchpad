import { describe, expect, it } from "vitest";

import { hasConsolePassword } from "../auth/auth-context";
import { ApiError } from "../lib/api";
import { isSsoPasswordRefusal } from "../lib/api/sso";

describe("isSsoPasswordRefusal", () => {
  it("knows the SSO account's refusal from any other failure", () => {
    expect(isSsoPasswordRefusal(new ApiError("auth.sso_account_no_password", "m", null))).toBe(true);
    expect(isSsoPasswordRefusal(new ApiError("auth.invalid_current_password", "m", null))).toBe(false);
    expect(isSsoPasswordRefusal(new Error("auth.sso_account_no_password"))).toBe(false);
  });
});

describe("hasConsolePassword", () => {
  it("hides change-password for SSO sessions and open consoles", () => {
    expect(hasConsolePassword(true, "local")).toBe(true);
    expect(hasConsolePassword(true, null)).toBe(true);
    expect(hasConsolePassword(true, "sso")).toBe(false);
    expect(hasConsolePassword(false, null)).toBe(false);
  });
});
