/**
 * Region-dependent model-id rules, mirrored from `backend/app/core/regions.py`
 * (`inference_profile_prefix`). Pure logic plus one piece of module state: the
 * region of the workspace the console is currently pointed at, set by
 * `WorkspaceProvider` (the routed subtree remounts on a switch, so reading it at
 * render time is safe).
 *
 * `global.` ids and bare ids work from any region and are always offered. A
 * geographic id (`us.`, `eu.`, `apac.`, `us-gov.`) is offered only in its own
 * geography. Nothing here blocks input: every model field keeps free-text entry.
 */

const GEO_PREFIX_BY_REGION: [RegExp, string][] = [
  [/^us-gov-/, "us-gov"],
  [/^us-/, "us"],
  [/^eu-/, "eu"],
  [/^ap-/, "apac"],
];
const GEO_PREFIXES = ["us-gov", "us", "eu", "apac"];

/** Geographic inference-profile prefix of a region (`us`, `eu`, `apac`, `us-gov`), or null. */
export function inferenceProfilePrefix(region: string): string | null {
  for (const [pattern, prefix] of GEO_PREFIX_BY_REGION) {
    if (pattern.test(region)) return prefix;
  }
  return null;
}

/** False only for a geographic profile id that does not belong to `region`. */
export function modelAvailableInRegion(modelId: string, region: string | null | undefined): boolean {
  if (!region) return true; // region unknown (not loaded yet): do not hide anything
  const head = modelId.split(".", 1)[0];
  if (!modelId.includes(".") || !GEO_PREFIXES.includes(head)) return true;
  return inferenceProfilePrefix(region) === head;
}

let activeRegion: string | null = null;

/** Called by `WorkspaceProvider` with the current workspace's region. */
export function setActiveRegion(region: string | null | undefined): void {
  activeRegion = region ?? null;
}

export function getActiveRegion(): string | null {
  return activeRegion;
}

/** `ids` without the geographic profiles that do not exist in the active region. */
export function modelsForActiveRegion<T extends string>(ids: readonly T[]): T[] {
  return ids.filter((id) => modelAvailableInRegion(id, activeRegion));
}
