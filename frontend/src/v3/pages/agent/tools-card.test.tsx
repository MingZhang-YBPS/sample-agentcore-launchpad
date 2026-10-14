import { renderToStaticMarkup } from "react-dom/server";
import { MemoryRouter } from "react-router-dom";
import { beforeAll, describe, expect, it, vi } from "vitest";

import i18n from "../../../i18n";
import type { AgentInfo } from "../../../lib/api";
import type { AgentTelemetry, ToolsFacts, VersionPair } from "../../../lib/api/agent-tools";
import { ToolsCardV2View } from "../../../v2/pages/agents/ToolsCard";
import { ToolsCardView } from "./ToolsCard";

const agent = (method: AgentInfo["method"], spec: Record<string, unknown>): AgentInfo =>
  ({ id: "a1", name: "lab", display_name: null, method, status: "active", version: "5", spec }) as unknown as AgentInfo;

const GATEWAY_SPEC = {
  tools: [
    { type: "gateway", name: "hr-database", config: { record_id: "rec-1", gateway_id: "gw-1", tools: ["hr-database___read_employee"] } },
    { type: "gateway", name: "crm", config: { record_id: "rec-2", gateway_id: "gw-1" } },
  ],
};
const FACTS: ToolsFacts = {
  records: { "rec-1": "APPROVED", "rec-2": "APPROVED" },
  gateways: { "gw-1": { name: "launchpad-gw", mode: "ENFORCE", policies: [] } },
  mcpAuth: {},
};
const tel = (over: Partial<AgentTelemetry>): AgentTelemetry => ({
  agent_id: "a1",
  endpoint: "DEFAULT",
  service_name: "lab.DEFAULT",
  status: "receiving",
  last_span_within: "24h",
  lookback_days: 14,
  reason: null,
  byoc_no_adot: false,
  checked_at: "2026-10-15T00:00:00Z",
  cached: false,
  ...over,
});

const v3 = (a: AgentInfo, facts: ToolsFacts | null, telemetry: AgentTelemetry | "error" | null, versions: VersionPair | null = null) =>
  renderToStaticMarkup(
    <MemoryRouter>
      <ToolsCardView agent={a} facts={facts} telemetry={telemetry} versions={versions} />
    </MemoryRouter>,
  );

beforeAll(async () => {
  vi.stubGlobal("document", { documentElement: {} });
  await i18n.changeLanguage("en");
  // the router's links use useLayoutEffect, which the static renderer warns about
  const error = console.error.bind(console);
  vi.spyOn(console, "error").mockImplementation((...args: unknown[]) => {
    if (String(args[0]).includes("useLayoutEffect does nothing on the server")) return;
    error(...args);
  });
});

describe("V3 Tools & capabilities card", () => {
  it("lists narrowed and whole-record Gateway tools with the gateway name and record status", () => {
    const html = v3(agent("zip_runtime", GATEWAY_SPEC), FACTS, tel({}));
    expect(html).toContain('data-testid="v3-agent-tools"');
    expect(html).toContain("Gateway tools");
    expect(html).toContain("hr-database___read_employee");
    expect(html).toContain("crm___*");
    expect(html).toContain("every tool of the record");
    expect(html).toContain("via gateway launchpad-gw");
    expect(html).toContain("APPROVED");
    // 2 gateway entries + 2 template tools
    expect(html).toMatch(/Tools &amp; capabilities <span class="v3-chip">4<\/span>/);
  });

  it("warns when the gateway is ENFORCE and no ACTIVE permit covers the tools, linking its policies", () => {
    const html = v3(agent("zip_runtime", GATEWAY_SPEC), FACTS, tel({}));
    expect(html).toContain('data-testid="v3-agent-tools-gateway-warning"');
    expect(html).toContain("launchpad-gw enforces its policies (ENFORCE)");
    expect(html).toContain('href="/v2/governance?view=gateway&amp;gateway=gw-1&amp;section=policies"');
    const covered: ToolsFacts = {
      ...FACTS,
      gateways: { "gw-1": { name: "launchpad-gw", mode: "ENFORCE", policies: [{ statement: "permit(principal, action, resource);", enforcement_mode: "ACTIVE" }] } },
    };
    expect(v3(agent("zip_runtime", GATEWAY_SPEC), covered, tel({}))).not.toContain("v3-agent-tools-gateway-warning");
  });

  it("flags a withdrawn record", () => {
    const html = v3(agent("zip_runtime", GATEWAY_SPEC), { ...FACTS, gateways: {}, records: { "rec-2": "DEPRECATED" } }, tel({}));
    expect(html).toContain("Registry record crm is DEPRECATED");
  });

  it("marks what the candidate adds over live and says what it removes", () => {
    const versions: VersionPair = {
      candidateVersion: "5",
      liveVersion: "4",
      candidateSpec: GATEWAY_SPEC,
      liveSpec: { tools: [GATEWAY_SPEC.tools[1]], skills: ["s3://b/agent-skills/rules/"] },
    };
    const html = v3(agent("zip_runtime", GATEWAY_SPEC), FACTS, tel({}), versions);
    expect(html).toContain('data-testid="v3-agent-tools-diff"');
    expect(html).toContain("Candidate v5 is not live yet (live: v4).");
    expect(html).toContain("The candidate adds: hr-database___read_employee.");
    expect(html).toContain("The candidate removes: rules.");
    expect(html).toContain("candidate only");
  });

  it("harness: allowed_tools as written, KB-gateway and MCP entries tagged", () => {
    const spec = {
      tools: [{ type: "mcp", name: "aws_knowledge", config: { url: "https://knowledge-mcp.global.api.aws" } }],
      allowed_tools: ["shell", "@aws_knowledge", "@launchpad_kb_gw"],
      native_tools: ["shell"],
      knowledge_bases: [{ kb_id: "KB1", name: "lab-unicorn-policies" }],
    };
    const html = v3(agent("harness", spec), FACTS, tel({}));
    expect(html).toContain("Remote MCP servers");
    expect(html).toContain("https://knowledge-mcp.global.api.aws");
    expect(html).toContain("no outbound auth");
    expect(html).toContain('href="/v3/knowledge?id=KB1"');
    expect(html).toContain("Native execution");
    expect(html).toContain('data-testid="v3-agent-tools-allowed"');
    expect(html).toContain("@launchpad_kb_gw</span><span style=\"margin-left:6px\">· knowledge base gateway");
    expect(html).toContain("@aws_knowledge</span><span style=\"margin-left:6px\">· remote MCP server");
  });

  it("BYOC code zip: tools in code, and no telemetry names the ADOT cause", () => {
    const html = v3(
      agent("byoc", { byoc: { artifact_kind: "code_zip" } }),
      FACTS,
      tel({ status: "none", last_span_within: null, byoc_no_adot: true, reason: "byoc_code_zip_no_adot" }),
    );
    expect(html).toContain("The tools are written in the agent&#x27;s code");
    expect(html).toContain('href="/v3/observability"');
    expect(html).toContain("no telemetry received in the last 14 days");
    expect(html).toContain("aws-opentelemetry-distro");
    expect(html).not.toContain("No tools or capabilities");
  });

  it("telemetry: the window, or an unknown status on error", () => {
    expect(v3(agent("zip_runtime", {}), FACTS, tel({ last_span_within: "1h" }))).toContain("last span within the last hour");
    expect(v3(agent("zip_runtime", {}), FACTS, "error")).toContain("telemetry could not be checked right now");
  });

  it("an agent with nothing registered says so", () => {
    expect(v3(agent("container", {}), null, null)).toContain("No tools or capabilities are registered for this agent.");
  });
});

describe("V2 card", () => {
  it("shows the same entries, warning and telemetry", () => {
    const html = renderToStaticMarkup(
      <MemoryRouter>
        <ToolsCardV2View agent={agent("zip_runtime", GATEWAY_SPEC)} facts={FACTS} telemetry={tel({})} />
      </MemoryRouter>,
    );
    expect(html).toContain('data-testid="v2-agent-tools"');
    expect(html).toContain("Tools &amp; capabilities · 4");
    expect(html).toContain("hr-database___read_employee");
    expect(html).toContain("crm___*");
    expect(html).toContain("launchpad-gw enforces its policies (ENFORCE)");
    expect(html).toContain("last span within the last 24 hours");
  });
});
