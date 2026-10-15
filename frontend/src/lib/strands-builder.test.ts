import { describe, expect, it } from "vitest";

import en from "../locales/en/common.json";
import zh from "../locales/zh-CN/common.json";
import {
  type AgentFormCatalogs,
  agentFormValid,
  buildAgentSpec,
  emptyAgentForm,
  formFromStoredSpec,
  prefillKbPick,
  republishSpec,
} from "./agent-spec";
import {
  emptyStrandsForm,
  EXAMPLE_OUTPUT_SCHEMA,
  outputSchemaProblem,
  promptPlaceholders,
  type StrandsForm,
  strandsIssues,
  strandsSpec,
} from "./strands-builder";

const CATALOGS: AgentFormCatalogs = {
  gatewayTargets: [],
  remoteMcp: [],
  storedGatewayConfig: {},
  kbInfo: (id) => ({ kb_id: id, name: id, description: "" }),
};
const CLAUDE = "global.anthropic.claude-sonnet-4-5-v1:0";
const HTTP = { protocol: "http" as "http" | "a2a" | "mcp", modelId: CLAUDE, modelSource: "bedrock" as const };
const CTX = { ...HTTP, hasKbs: false, guardrail: false };
const SB_FIELDS = [
  "strands_tools",
  "strands_tool_config",
  "model_params",
  "conversation",
  "output_schema",
  "few_shot_examples",
  "prompt_variables",
];

const strands = (over: Partial<StrandsForm>): StrandsForm => ({ ...emptyStrandsForm(), ...over });
const zipForm = (over: Partial<StrandsForm> = {}) => ({
  ...emptyAgentForm("zip_runtime"),
  name: "sb-agent",
  systemPrompt: "Be brief.",
  modelId: CLAUDE,
  modelSource: "bedrock" as const,
  strands: strands(over),
});

describe("an agent using none of the options posts the spec it always did", () => {
  it("adds no builder field for a fresh Strands form", () => {
    const spec = buildAgentSpec(zipForm(), CATALOGS) as unknown as Record<string, unknown>;
    for (const key of SB_FIELDS) expect(spec).not.toHaveProperty(key);
  });

  it("never sends builder fields for another method, even with a carried slice", () => {
    const form = { ...zipForm({ tools: ["calculator"], temperature: "0.2" }), method: "harness" as const };
    const spec = buildAgentSpec(form, CATALOGS) as unknown as Record<string, unknown>;
    for (const key of SB_FIELDS) expect(spec).not.toHaveProperty(key);
  });

  it("a stored spec without the fields loads to the empty slice", () => {
    const { form } = formFromStoredSpec("zip_runtime", "old", { system_prompt: "x", model_id: CLAUDE });
    expect(form.strands).toEqual(emptyStrandsForm());
  });
});

describe("spec mapping", () => {
  const FULL = {
    name: "sb-agent",
    method: "zip_runtime",
    system_prompt: "Weather for {{city}}",
    model_id: CLAUDE,
    strands_tools: ["current_time", "http_request"],
    strands_tool_config: { http_request: { allowed_hosts: ["api.example.com", "*.svc.example.org"] } },
    model_params: { temperature: 0, max_tokens: 9000, stop_sequences: ["END"], prompt_caching: true },
    conversation: { strategy: "summarizing", window_size: 12, summary_model: "us.amazon.nova-lite-v1:0" },
    output_schema: { type: "object", properties: { city: { type: "string" } }, required: ["city"] },
    few_shot_examples: [{ question: "q", answer: "a" }],
    prompt_variables: { city: "Rome" },
  };

  it("round-trips every field through the form", () => {
    const { form } = formFromStoredSpec("zip_runtime", "sb-agent", FULL);
    const spec = buildAgentSpec(form, CATALOGS) as unknown as Record<string, unknown>;
    for (const key of SB_FIELDS) expect(spec[key]).toEqual(FULL[key as keyof typeof FULL]);
    // and a re-publish keeps them
    const again = republishSpec(buildAgentSpec(form, CATALOGS), FULL) as unknown as Record<string, unknown>;
    expect(again.model_params).toEqual(FULL.model_params);
  });

  it("keeps a zero temperature and sends thinking only for Claude on Bedrock", () => {
    const form = strands({ temperature: "0", thinking: true, thinkingBudget: "2048" });
    expect(strandsSpec(form, { ...HTTP, modelId: "x" }).model_params).toEqual({ temperature: 0 });
    expect(strandsSpec(form, HTTP).model_params).toEqual({ temperature: 0, thinking_budget_tokens: 2048 });
    const mantle = { ...HTTP, modelSource: "mantle" as const };
    expect(strandsSpec(strands({ stopSequences: "X", promptCaching: true, maxTokens: "10" }), mantle).model_params)
      .toEqual({ max_tokens: 10 });
  });

  it("drops the host config with the tool and the schema on A2A", () => {
    expect(strandsSpec(strands({ httpHosts: "a.example.com" }), HTTP)).toEqual({});
    const a2a = strandsSpec(strands({ outputSchema: EXAMPLE_OUTPUT_SCHEMA, tools: ["think"] }), { ...HTTP, protocol: "a2a" });
    expect(a2a).toEqual({ strands_tools: ["think"] });
  });

  it("ignores blank variable and example rows", () => {
    const spec = strandsSpec(
      strands({ variables: [{ name: " ", value: "x" }, { name: "team", value: "ops" }], fewShot: [{ question: "", answer: "" }] }),
      HTTP,
    );
    expect(spec).toEqual({ prompt_variables: { team: "ops" } });
  });
});

describe("validation mirrors the backend", () => {
  const issues = (over: Partial<StrandsForm>, ctx = CTX) => Object.keys(strandsIssues(strands(over), ctx).issues);

  it("refuses http_request without a well-formed allowed host", () => {
    expect(issues({ tools: ["http_request"] })).toEqual(["httpHosts"]);
    expect(issues({ tools: ["http_request"], httpHosts: "https://a.example.com" })).toEqual(["httpHosts"]);
    expect(issues({ tools: ["http_request"], httpHosts: "a.example.com, *.b.example.org" })).toEqual([]);
  });

  it("needs a knowledge base for retrieve", () => {
    expect(issues({ tools: ["retrieve"] })).toEqual(["retrieve"]);
    expect(issues({ tools: ["retrieve"] }, { ...CTX, hasKbs: true })).toEqual([]);
  });

  it("checks the thinking and sampling combinations", () => {
    expect(issues({ thinking: true, temperature: "0.5" })).toContain("thinkingTemperature");
    expect(issues({ thinking: true, topP: "0.5" })).toContain("thinkingTopP");
    expect(issues({ thinking: true, thinkingBudget: "4096", maxTokens: "4096" })).toContain("thinkingMaxTokens");
    expect(issues({ thinking: true, thinkingBudget: "100" })).toContain("thinkingBudget");
    expect(issues({ temperature: "0.3", topP: "0.9" })).toEqual(["claudeBoth"]);
    expect(issues({ temperature: "0.3", topP: "0.9" }, { ...CTX, modelId: "us.amazon.nova-pro-v1:0" })).toEqual([]);
    expect(issues({ temperature: "2" })).toEqual(["temperature"]);
    expect(issues({ maxTokens: "1.5" })).toEqual(["maxTokens"]);
  });

  it("validates the output schema subset and where it may sit", () => {
    expect(outputSchemaProblem(EXAMPLE_OUTPUT_SCHEMA)).toBeNull();
    expect(outputSchemaProblem("{")).not.toBeNull();
    expect(outputSchemaProblem('{"type":"string"}')).toMatch(/object/);
    expect(outputSchemaProblem('{"type":"object","properties":{"a-b":{"type":"string"}}}')).toMatch(/property/);
    expect(outputSchemaProblem('{"type":"object","properties":{"a":{"type":"date"}}}')).toMatch(/type/);
    expect(issues({ outputSchema: EXAMPLE_OUTPUT_SCHEMA }, { ...CTX, protocol: "a2a" })).toEqual(["outputSchemaA2a"]);
    expect(issues({ outputSchema: EXAMPLE_OUTPUT_SCHEMA }, { ...CTX, guardrail: true })).toEqual(["outputSchemaGuardrail"]);
  });

  it("checks variable names and half-filled examples", () => {
    expect(issues({ variables: [{ name: "not ok", value: "" }] })).toEqual(["variables"]);
    expect(issues({ variables: [{ name: "a", value: "" }, { name: "a", value: "" }] })).toEqual(["variables"]);
    expect(issues({ fewShot: [{ question: "q", answer: "" }] })).toEqual(["fewShot"]);
  });

  it("blocks the wizard's gate on a Strands issue", () => {
    const gate = (form: ReturnType<typeof zipForm>) =>
      agentFormValid(form, CATALOGS, { knobIssues: [], byocUploading: false, connections: [] });
    expect(gate(zipForm())).toBe(true);
    expect(gate(zipForm({ tools: ["http_request"] }))).toBe(false);
  });
});

describe("helpers", () => {
  it("finds {{placeholders}} once each", () => {
    expect(promptPlaceholders("Hi {{ name }}, {{city}} and {{name}} {{ 1x }}")).toEqual(["name", "city"]);
  });

  it("kb=1 picks the first ACTIVE managed KB only when none is selected", () => {
    const catalog = [
      { kb_id: "KB0", name: "creating", status: "CREATING" },
      { kb_id: "KB1", name: "one", status: "ACTIVE" },
      { kb_id: "KB2", name: "two", status: "ACTIVE" },
    ];
    expect(prefillKbPick([], catalog)).toEqual(["KB1"]);
    expect(prefillKbPick(["KB2"], catalog)).toEqual(["KB2"]);
    expect(prefillKbPick([], [{ kb_id: "KB0", name: "x", status: "CREATING" }])).toEqual([]);
  });
});

describe("locales", () => {
  const keys = (node: unknown, prefix = ""): string[] =>
    typeof node === "object" && node !== null
      ? Object.entries(node).flatMap(([k, v]) => keys(v, `${prefix}${k}.`))
      : [prefix.slice(0, -1)];
  const leaves = (node: unknown): string[] =>
    typeof node === "object" && node !== null ? Object.values(node).flatMap(leaves) : [String(node)];

  it("has the same strandsBuilder keys in both languages", () => {
    expect(keys((zh as Record<string, unknown>).strandsBuilder).sort()).toEqual(
      keys((en as Record<string, unknown>).strandsBuilder).sort(),
    );
  });

  it("uses full-width punctuation in zh-CN", () => {
    for (const text of leaves((zh as Record<string, unknown>).strandsBuilder)) {
      // ASCII commas / periods / colons next to CJK text are the tell-tale
      expect(text).not.toMatch(/[一-鿿][,.:;!?()]|[,.:;!?()][一-鿿]/);
    }
  });
});
