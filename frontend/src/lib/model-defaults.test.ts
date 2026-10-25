import { afterEach, describe, expect, it, vi } from "vitest";

import { api, type ModelDefaults, type ModelSettingRole } from "./api";
import { actorModelOptions, defaultActorModel } from "./evaluation";
import { defaultJudgeModel, judgeModelOptions } from "./evaluators";
import {
  activeCatalogIds,
  inFamilies,
  loadModelCatalog,
  modelFamily,
  resetModelCatalogForTests,
  setCatalogWorkspace,
  setModelCatalogForTests,
  withCatalogModels,
} from "./model-catalog";
import {
  adoptModelDefault,
  currentModelDefaults,
  GLM_MODEL_ID,
  loadModelDefaults,
  mergeModelOptions,
  normalizeModelDefaults,
  peekModelDefaults,
  reloadModelDefaults,
  resetModelDefaultsForTests,
  STATIC_MODEL_DEFAULTS,
} from "./model-defaults";
import { isSavable, roleModelOptions } from "./model-settings";
import { defaultModelFor, isCustomModelId, modelOptionsFor, specDefaultModelId } from "./models";
import { setActiveRegion } from "./regions";
import { proposalDefaultModel, proposalDraftFrom } from "./assistant";
import { defaultModelForMethod } from "./agent-spec";
import {
  newAgentModel,
  setStudioModelContext,
  studioBedrockModels,
  studioFallbackModelId,
} from "../studio/lib/models";

// An account with neither OpenAI nor Anthropic: the operator moved every role.
const CONFIGURED: ModelDefaults = {
  ...STATIC_MODEL_DEFAULTS,
  judge_model_id: "us.amazon.nova-premier-v1:0",
  judge_fallback_model_id: "global.zai.glm-5.3",
  actor_model_id: "us.meta.llama4-maverick-17b-instruct-v1:0",
  assistant_model_id: "global.zai.glm-5.3",
  agent_default_model_id: "us.amazon.nova-pro-v1:0",
  claude_sdk_model_id: "global.anthropic.claude-sonnet-4-6",
  studio_fallback_model_id: "us.amazon.nova-lite-v1:0",
};

afterEach(() => {
  vi.restoreAllMocks();
  resetModelDefaultsForTests();
  resetModelCatalogForTests();
  setActiveRegion(null);
  setStudioModelContext({});
});

describe("model defaults loader", () => {
  it("fetches the defaults once and serves them from the cache", async () => {
    const spy = vi.spyOn(api, "modelDefaults").mockResolvedValue(CONFIGURED);
    expect(peekModelDefaults()).toBeNull();
    const [a, b] = await Promise.all([loadModelDefaults(), loadModelDefaults()]);
    await loadModelDefaults();
    expect(spy).toHaveBeenCalledTimes(1);
    expect(a).toEqual(CONFIGURED);
    expect(b).toBe(a);
    expect(currentModelDefaults().judge_model_id).toBe("us.amazon.nova-premier-v1:0");
  });

  it("falls back to the static defaults on error and retries on the next load", async () => {
    const spy = vi.spyOn(api, "modelDefaults").mockRejectedValueOnce(new Error("boom"));
    expect(await loadModelDefaults()).toEqual(STATIC_MODEL_DEFAULTS);
    expect(peekModelDefaults()).toBeNull();
    spy.mockResolvedValueOnce(CONFIGURED);
    expect((await loadModelDefaults()).actor_model_id).toBe(CONFIGURED.actor_model_id);
    expect(spy).toHaveBeenCalledTimes(2);
  });

  it("refetches after an admin change", async () => {
    vi.spyOn(api, "modelDefaults")
      .mockResolvedValueOnce(STATIC_MODEL_DEFAULTS)
      .mockResolvedValueOnce(CONFIGURED);
    await loadModelDefaults();
    expect(defaultJudgeModel()).toBe(STATIC_MODEL_DEFAULTS.judge_model_id);
    await reloadModelDefaults();
    expect(defaultJudgeModel()).toBe(CONFIGURED.judge_model_id);
  });

  it("keeps static values for missing keys but honours an empty judge fallback", () => {
    const out = normalizeModelDefaults({ judge_model_id: "  us.amazon.nova-pro-v1:0 ", judge_fallback_model_id: "", actor_model_id: "" });
    expect(out.judge_model_id).toBe("us.amazon.nova-pro-v1:0");
    expect(out.judge_fallback_model_id).toBe("");
    expect(out.actor_model_id).toBe(STATIC_MODEL_DEFAULTS.actor_model_id);
    expect(normalizeModelDefaults(null)).toEqual(STATIC_MODEL_DEFAULTS);
  });
});

describe("pre-selected defaults come from the API", () => {
  it("a new evaluator's judge is the configured judge once loaded (static before)", async () => {
    expect(defaultJudgeModel()).toBe(STATIC_MODEL_DEFAULTS.judge_model_id);
    vi.spyOn(api, "modelDefaults").mockResolvedValue(CONFIGURED);
    await loadModelDefaults();
    expect(defaultJudgeModel()).toBe("us.amazon.nova-premier-v1:0");
    expect(defaultJudgeModel(CONFIGURED)).toBe("us.amazon.nova-premier-v1:0");
  });

  it("a new run's actor is the configured actor once loaded (static before)", async () => {
    expect(defaultActorModel()).toBe(STATIC_MODEL_DEFAULTS.actor_model_id);
    vi.spyOn(api, "modelDefaults").mockResolvedValue(CONFIGURED);
    await loadModelDefaults();
    expect(defaultActorModel()).toBe("us.meta.llama4-maverick-17b-instruct-v1:0");
  });

  it("agent, Claude SDK, proposal and stored-spec defaults follow the configured roles", async () => {
    vi.spyOn(api, "modelDefaults").mockResolvedValue(CONFIGURED);
    await loadModelDefaults();
    expect(defaultModelFor("bedrock")).toBe("us.amazon.nova-pro-v1:0");
    expect(defaultModelForMethod("harness")).toBe("us.amazon.nova-pro-v1:0");
    expect(defaultModelForMethod("container")).toBe("global.anthropic.claude-sonnet-4-6");
    expect(proposalDraftFrom({}).model_id).toBe("global.zai.glm-5.3");
    expect(proposalDefaultModel("bedrock")).toBe("global.zai.glm-5.3");
    expect(specDefaultModelId()).toBe("us.amazon.nova-pro-v1:0");
    // the configured id is an offered option, not a custom one
    expect(isCustomModelId("us.amazon.nova-pro-v1:0", "bedrock")).toBe(false);
  });

  it("adopts the arriving default only into an untouched, still-seeded field", () => {
    const seeded = STATIC_MODEL_DEFAULTS.judge_model_id;
    expect(adoptModelDefault(seeded, seeded, "us.amazon.nova-pro-v1:0", false)).toBe("us.amazon.nova-pro-v1:0");
    // the user picked something else, or picked the seeded value on purpose
    expect(adoptModelDefault("global.zai.glm-5.3", seeded, "us.amazon.nova-pro-v1:0", false)).toBe("global.zai.glm-5.3");
    expect(adoptModelDefault(seeded, seeded, "us.amazon.nova-pro-v1:0", true)).toBe(seeded);
    // a stored evaluator's model is never the seed, so it is kept
    expect(adoptModelDefault("global.anthropic.claude-opus-4-8", seeded, "x.y", false)).toBe("global.anthropic.claude-opus-4-8");
    expect(adoptModelDefault(seeded, seeded, "", false)).toBe(seeded);
  });
});

describe("option lists", () => {
  it("merges configured ids first, deduped, blanks dropped", () => {
    expect(mergeModelOptions("a.b", ["", "c.d", "a.b", null], ["e.f", "c.d", "a.b"])).toEqual(["a.b", "c.d", "e.f"]);
  });

  it("judge options: configured judge first, then fallback and GLM, even outside the static list", () => {
    const options = judgeModelOptions(CONFIGURED, null, []);
    expect(options[0]).toBe("us.amazon.nova-premier-v1:0");
    expect(options.slice(1, 2)).toEqual([GLM_MODEL_ID]); // the fallback IS GLM here: one entry
    expect(new Set(options).size).toBe(options.length);
    expect(options).toContain("global.anthropic.claude-sonnet-5-5");
  });

  it("judge options: a disabled fallback adds nothing, a stored id is kept", () => {
    const options = judgeModelOptions({ ...CONFIGURED, judge_fallback_model_id: "" }, "custom.judge-v1", []);
    expect(options[0]).toBe("custom.judge-v1");
    expect(options[1]).toBe("us.amazon.nova-premier-v1:0");
    expect(options).toContain(GLM_MODEL_ID);
    expect(options).not.toContain("");
  });

  it("judge options: configured geo ids and GLM survive the region filter", () => {
    setActiveRegion("eu-west-1");
    const options = judgeModelOptions(CONFIGURED, null, []);
    expect(options).toContain("us.amazon.nova-premier-v1:0"); // configured: kept although `us.` in the EU
    expect(options).toContain(GLM_MODEL_ID);
  });

  it("judge and actor options append the account catalog", () => {
    const options = judgeModelOptions(CONFIGURED, null, ["us.amazon.nova-premier-v1:0", "us.deepseek.r1-v1:0"]);
    expect(options[0]).toBe("us.amazon.nova-premier-v1:0");
    expect(options.at(-1)).toBe("us.deepseek.r1-v1:0");
    expect(options.filter((id) => id === "us.amazon.nova-premier-v1:0")).toHaveLength(1);
    const actors = actorModelOptions(CONFIGURED, null, ["us.mistral.large-3"]);
    expect(actors.slice(0, 2)).toEqual(["us.meta.llama4-maverick-17b-instruct-v1:0", GLM_MODEL_ID]);
    expect(actors.at(-1)).toBe("us.mistral.large-3");
  });

  it("actor options include GLM-5.3 with the static defaults too", () => {
    const actors = actorModelOptions(null, null, []);
    expect(actors[0]).toBe(STATIC_MODEL_DEFAULTS.actor_model_id);
    expect(actors).toContain(GLM_MODEL_ID);
    expect(new Set(actors).size).toBe(actors.length);
  });
});

describe("account model catalog", () => {
  it("loads once per workspace and joins the agent dropdown after the static catalog", async () => {
    const spy = vi.spyOn(api, "modelCatalog").mockResolvedValue({
      region: "us-east-1",
      models: [
        { model_id: "us.mistral.large-3", provider: "mistral", name: "Mistral Large 3", kind: "inference_profile" },
        { model_id: "global.zai.glm-5.3", provider: "zai", name: "GLM 5.3", kind: "inference_profile" },
        { model_id: "us.mistral.large-3", provider: "mistral", name: "dup", kind: "inference_profile" },
      ],
    });
    setCatalogWorkspace("ws-1");
    await Promise.all([loadModelCatalog("ws-1"), loadModelCatalog("ws-1")]);
    expect(spy).toHaveBeenCalledTimes(1);
    expect(spy).toHaveBeenCalledWith("ws-1");
    expect(activeCatalogIds()).toEqual(["us.mistral.large-3", "global.zai.glm-5.3"]);
    const ids = modelOptionsFor("bedrock").map((o) => o.model_id);
    expect(ids.at(-1)).toBe("us.mistral.large-3");
    expect(ids.filter((id) => id === "global.zai.glm-5.3")).toHaveLength(1);
    expect(modelOptionsFor("bedrock").find((o) => o.model_id === "us.mistral.large-3")?.label).toContain("Mistral Large 3");
    // another workspace has its own (not yet loaded) catalog
    setCatalogWorkspace("ws-2");
    expect(activeCatalogIds()).toEqual([]);
  });

  it("a failed or empty listing leaves the static lists alone", async () => {
    vi.spyOn(api, "modelCatalog").mockResolvedValue({ region: "us-east-1", models: [], error: "AccessDenied" });
    setCatalogWorkspace("ws-1");
    await loadModelCatalog("ws-1");
    expect(activeCatalogIds()).toEqual([]);
    expect(modelOptionsFor("bedrock")[0].model_id).toBe(STATIC_MODEL_DEFAULTS.agent_default_model_id);
  });

  it("Claude-only pickers keep only Anthropic models from the catalog", () => {
    setModelCatalogForTests([
      { model_id: "us.anthropic.claude-haiku-9", provider: "anthropic", name: "Haiku 9", kind: "inference_profile" },
      { model_id: "us.amazon.nova-pro-v1:0", provider: "amazon", name: "Nova Pro", kind: "inference_profile" },
    ]);
    const ids = modelOptionsFor("bedrock", true).map((o) => o.model_id);
    expect(ids).toContain("us.anthropic.claude-haiku-9");
    expect(ids).not.toContain("us.amazon.nova-pro-v1:0");
    expect(ids.every((id) => modelFamily(id) === "anthropic")).toBe(true);
  });

  it("server model lists gain the catalog models they lack", () => {
    const out = withCatalogModels([{ model_id: "a.b", label: "A" }], [
      { model_id: "a.b", provider: "a", name: "dup", kind: "foundation_model" },
      { model_id: "c.d", provider: "c", name: "C", kind: "foundation_model" },
    ]);
    expect(out).toEqual([{ model_id: "a.b", label: "A" }, { model_id: "c.d", label: "C · c.d" }]);
  });

  it("model families mirror the backend", () => {
    expect(modelFamily("global.anthropic.claude-sonnet-5")).toBe("anthropic");
    expect(modelFamily("anthropic.claude-3")).toBe("anthropic");
    expect(modelFamily("us.amazon.nova-pro-v1:0")).toBe("amazon");
    expect(modelFamily("zai.glm-5")).toBe("zai");
    expect(inFamilies(["us.openai.gpt-6-sol", "global.zai.glm-5.3"], ["openai"])).toEqual(["us.openai.gpt-6-sol"]);
    expect(inFamilies(["a.b"], null)).toEqual(["a.b"]);
  });
});

const R1 = "us.deepseek.r1-v1:0";
const KIMI_DEEPSEEK_TOOLS = [
  "deepseek.v3.2",
  "global.moonshotai.kimi-k3",
  "us.moonshotai.kimi-k3",
  "moonshotai.kimi-k2.5",
  "moonshot.kimi-k2-thinking",
  "deepseek.v3-v1:0",
];

describe("DeepSeek / Kimi", () => {
  it("are offered to judges, R1 included", () => {
    setActiveRegion("us-west-2");
    expect(judgeModelOptions(null, null, [])).toEqual(expect.arrayContaining([R1, ...KIMI_DEEPSEEK_TOOLS]));
  });

  it("tool pickers (agents, actor, Studio) offer them without R1", () => {
    setActiveRegion("us-west-2");
    const agents = modelOptionsFor("bedrock").map((o) => o.model_id);
    expect(agents).toEqual(expect.arrayContaining(KIMI_DEEPSEEK_TOOLS));
    expect(agents).not.toContain(R1);
    expect(modelOptionsFor("bedrock").find((o) => o.model_id === "deepseek.v3.2")?.label).toBe("DeepSeek V3.2");
    const actors = actorModelOptions(null, null, [R1]);
    expect(actors).toEqual(expect.arrayContaining(KIMI_DEEPSEEK_TOOLS));
    expect(actors).not.toContain(R1);
    const studio = studioBedrockModels().map((m) => m.model_id);
    expect(studio).toEqual(expect.arrayContaining(KIMI_DEEPSEEK_TOOLS));
    expect(studio).not.toContain(R1);
  });

  it("DeepSeek V3.1 is offered in us-west-2 only", () => {
    setActiveRegion("us-east-1");
    expect(judgeModelOptions(null, null, [])).not.toContain("deepseek.v3-v1:0");
    expect(actorModelOptions(null, null, [])).not.toContain("deepseek.v3-v1:0");
    expect(modelOptionsFor("bedrock").map((o) => o.model_id)).not.toContain("deepseek.v3-v1:0");
    expect(modelOptionsFor("bedrock").map((o) => o.model_id)).toContain("deepseek.v3.2");
    setActiveRegion("us-west-2");
    expect(judgeModelOptions(null, null, [])).toContain("deepseek.v3-v1:0");
  });

  it("a catalog model flagged tool_use: false stays out of tool pickers", () => {
    setModelCatalogForTests([
      { model_id: "us.acme.no-tools-v1", provider: "acme", name: "No tools", kind: "inference_profile", tool_use: false },
      { model_id: "us.acme.tools-v1", provider: "acme", name: "Tools", kind: "inference_profile", tool_use: true },
    ]);
    const agents = modelOptionsFor("bedrock").map((o) => o.model_id);
    expect(agents).toContain("us.acme.tools-v1");
    expect(agents).not.toContain("us.acme.no-tools-v1");
    expect(actorModelOptions(null, null)).not.toContain("us.acme.no-tools-v1");
    expect(judgeModelOptions(null, null)).toContain("us.acme.no-tools-v1");
  });
});

describe("Studio model context", () => {
  it("uses the configured fallback and new-agent model, and offers the catalog", () => {
    setStudioModelContext({
      fallbackModelId: "us.amazon.nova-lite-v1:0",
      newAgentModelId: "us.amazon.nova-pro-v1:0",
      catalog: [{ model_id: "us.deepseek.r1-v1:0", model_name: "DeepSeek R1" }],
    });
    expect(studioFallbackModelId()).toBe("us.amazon.nova-lite-v1:0");
    expect(newAgentModel().model_id).toBe("us.amazon.nova-pro-v1:0");
    const ids = studioBedrockModels().map((m) => m.model_id);
    expect(ids.slice(0, 2)).toEqual(["us.amazon.nova-pro-v1:0", "us.amazon.nova-lite-v1:0"]);
    expect(ids).toContain("us.deepseek.r1-v1:0");
    expect(new Set(ids).size).toBe(ids.length);
    setStudioModelContext({});
    expect(studioFallbackModelId()).toBe(STATIC_MODEL_DEFAULTS.studio_fallback_model_id);
  });
});

describe("admin model roles", () => {
  const role = (patch: Partial<ModelSettingRole>): ModelSettingRole => ({
    field: "claude_sdk_model_id", env: "LAUNCHPAD_CLAUDE_SDK_MODEL_ID", value: "global.anthropic.claude-sonnet-5",
    default: "global.anthropic.claude-sonnet-5", env_value: null, console_value: null, source: "default",
    probe: "converse", families: ["anthropic"], allow_empty: false, updated_by: null, updated_at: null, ...patch,
  });

  it("a family-restricted role only offers that family", () => {
    const options = roleModelOptions(role({}), ["us.anthropic.claude-haiku-9", "global.zai.glm-5.3", "us.amazon.nova-pro-v1:0"], []);
    expect(options).toEqual(["global.anthropic.claude-sonnet-5", "us.anthropic.claude-haiku-9"]);
  });

  it("an unrestricted role offers current, default, GLM and the catalog", () => {
    const options = roleModelOptions(
      role({ field: "judge_model_id", value: "us.amazon.nova-pro-v1:0", default: "global.openai.gpt-6-sol", families: null }),
      ["us.deepseek.r1-v1:0"],
      [],
    );
    expect(options).toEqual(["us.amazon.nova-pro-v1:0", "global.openai.gpt-6-sol", GLM_MODEL_ID, "us.deepseek.r1-v1:0"]);
  });

  it("a role that runs tools never offers a model without tool use; static Kimi/DeepSeek join", () => {
    const tools = roleModelOptions(role({ field: "agent_default_model_id", families: null, needs_tools: true }), [
      "us.mistral.large-3",
    ]);
    expect(tools).not.toContain(R1);
    expect(tools).toEqual(expect.arrayContaining(KIMI_DEEPSEEK_TOOLS));
    expect(tools).toContain("us.mistral.large-3");
    const judge = roleModelOptions(role({ field: "judge_model_id", families: null, probe: "judge" }), []);
    expect(judge).toEqual(expect.arrayContaining([R1, ...KIMI_DEEPSEEK_TOOLS]));
  });

  it("saving needs a change, and blank only where allowed", () => {
    expect(isSavable(role({}), "global.anthropic.claude-sonnet-5")).toBe(false);
    expect(isSavable(role({}), "")).toBe(false);
    expect(isSavable(role({ allow_empty: true, value: "x.y" }), "")).toBe(true);
    expect(isSavable(role({}), "us.anthropic.claude-haiku-9")).toBe(true);
  });
});
