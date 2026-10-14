import { describe, expect, it } from "vitest";
import type { ByocUploadInfo } from "./api";
import { adotLauncher } from "./byocTelemetry";

const base: ByocUploadInfo["detected"] = {
  entrypoint_candidates: ["main.py"],
  has_requirements: true,
  has_dockerfile: false,
  agentcore_sdk_detected: true,
};

const withAdot = (source: "requirements" | "vendored" | null) =>
  ({ ...base, adot_detected: source !== null, adot_source: source }) as ByocUploadInfo["detected"];

describe("adotLauncher", () => {
  it("is on when requirements list ADOT and the deploy installs them", () => {
    expect(adotLauncher(withAdot("requirements"), true)).toBe(true);
  });
  it("is off when requirements list ADOT but the install is skipped", () => {
    expect(adotLauncher(withAdot("requirements"), false)).toBe(false);
  });
  it("is on for a vendored distro regardless of the install toggle", () => {
    expect(adotLauncher(withAdot("vendored"), false)).toBe(true);
  });
  it("is off without ADOT, for old manifests and without an upload", () => {
    expect(adotLauncher(withAdot(null), true)).toBe(false);
    expect(adotLauncher(base, true)).toBe(false);
    expect(adotLauncher(undefined, true)).toBe(false);
  });
});
