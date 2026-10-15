import { describe, expect, it } from "vitest";

import en from "../locales/en/common.json";
import zh from "../locales/zh-CN/common.json";
import type { AgentInfo } from "./api";
import {
  agentToolBlock,
  agentToolName,
  agentToolRow,
  backoffDelays,
  emptyStrandsForm,
  type Sb2IssueKey,
  sb2Issues,
  type StrandsForm,
  strandsFormFromSpec,
  strandsSpec,
  strandsValid,
} from "./strands-builder";

const CLAUDE = "global.anthropic.claude-sonnet-4-5-v1:0";
const HTTP = { protocol: "http" as "http" | "a2a" | "mcp", modelId: CLAUDE, modelSource: "bedrock" as const };
const CTX = { ...HTTP, hasKbs: false, guardrail: false };
const SB2_FIELDS = ["agent_tools", "model_provider", "tool_hooks", "retry"];
const strands = (over: Partial<StrandsForm>): StrandsForm => ({ ...emptyStrandsForm(), ...over });
const keys = (over: Partial<StrandsForm>, ctx = CTX) => Object.keys(sb2Issues(strands(over), ctx)) as Sb2IssueKey[];

const agent = (over: Partial<AgentInfo>): AgentInfo =>
  ({ id: "a1", name: "billing-desk", display_name: "Billing desk", method: "zip_runtime", status: "active",
     arn: "arn:aws:bedrock-agentcore:us-east-1:111122223333:runtime/billing", spec: { protocol: "http" },
     inbound_auth_mode: "iam", ...over }) as AgentInfo;

describe("SB-2 fields stay out of the spec until set", () => {
  it("posts none for a fresh form, and none from a pre-SB-2 draft slice", () => {
    const spec = strandsSpec(emptyStrandsForm(), HTTP) as Record<string, unknown>;
    for (const k of SB2_FIELDS) expect(spec).not.toHaveProperty(k);
    const old = { ...emptyStrandsForm() } as Partial<StrandsForm>;
    delete old.agentTools;
    delete old.provider;
    const legacy = strandsSpec(old as StrandsForm, HTTP) as Record<string, unknown>;
    for (const k of SB2_FIELDS) expect(legacy).not.toHaveProperty(k);
    expect(strandsValid(old as StrandsForm, CTX)).toBe(true);
  });
});

describe("agents as tools", () => {
  it("posts only id, tool name and description — the ARN is server-owned", () => {
    const row = agentToolRow(agent({}));
    expect(row.toolName).toBe("ask_billing_desk");
    const spec = strandsSpec(strands({ agentTools: [{ ...row, toolName: " ask_billing " }] }), HTTP);
    expect(spec.agent_tools).toEqual([{ agent_id: "a1", tool_name: "ask_billing", description: row.description }]);
  });

  it("reads stored refs back", () => {
    const form = strandsFormFromSpec({
      agent_tools: [{ agent_id: "a9", tool_name: "ask_peer", description: "Peer.", protocol: "a2a",
                      runtime_arn: "arn:…", shared_from: "alpha" }],
    });
    expect(form.agentTools).toEqual([{ agentId: "a9", label: "a9", toolName: "ask_peer", description: "Peer.",
                                       protocol: "a2a", sharedFrom: "alpha" }]);
  });

  it("names the agents that cannot be called, as the backend does", () => {
    expect(agentToolBlock(agent({}), "caller")).toBeNull();
    expect(agentToolBlock(agent({ spec: { protocol: "a2a" } }), "caller")).toBeNull();
    expect(agentToolBlock(agent({}), "billing-desk")).toBe("self");
    expect(agentToolBlock(agent({ method: "harness" }), "x")).toBe("not_runtime");
    expect(agentToolBlock(agent({ status: "draft" }), "x")).toBe("not_deployed");
    expect(agentToolBlock(agent({ arn: null }), "x")).toBe("not_deployed");
    expect(agentToolBlock(agent({ spec: { protocol: "mcp" } }), "x")).toBe("protocol");
    expect(agentToolBlock(agent({ inbound_auth_mode: "jwt" }), "x")).toBe("jwt_inbound");
  });

  it("checks names and descriptions", () => {
    const row = agentToolRow(agent({}));
    expect(keys({ agentTools: [{ ...row, toolName: "ask-billing" }] })).toEqual(["agentToolName"]);
    expect(keys({ agentTools: [row, { ...row, agentId: "a2" }] })).toEqual(["agentToolDuplicate"]);
    expect(keys({ agentTools: [{ ...row, toolName: "calculator" }] })).toEqual(["agentToolDuplicate"]);
    expect(keys({ tools: ["think"], agentTools: [{ ...row, toolName: "think" }] })).toEqual(["agentToolDuplicate"]);
    expect(keys({ agentTools: [{ ...row, description: " " }] })).toEqual(["agentToolDescription"]);
    expect(agentToolName("Weird  Name!")).toBe("ask_weird_name");
  });
});

describe("model provider", () => {
  it("sends the Connection name, never a key, and only where it applies", () => {
    const form = strands({ provider: "litellm", providerConnection: "llm-gw", providerBaseUrl: "https://gw.example.com" });
    expect(strandsSpec(form, HTTP).model_provider).toEqual({ kind: "litellm", connection: "llm-gw", base_url: "https://gw.example.com" });
    expect(strandsSpec(form, { ...HTTP, protocol: "a2a" })).not.toHaveProperty("model_provider");
    expect(strandsSpec(form, { ...HTTP, modelSource: "mantle" })).not.toHaveProperty("model_provider");
    expect(strandsSpec(strands({ provider: "anthropic", providerConnection: "k" }), HTTP).model_provider).toEqual({
      kind: "anthropic", connection: "k" });
    expect(strandsFormFromSpec({ model_provider: { kind: "anthropic", connection: "k" } }).provider).toBe("anthropic");
  });

  it("checks the Connection, the URL and the Converse-only options", () => {
    expect(keys({ provider: "litellm" })).toEqual(["providerConnection", "providerBaseUrl"]);
    expect(keys({ provider: "litellm", providerConnection: "k", providerBaseUrl: "https://u:p@gw.example.com" }))
      .toEqual(["providerBaseUrl"]);
    expect(keys({ provider: "anthropic", providerConnection: "k" })).toEqual([]);
    expect(keys({ provider: "anthropic", providerConnection: "k", promptCaching: true })).toEqual(["providerConverse"]);
    // not applicable on A2A: nothing to check
    expect(keys({ provider: "litellm" }, { ...CTX, protocol: "a2a" })).toEqual([]);
  });
});

describe("tool-call guards and retry", () => {
  it("maps the guards", () => {
    const spec = strandsSpec(strands({ approvalTools: "send_email, crm___*  send_email", maxToolCalls: "8",
                                       logToolCalls: true, maskArgs: "SSN card_number" }), HTTP);
    expect(spec.tool_hooks).toEqual({ approval_tools: ["send_email", "crm___*"], max_tool_calls_per_turn: 8,
                                      log_tool_calls: true, mask_args: ["ssn", "card_number"] });
    // masking alone does nothing, so it is not sent
    expect(strandsSpec(strands({ maskArgs: "ssn" }), HTTP)).not.toHaveProperty("tool_hooks");
    expect(keys({ approvalTools: "a b, c/d" })).toEqual(["approvalTools"]);
    expect(keys({ maxToolCalls: "0" })).toEqual(["maxToolCalls"]);
  });

  it("maps retry and draws its backoff", () => {
    expect(strandsSpec(strands({ retry: true, retryAttempts: "3" }), HTTP).retry).toEqual({
      max_attempts: 3, initial_delay_s: 4, max_delay_s: 240 });
    expect(keys({ retry: true, retryInitial: "30", retryMax: "10" })).toEqual(["retryMax"]);
    expect(keys({ retry: true, retryAttempts: "11" })).toEqual(["retryAttempts"]);
    expect(keys({ retryAttempts: "99" })).toEqual([]); // off: not checked
    expect(backoffDelays(6, 4, 240)).toEqual([4, 8, 16, 32, 64]);
    expect(backoffDelays(5, 10, 25)).toEqual([10, 20, 25, 25]);
    expect(backoffDelays(1, 4, 240)).toEqual([]);
    expect(strandsFormFromSpec({ retry: { max_attempts: 2 } })).toMatchObject({ retry: true, retryAttempts: "2" });
  });

  it("blocks the wizard on an SB-2 issue", () => {
    expect(strandsValid(strands({ maxToolCalls: "x" }), CTX)).toBe(false);
  });
});

describe("locales", () => {
  it("has every SB-2 error key in both languages", () => {
    const all: Sb2IssueKey[] = ["agentToolName", "agentToolDuplicate", "agentToolDescription", "agentToolCount",
      "providerConnection", "providerBaseUrl", "providerConverse", "approvalTools", "maxToolCalls", "maskArgs",
      "retryAttempts", "retryInitial", "retryMax"];
    for (const k of all) {
      expect(en.strandsBuilder.err).toHaveProperty(k);
      expect(zh.strandsBuilder.err).toHaveProperty(k);
    }
    const flat = (o: object, p = ""): string[] =>
      Object.entries(o).flatMap(([k, v]) => (typeof v === "object" ? flat(v, `${p}${k}.`) : [`${p}${k}`]));
    for (const ns of ["agents", "guards", "provider", "retry"] as const) {
      expect(flat(zh.strandsBuilder[ns])).toEqual(flat(en.strandsBuilder[ns]));
    }
  });
});
