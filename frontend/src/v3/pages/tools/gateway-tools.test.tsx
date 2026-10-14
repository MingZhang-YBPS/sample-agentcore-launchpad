import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";

import i18n from "../../../i18n";
import {
  createLambdaTarget,
  parseToolSchema,
  rollbackMcpServer,
  scaffoldMcpPolicies,
  toolReviewApi,
  toolReviewPath,
  type McpScaffoldPlan,
  type McpServerOps,
  validateLambdaForm,
} from "../../../lib/api/mcpReviews";
import { McpServerOpsView } from "../agent/McpServerOps";

const SCHEMA = '[{"name": "lookup", "description": "Look up", "inputSchema": {"type": "object"}}]';
const ARN = "arn:aws:lambda:us-west-2:111122223333:function:crm-tools";

const renderOps = (ops: McpServerOps, plan: McpScaffoldPlan | null = null, mode: "" | "ENFORCE" = "") =>
  renderToStaticMarkup(
    <McpServerOpsView
      ops={ops}
      version="3"
      busy={false}
      plan={plan}
      mode={mode}
      confirmName=""
      reason=""
      onMode={() => undefined}
      onConfirmName={() => undefined}
      onReason={() => undefined}
      onCheck={() => undefined}
      onRollback={() => undefined}
      onPreview={() => undefined}
      onApply={() => undefined}
    />,
  );

beforeAll(async () => {
  vi.stubGlobal("document", { documentElement: {} });
  await i18n.changeLanguage("en");
});

describe("Lambda target form checks", () => {
  it("parses a tool list or a {tools} object and refuses other shapes", () => {
    expect(parseToolSchema(SCHEMA)).toEqual({ tools: [{ name: "lookup", description: "Look up", inputSchema: { type: "object" } }] });
    expect("tools" in parseToolSchema(`{"tools": ${SCHEMA}}`)).toBe(true);
    expect(parseToolSchema("{nope")).toEqual({ error: "json" });
    expect(parseToolSchema("[]")).toEqual({ error: "shape" });
    expect(parseToolSchema('[{"name": "x"}]')).toEqual({ error: "shape" });
  });

  it("validates the ARN, the target name and the schema", () => {
    expect(validateLambdaForm(ARN, "crm-tools", SCHEMA)).toEqual({});
    expect(validateLambdaForm(`${ARN}:prod`, "crm-tools", SCHEMA)).toEqual({});
    const errors = validateLambdaForm("arn:aws:s3:::bucket", "bad_name", "[");
    expect(errors).toEqual({ arn: "lambdaTool.form.badArn", name: "lambdaTool.form.badName", schema: "lambdaTool.form.badJson" });
  });
});

describe("gateway tool and server operation API helpers", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("posts to the encoded paths", async () => {
    expect(toolReviewPath("t/1", "r 1", "approve")).toBe("/api/gateway-tools/t%2F1/reviews/r%201/approve");
    const fetchMock = vi.fn(async () => new Response(JSON.stringify({}), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    await createLambdaTarget({ lambda_arn: ARN, target_name: "crm-tools", tool_schema: [] });
    await toolReviewApi("t1").approve("r2", "ok");
    await rollbackMcpServer("a1", "2");
    await scaffoldMcpPolicies("a1", { dry_run: true });
    const calls = fetchMock.mock.calls as unknown as [string, RequestInit][];
    expect(calls.map((c) => [c[0], c[1].method])).toEqual([
      ["/api/gateway-tools/lambda", "POST"],
      ["/api/gateway-tools/t1/reviews/r2/approve", "POST"],
      ["/api/mcp-servers/a1/rollback", "POST"],
      ["/api/mcp-servers/a1/policies/scaffold", "POST"],
    ]);
    expect(JSON.parse(String(calls[2][1].body))).toEqual({ version: "2" });
    expect(toolReviewApi("t1").check).toBeUndefined();
  });
});

describe("V3 MCP server operations", () => {
  it("shows health, the default endpoint and the rollback targets", () => {
    const html = renderOps({
      qualifier: "DEFAULT",
      health: { status: "healthy", tool_count: 4, checked_at: "2026-10-15T00:00:00Z" },
      rollback_versions: [
        { version: "3", review_id: "r3", decided_at: null },
        { version: "2", review_id: "r2", decided_at: null },
      ],
    });
    expect(html).toContain('data-testid="v3-mcp-ops"');
    expect(html).toContain("healthy");
    expect(html).toContain("4 tool(s)");
    expect(html).toContain("follows every new version");
    expect(html).toContain("Roll back to v2");
    expect(html).not.toContain("Roll back to v3"); // the version served now
  });

  it("shows a pinned endpoint, a failing alert, an operation and a scaffold plan", () => {
    const html = renderOps(
      {
        qualifier: "live",
        live_version: "2",
        health: { status: "failing", alert: true, consecutive_failures: 3, error: "401" },
        operation: { kind: "rollback", status: "failed", version: "2", error: "boom" },
        rollback_versions: [{ version: "2", review_id: "r2", decided_at: null }, { version: "3", review_id: "r3", decided_at: null }],
      },
      {
        gateway_id: "gw-1",
        gateway_name: "launchpad-gw",
        engine_id: "pe-1",
        mode: "LOG_ONLY",
        steps: [{ name: "lp_mcp_crm_project", action: "create", statement: "permit(...);" }],
        approved_review_id: "r3",
        open_review_id: null,
      },
      "ENFORCE",
    );
    expect(html).toContain("pinned at v2");
    expect(html).toContain("Roll back to v3");
    expect(html).toContain("3 health checks failed in a row");
    expect(html).toContain("boom");
    expect(html).toContain("lp_mcp_crm_project");
    expect(html).toContain("permit(...);");
    expect(html).toContain("Type the Gateway name");
  });
});
