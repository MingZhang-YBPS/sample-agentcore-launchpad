import { describe, expect, it } from "vitest";

import { diffTools, gatewayNotices, policyCovers, toolsCount, toolsModel } from "./agent-tools";
import type { AgentInfo } from "./api";

type Method = AgentInfo["method"];
const model = (method: Method, spec: Record<string, unknown>) => toolsModel({ method }, spec);
const keys = (method: Method, spec: Record<string, unknown>) => model(method, spec).items.map((item) => item.key);

// the §18.1 agents, one fixture per method
const HARNESS = {
  tools: [{ type: "mcp", name: "aws_knowledge", config: { url: "https://knowledge-mcp.global.api.aws" } }],
  skills: ["s3://launchpad-skills/system/aws-architect/1.2.0/"],
  native_tools: ["shell", "file_operations"],
  allowed_tools: ["shell", "file_*", "@aws_knowledge", "@launchpad_kb_gw"],
  knowledge_bases: [{ kb_id: "KB1", name: "lab-unicorn-policies" }],
};
const ZIP = {
  knowledge_bases: [{ kb_id: "KB2", name: "service-docs" }],
  tools: [
    { type: "gateway", name: "hr-database", config: { record_id: "rec-1", gateway_id: "gw-1", tools: ["hr-database___read_employee"] } },
    { type: "gateway", name: "crm", config: { record_id: "rec-2", gateway_id: "gw-1" } },
    { type: "rest", name: "weather", config: { url: "https://api.weather.example/v1" }, auth: { kind: "api_key", mode: "as_agent" } },
  ],
};

describe("toolsModel per method", () => {
  it("harness: remote MCP with its URL, skills, native execution, allowed_tools tagged by source", () => {
    const m = model("harness", HARNESS);
    expect(m.mcp).toEqual([{ name: "aws_knowledge", url: "https://knowledge-mcp.global.api.aws", auth: null }]);
    expect(m.skills).toEqual([{ name: "1.2.0", path: "s3://launchpad-skills/system/aws-architect/1.2.0/" }]);
    expect(m.native).toEqual(["shell", "file_operations"]);
    expect(m.allowed).toEqual([
      { pattern: "shell", source: null },
      { pattern: "file_*", source: null },
      { pattern: "@aws_knowledge", source: "mcp" },
      { pattern: "@launchpad_kb_gw", source: "kb" },
    ]);
    expect(m.allowedDerived).toBe(false);
    // a Harness KB goes through the KB gateway, no baked-in kb_search
    expect(m.kbTools).toEqual([]);
    expect(m.template).toEqual([]);
    expect(m.items).toHaveLength(5); // mcp + kb + skill + 2 native
  });

  it("harness without allowed_tools derives them", () => {
    const m = model("harness", { skills: ["s3://b/agent-skills/rules/"] });
    expect(m.allowed).toBeNull();
    expect(m.allowedDerived).toBe(true);
    expect(m.skills[0].name).toBe("rules");
  });

  it("zip_runtime: gateway tools narrowed or whole-record, REST host, kb_search, template tools", () => {
    const m = model("zip_runtime", ZIP);
    expect(m.gateway).toEqual([
      { name: "hr-database", recordId: "rec-1", gatewayId: "gw-1", tools: ["hr-database___read_employee"], scope: "hr-database___*" },
      { name: "crm", recordId: "rec-2", gatewayId: "gw-1", tools: null, scope: "crm___*" },
    ]);
    expect(m.rest).toEqual([{ name: "weather", host: "api.weather.example" }]);
    expect(m.kbTools).toEqual(["kb_search", "kb_deep_search"]);
    expect(m.template).toEqual(["calculator", "current_utc_time"]);
    expect(keys("zip_runtime", ZIP)).toEqual([
      "gateway:hr-database:hr-database___read_employee",
      "gateway:crm:crm___*",
      "rest:weather",
      "kb:KB2",
      "template:calculator",
      "template:current_utc_time",
    ]);
    expect(m.allowed).toBeNull();
    expect(m.allowedDerived).toBe(false);
    expect(m.codeDefined).toBe(false);
  });

  it("a toolkit replaces the template tools; custom code has none", () => {
    const kit = model("zip_runtime", { toolkits: ["hr_assistant"] });
    expect(kit.template).toEqual([]);
    expect(kit.toolkits[0].name).toBe("hr_assistant");
    expect(kit.toolkits[0].tools.length).toBeGreaterThan(0);
    expect(model("zip_runtime", { code_bundle: { "main.py": "" } }).template).toEqual([]);
    expect(model("studio", { code: "x" }).template).toEqual([]);
  });

  it("A2A zip runtime: template tools only", () => {
    expect(keys("zip_runtime", { protocol: "a2a" })).toEqual(["template:calculator", "template:current_utc_time"]);
  });

  it("container: built-in tools and kb_search, no template tools", () => {
    const m = model("container", { tools: [{ type: "builtin", name: "code-interpreter" }], knowledge_bases: [{ kb_id: "K", name: "" }] });
    expect(m.builtin).toEqual(["code-interpreter"]);
    expect(m.kbs).toEqual([{ kbId: "K", name: "K" }]);
    expect(m.kbTools).toEqual(["kb_search", "kb_deep_search"]);
    expect(m.template).toEqual([]);
  });

  it("BYOC and imported runtimes keep their tools in code", () => {
    expect(model("byoc", { byoc: { artifact_kind: "code_zip" } })).toMatchObject({ codeDefined: true, items: [] });
    expect(model("discovered_runtime", {})).toMatchObject({ codeDefined: true, items: [] });
    // an MCP server is a tool itself, not code with tools
    expect(model("byoc", { protocol: "mcp" }).codeDefined).toBe(false);
  });

  it("counts every item for the stats row", () => {
    expect(toolsCount({ method: "zip_runtime", spec: ZIP } as unknown as AgentInfo)).toBe(6);
    expect(toolsCount({ method: "byoc", spec: {} } as unknown as AgentInfo)).toBe(0);
  });
});

describe("candidate vs live", () => {
  it("names the Gateway tool the candidate adds and the skill it drops", () => {
    const live = model("zip_runtime", { skills: ["s3://b/agent-skills/rules/"], tools: [ZIP.tools[1]] });
    const candidate = model("zip_runtime", { tools: [ZIP.tools[0], ZIP.tools[1]] });
    const diff = diffTools(candidate, live);
    expect(diff.added.map((item) => item.key)).toEqual(["gateway:hr-database:hr-database___read_employee"]);
    expect(diff.removed.map((item) => item.label)).toEqual(["rules"]);
  });

  it("no drift when the sets match", () => {
    const m = model("zip_runtime", ZIP);
    expect(diffTools(m, model("zip_runtime", ZIP))).toEqual({ added: [], removed: [] });
  });
});

describe("gateway policies", () => {
  const permit = (actions: string) => `permit(\n  principal is AgentCore::OAuthUser,\n  ${actions},\n  resource == AgentCore::Gateway::"arn"\n);`;

  it("an ACTIVE permit naming the tool covers it; LOG_ONLY or forbid does not", () => {
    const statement = permit('action == AgentCore::Action::"hr-database___read_employee"');
    expect(policyCovers({ statement, enforcement_mode: "ACTIVE" }, "hr-database___read_employee")).toBe(true);
    expect(policyCovers({ statement, enforcement_mode: "ACTIVE" }, "hr-database___delete_employee")).toBe(false);
    expect(policyCovers({ statement, enforcement_mode: "LOG_ONLY" }, "hr-database___read_employee")).toBe(false);
    expect(policyCovers({ statement: statement.replace("permit", "forbid"), enforcement_mode: "ACTIVE" }, "hr-database___read_employee")).toBe(false);
  });

  it("a permit with no action clause covers everything; a whole record needs any of its tools", () => {
    expect(policyCovers({ statement: permit("action"), enforcement_mode: "ACTIVE" }, "crm___*")).toBe(true);
    const list = permit('action in [AgentCore::Action::"crm___find", AgentCore::Action::"x___y"]');
    expect(policyCovers({ statement: list, enforcement_mode: "ACTIVE" }, "crm___*")).toBe(true);
    expect(policyCovers({ statement: list, enforcement_mode: "ACTIVE" }, "hr___*")).toBe(false);
    expect(policyCovers({ statement: `// note\n${permit("action")}`, enforcement_mode: "ACTIVE" }, "a___b")).toBe(true);
  });

  it("warns under ENFORCE for uncovered tools and for a record no longer approved", () => {
    const m = model("zip_runtime", ZIP);
    const covered = permit('action == AgentCore::Action::"hr-database___read_employee"');
    const notices = gatewayNotices(
      m,
      { "gw-1": { name: "launchpad-gw", mode: "ENFORCE", policies: [{ statement: covered, enforcement_mode: "ACTIVE" }] } },
      { "rec-1": "APPROVED", "rec-2": "DEPRECATED" },
    );
    expect(notices.map((n) => [n.gateway.name, n.uncovered, n.recordStatus])).toEqual([["crm", ["crm___*"], "DEPRECATED"]]);
    // LOG_ONLY never denies
    expect(gatewayNotices(m, { "gw-1": { name: "g", mode: "LOG_ONLY", policies: [] } }, {})).toEqual([]);
    // unknown gateway facts (no read access) warn about nothing
    expect(gatewayNotices(m, {}, {})).toEqual([]);
  });
});
