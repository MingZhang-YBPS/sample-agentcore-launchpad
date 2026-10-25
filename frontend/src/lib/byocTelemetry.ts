import type { ByocUploadInfo } from "./api";

/**
 * Upload-time ADOT detection (projects §17.3-1). The manifest fields are
 * typed here until `ByocUploadInfo.detected` declares them; manifests staged
 * before the check existed carry neither.
 */
export interface AdotDetection {
  adot_detected?: boolean;
  adot_source?: "requirements" | "vendored" | null;
}

/**
 * Whether the deploy wraps the code zip's entrypoint in the
 * `opentelemetry-instrument` launcher — the same rule as the backend's
 * `byoc_uploads.adot_launcher`: the distro must end up in the bundle, either
 * vendored in the zip or installed from requirements.txt.
 */
export function adotLauncher(detected: ByocUploadInfo["detected"] | undefined, installRequirements: boolean): boolean {
  const adot = (detected ?? {}) as AdotDetection;
  if (!adot.adot_detected) return false;
  return adot.adot_source === "vendored" || installRequirements;
}
