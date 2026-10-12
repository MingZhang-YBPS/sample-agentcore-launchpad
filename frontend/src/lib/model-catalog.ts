/**
 * The active workspace's account/Region model catalog (`GET /api/model-catalog`):
 * every text-output, on-demand-invocable model and inference profile Bedrock offers
 * there. Model pickers merge it with their static lists, so an account that lacks
 * OpenAI or Anthropic models still sees what it can call.
 *
 * Module state, like `regions.ts`'s active region: `WorkspaceProvider` names the
 * current workspace (`setCatalogWorkspace`) and prefetches; pure option builders
 * read `activeCatalog()` at render time and components subscribe through
 * `useModelCatalog()` so they re-render when it lands. One request per workspace
 * per page load; a failed or empty listing leaves the static lists alone (a
 * failure is not cached, so a later mount retries).
 */
import { useEffect, useSyncExternalStore } from "react";

import { api, type ModelCatalogEntry } from "./api";

const EMPTY: readonly ModelCatalogEntry[] = Object.freeze([]);

const byWorkspace = new Map<string, readonly ModelCatalogEntry[]>();
const inflight = new Map<string, Promise<readonly ModelCatalogEntry[]>>();
const listeners = new Set<() => void>();
let workspaceKey = "";

const keyOf = (workspaceId: string | null | undefined) => workspaceId ?? "";

function notify(): void {
  for (const listener of listeners) listener();
}

/** Called by `WorkspaceProvider` with the current workspace (render-time, like the region). */
export function setCatalogWorkspace(workspaceId: string | null | undefined): void {
  workspaceKey = keyOf(workspaceId);
}

/** Valid entries only, first occurrence of an id kept. */
export function normalizeCatalog(raw: unknown): ModelCatalogEntry[] {
  const models = (raw as { models?: unknown } | null)?.models;
  if (!Array.isArray(models)) return [];
  const seen = new Set<string>();
  const out: ModelCatalogEntry[] = [];
  for (const row of models) {
    const id = typeof row?.model_id === "string" ? row.model_id.trim() : "";
    if (!id || seen.has(id)) continue;
    seen.add(id);
    out.push({
      model_id: id,
      provider: typeof row.provider === "string" ? row.provider : "",
      name: typeof row.name === "string" ? row.name : "",
      kind: row.kind === "foundation_model" ? "foundation_model" : "inference_profile",
      tool_use: typeof row.tool_use === "boolean" ? row.tool_use : null,
    });
  }
  return out;
}

/** The catalog of `workspaceId` (default: the current one); `[]` when listing failed. */
export function loadModelCatalog(workspaceId: string | null = workspaceKey || null): Promise<readonly ModelCatalogEntry[]> {
  const key = keyOf(workspaceId);
  const cached = byWorkspace.get(key);
  if (cached) return Promise.resolve(cached);
  let pending = inflight.get(key);
  if (!pending) {
    pending = api.modelCatalog(workspaceId).then(
      (raw) => {
        const models = normalizeCatalog(raw);
        byWorkspace.set(key, models.length ? models : EMPTY);
        inflight.delete(key);
        notify();
        return byWorkspace.get(key) ?? EMPTY;
      },
      () => {
        inflight.delete(key); // not cached: the next mount retries
        return EMPTY;
      },
    );
    inflight.set(key, pending);
  }
  return pending;
}

/** The current workspace's catalog, `[]` until it lands (or when listing failed). */
export function activeCatalog(): readonly ModelCatalogEntry[] {
  return byWorkspace.get(workspaceKey) ?? EMPTY;
}

/** The current workspace's catalog ids, in catalog order. */
export function activeCatalogIds(): string[] {
  return activeCatalog().map((entry) => entry.model_id);
}

/** Models known to refuse `toolConfig` (live-probed 2026-10-12): fine as a judge or
 *  for prompt optimisation, never offered where the model runs tools (agents, Studio
 *  nodes, proposals, the simulated user). */
export const NO_TOOL_USE_MODEL_IDS: ReadonlySet<string> = new Set(["us.deepseek.r1-v1:0"]);

/** False when the model is known not to accept tool use (static list or catalog flag). */
export function supportsToolUse(modelId: string, catalog: readonly ModelCatalogEntry[] = activeCatalog()): boolean {
  if (NO_TOOL_USE_MODEL_IDS.has(modelId)) return false;
  return catalog.find((entry) => entry.model_id === modelId)?.tool_use !== false;
}

/** `ids` without the models known not to accept tool use. */
export function toolCapable(ids: readonly string[], catalog: readonly ModelCatalogEntry[] = activeCatalog()): string[] {
  return ids.filter((id) => supportsToolUse(id, catalog));
}

/** The catalog entries usable where the model runs tools. */
export function toolCapableCatalog(catalog: readonly ModelCatalogEntry[] = activeCatalog()): ModelCatalogEntry[] {
  return catalog.filter((entry) => supportsToolUse(entry.model_id, catalog));
}

const GEO_PREFIXES = new Set(["global", "us", "eu", "apac", "jp", "au", "ca", "us-gov"]);

/** Provider family of a model id — the segment after a geographic prefix
 *  (`<geo>.<family>.<model>`), else the first one (`<family>.<model>`); mirrors
 *  backend `model_settings.model_family`. */
export function modelFamily(modelId: string): string {
  const parts = modelId.split(".");
  if (parts.length >= 3 && GEO_PREFIXES.has(parts[0])) return parts[1];
  return parts.length >= 2 ? parts[0] : "";
}

/** `ids` narrowed to the given provider families (`null` = any). */
export function inFamilies(ids: readonly string[], families: readonly string[] | null | undefined): string[] {
  return families?.length ? ids.filter((id) => families.includes(modelFamily(id))) : [...ids];
}

/** Display label of a catalog entry: its name and id (ids alone are ambiguous across kinds). */
export function catalogLabel(entry: ModelCatalogEntry): string {
  return entry.name && entry.name !== entry.model_id ? `${entry.name} · ${entry.model_id}` : entry.model_id;
}

/** A server-supplied model list followed by the catalog models it lacks
 *  (labelled with `catalogLabel`), for pickers whose provider runs Bedrock models. */
export function withCatalogModels(
  models: readonly { model_id: string; label: string }[],
  catalog: readonly ModelCatalogEntry[] = activeCatalog(),
): { model_id: string; label: string }[] {
  const have = new Set(models.map((m) => m.model_id));
  return [
    ...models,
    ...catalog.filter((e) => !have.has(e.model_id)).map((e) => ({ model_id: e.model_id, label: catalogLabel(e) })),
  ];
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/** The current workspace's catalog; re-renders the caller when it lands. */
export function useModelCatalog(): readonly ModelCatalogEntry[] {
  const catalog = useSyncExternalStore(subscribe, activeCatalog, activeCatalog);
  const key = workspaceKey;
  useEffect(() => {
    void loadModelCatalog(key || null);
  }, [key]);
  return catalog;
}

/** Test hook: forget every cached catalog. */
export function resetModelCatalogForTests(): void {
  byWorkspace.clear();
  inflight.clear();
  workspaceKey = "";
}

/** Test hook: install a catalog for the current workspace. */
export function setModelCatalogForTests(models: ModelCatalogEntry[]): void {
  byWorkspace.set(workspaceKey, models);
  notify();
}
