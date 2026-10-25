/**
 * Pure rules of the admin Models page (`/v3/models`, `GET /api/models/settings`):
 * which models a role's picker offers and how a role's source reads.
 */
import type { ModelSettingRole, ModelSettingSource } from "./api";
import { ACTOR_MODELS } from "./evaluation";
import { JUDGE_MODEL_OPTIONS } from "./evaluators";
import { inFamilies, toolCapable } from "./model-catalog";
import { GLM_MODEL_ID, mergeModelOptions } from "./model-defaults";
import { MODEL_CATALOG } from "./models";
import { modelsForActiveRegion } from "./regions";

/** Every Bedrock id the console knows statically (agent catalog, judges, actors). */
export function knownBedrockModelIds(): string[] {
  return mergeModelOptions("", [], [
    ...MODEL_CATALOG.bedrock.map((option) => option.model_id),
    ...JUDGE_MODEL_OPTIONS,
    ...ACTOR_MODELS,
  ]);
}

/**
 * A role's picker: its current value first, then its build default, GLM-5.3, the
 * static Bedrock models of this workspace's geography and the account's catalog —
 * narrowed to the families the role's feature can drive (the Claude Agent SDK takes
 * Claude only, codex OpenAI only) and, for a role that runs tools (`needs_tools`),
 * to models that accept tool use. The current value is always kept: the server
 * accepted it.
 */
export function roleModelOptions(
  role: ModelSettingRole,
  catalog: readonly string[],
  known: readonly string[] = modelsForActiveRegion(knownBedrockModelIds()),
): string[] {
  const merged = mergeModelOptions(role.value, [role.default, GLM_MODEL_ID], [...known, ...catalog]);
  const families = inFamilies(merged, role.families);
  const allowed = role.needs_tools ? toolCapable(families) : families;
  return role.value && !allowed.includes(role.value) ? [role.value, ...allowed] : allowed;
}

/** The chip colour of a role's source: a console override stands out, a default is quiet. */
export function sourceSignal(source: ModelSettingSource): "info" | "wait" | "ok" | undefined {
  if (source === "console") return "info";
  if (source === "env") return "wait";
  if (source === "config") return "ok";
  return undefined;
}

/** Whether `draft` would change the role (blank only counts where blank is allowed). */
export function isSavable(role: ModelSettingRole, draft: string): boolean {
  const value = draft.trim();
  if (!value && !role.allow_empty) return false;
  return value !== role.value;
}
