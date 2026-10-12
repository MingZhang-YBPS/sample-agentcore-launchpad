/**
 * Files allowed to spell out Bedrock model ids (checked by `model-ids.test.ts`).
 *
 * Every model the console PRE-SELECTS at runtime comes from the server
 * (`GET /api/models/defaults`, `lib/model-defaults.ts`) and every picker merges the
 * account's catalog (`GET /api/model-catalog`). What remains hard-coded is listed
 * here with the reason: static fallbacks used only until (or if) the API answers,
 * and curated label tables. A new hit anywhere else fails the test — make it a
 * server default or a catalog entry instead, or add it here with a reason.
 */
export const MODEL_ID_ALLOWLIST: Record<string, string> = {
  "lib/model-defaults.ts":
    "STATIC_MODEL_DEFAULTS: the backend's build defaults, the initial value until /api/models/defaults answers and the fallback when it fails",
  "lib/models.ts":
    "MODEL_CATALOG: curated Bedrock/Mantle label table merged under the configured default and the account catalog",
  "lib/evaluators.ts":
    "JUDGE_MODEL_OPTIONS: static judge list appended after the configured judge/fallback, GLM and the account catalog",
  "lib/evaluation.ts":
    "ACTOR_MODELS: static actor list (models verified for forced tool choice) appended after the configured actor",
  "lib/skillLab.ts":
    "JUDGE_MODEL_SUGGESTIONS: static Skill Lab judge datalist entries after the server default and the catalog",
  "lib/model-catalog.ts":
    "NO_TOOL_USE_MODEL_IDS: models live-probed to refuse toolConfig (kept out of tool pickers even before the catalog flags them)",
  "studio/lib/models.ts":
    "BEDROCK_MODELS / DEFAULT_MODEL_ID: Strands Studio's curated label table and the static canvas fallback, replaced by the configured defaults once loaded",
};
