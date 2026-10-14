import { describe, expect, it } from "vitest";

import {
  type AgentFormCatalogs,
  agentFormValid,
  authToolIssues,
  buildAgentSpec,
  buildMcpServerSpec,
  byocIssues,
  type ConnectionRef,
  emptyAgentForm,
  filesystemIssues,
  formFromStoredSpec,
  hasByoMounts,
  republishSpec,
} from "./agent-spec";

const CATALOGS: AgentFormCatalogs = {
  gatewayTargets: [],
  remoteMcp: [],
  storedGatewayConfig: {},
  kbInfo: (id) => ({ kb_id: id, name: id, description: "" }),
};
const CONNECTIONS: ConnectionRef[] = [
  { name: "team-idp", kind: "oauth2", status: "ready" },
  { name: "team-key", kind: "api_key", status: "ready" },
];

const CRM = {
  type: "rest",
  name: "crm",
  config: { url: "https://crm.example/api", method: "POST" },
  auth: { connection: "team-idp", kind: "oauth2", mode: "as_agent", scopes: ["crm/read"], audience: null, api_key: null },
};
const FACTS = {
  type: "rest",
  name: "facts",
  config: { url: "https://facts.example" },
  auth: { connection: "team-key", kind: "api_key", mode: "as_agent", scopes: [], api_key: { in: "query", name: "key" } },
};
const STORED = {
  name: "auth-agent",
  method: "zip_runtime",
  system_prompt: "Be helpful.",
  model_id: "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
  tools: [CRM, FACTS, { type: "mcp", name: "docs", config: { url: "https://docs.example/mcp" } }],
};

const load = (stored: object) => formFromStoredSpec("zip_runtime", "auth-agent", stored).form;
const valid = (form: ReturnType<typeof load>, connections: ConnectionRef[] | null) =>
  agentFormValid(form, CATALOGS, { knobIssues: [], byocUploading: false, connections });

describe("tool-level auth round trip", () => {
  it("routes auth-carrying tools to the Identity rows, not the MCP picks", () => {
    const form = load(STORED);
    expect(form.authTools.map((r) => [r.type, r.name, r.connection, r.kind])).toEqual([
      ["rest", "crm", "team-idp", "oauth2"],
      ["rest", "facts", "team-key", "api_key"],
    ]);
    expect(form.authTools[0].extraConfig).toEqual({ method: "POST" });
    expect(form.selectedMcp).toEqual(["docs"]);
  });

  it("keeps every auth block when an unrelated field is edited and republished", () => {
    const form = { ...load(STORED), systemPrompt: "Now answer in French." };
    expect(valid(form, CONNECTIONS)).toBe(true);
    const spec = republishSpec(buildAgentSpec(form, CATALOGS), STORED);
    expect(spec.system_prompt).toBe("Now answer in French.");
    const byName = Object.fromEntries((spec.tools ?? []).map((tool) => [tool.name, tool]));
    expect(byName.crm.auth).toEqual({ connection: "team-idp", kind: "oauth2", mode: "as_agent", scopes: ["crm/read"] });
    expect(byName.crm.config).toEqual({ url: "https://crm.example/api", method: "POST" });
    expect(byName.facts.auth).toEqual({
      connection: "team-key",
      kind: "api_key",
      mode: "as_agent",
      scopes: [],
      api_key: { in: "query", name: "key" },
    });
  });

  it("maps the earlier fork's provider/flow shape", () => {
    const form = load({ ...STORED, tools: [{ ...CRM, auth: { provider: "team-key", flow: "API_KEY" } }] });
    expect(form.authTools[0]).toMatchObject({ connection: "team-key", kind: "api_key", mode: "as_agent" });
  });
});

describe("republish carry-through", () => {
  it("fills in auth only for a built tool that does not state it", () => {
    const built = { name: "auth-agent", method: "zip_runtime" as const, system_prompt: "p", tools: [{ type: "rest", name: "crm" }] };
    expect(republishSpec(built, STORED).tools?.[0].auth).toEqual(CRM.auth);
  });

  it("never back-fills a row the member turned open on purpose", () => {
    const form = load(STORED);
    form.authTools[0] = { ...form.authTools[0], connection: "" };
    const tools = republishSpec(buildAgentSpec(form, CATALOGS), STORED).tools ?? [];
    expect(tools.find((tool) => tool.name === "crm")?.auth).toBeNull();
  });
});

describe("a missing Connection blocks submission", () => {
  it("flags the row and refuses the form when the Connection left the vault", () => {
    const form = load(STORED);
    const gone = CONNECTIONS.filter((c) => c.name !== "team-idp");
    expect(authToolIssues(form, gone).rows[0]).toEqual({ connection: "missing" });
    expect(valid(form, gone)).toBe(false);
    // ...and the row is still there, not stripped
    expect(buildAgentSpec(form, CATALOGS).tools?.find((t) => t.name === "crm")?.auth?.connection).toBe("team-idp");
  });

  it("treats an audit row whose provider is missing as absent", () => {
    const form = load(STORED);
    const stale: ConnectionRef[] = [{ ...CONNECTIONS[0], status: "missing" }, CONNECTIONS[1]];
    expect(valid(form, stale)).toBe(false);
  });

  it("refuses while the catalog is unavailable", () => {
    const form = load(STORED);
    expect(authToolIssues(form, null).catalogPending).toBe(true);
    expect(valid(form, null)).toBe(false);
  });

  it("refuses a kind mismatch and an unimplemented acting mode", () => {
    const mismatched = load({ ...STORED, tools: [{ ...CRM, auth: { ...CRM.auth, kind: "api_key", scopes: [] } }] });
    expect(authToolIssues(mismatched, CONNECTIONS).rows[0]).toEqual({ connection: "kind" });
    const obo = load({ ...STORED, tools: [{ ...CRM, auth: { ...CRM.auth, mode: "obo" } }] });
    expect(authToolIssues(obo, CONNECTIONS).rows[0]).toEqual({ mode: true });
    expect(valid(obo, CONNECTIONS)).toBe(false);
  });

  it("accepts as_user on an OAuth2 Connection only", () => {
    const asUser = load({ ...STORED, tools: [{ ...CRM, auth: { ...CRM.auth, mode: "as_user" } }] });
    expect(authToolIssues(asUser, CONNECTIONS).rows[0]).toBeUndefined();
    expect(buildAgentSpec(asUser, CATALOGS).tools?.find((t) => t.name === "crm")?.auth?.mode).toBe("as_user");
    const keyAsUser = load({ ...STORED, tools: [{ ...FACTS, auth: { ...FACTS.auth, mode: "as_user" } }] });
    expect(authToolIssues(keyAsUser, CONNECTIONS).rows[0]).toEqual({ mode: true });
  });

  it("accepts an open rest tool with no catalog at all", () => {
    const form = load({ ...STORED, tools: [{ type: "rest", name: "open", config: { url: "https://open.example" } }] });
    expect(valid(form, null)).toBe(true);
  });

  it("refuses auth rows where nothing would send them", () => {
    const form = { ...load(STORED), protocol: "a2a" as const };
    expect(authToolIssues(form, CONNECTIONS).unsupported).toBe(true);
    expect(valid(form, CONNECTIONS)).toBe(false);
  });
});

describe("MCP stdio wrapper (§15.5)", () => {
  const server = () => ({
    ...emptyAgentForm("byoc"),
    name: "stdio_srv",
    protocol: "mcp" as const,
    mcpTargetName: "crm",
    byocUploadId: "up-1",
  });
  const on = { on: true, base: "node" as const, command: "node", args: "build/index.js" };

  it("writes stdio_wrapper only for a code zip with the switch on", () => {
    expect(buildMcpServerSpec(server()).mcp).toEqual({ target_name: "crm" });
    const spec = buildMcpServerSpec({ ...server(), mcpStdio: on });
    expect(spec.mcp).toEqual({ target_name: "crm", stdio_wrapper: { command: "node", args: ["build/index.js"], base: "node" } });
    const image = buildMcpServerSpec({ ...server(), mcpStdio: on, byocKind: "container_image", byocImageUri: "x" });
    expect(image.mcp).toEqual({ target_name: "crm" });
  });

  it("reads the wrapper back and stops asking for a .py entrypoint", () => {
    const stored = { protocol: "mcp", mcp: { target_name: "crm", stdio_wrapper: { command: "node", args: ["a.js"], base: "node" } } };
    expect(formFromStoredSpec("byoc", "stdio_srv", { ...stored, byoc: { artifact_kind: "code_zip" } }).form.mcpStdio).toEqual({ on: true, base: "node", command: "node", args: "a.js" });
    expect(formFromStoredSpec("byoc", "stdio_srv", { protocol: "mcp", mcp: { target_name: "crm" } }).form.mcpStdio?.on).toBe(false);
    expect(byocIssues({ ...server(), byocEntrypoint: "index.js" }).entrypoint).toBe(true);
    expect(byocIssues({ ...server(), byocEntrypoint: "index.js", mcpStdio: on }).entrypoint).toBe(false);
  });
});

describe("container network profile", () => {
  const stored = {
    model_id: "m",
    filesystem: { session_storage: null, s3_files: [{ access_point_arn: "arn:aws:s3files:us-east-1:1:ap/x", mount_path: "/mnt/data" }] },
    network: { profile_id: "np-1" },
  };

  it("keeps a stored profile reference instead of inline ids", () => {
    const form = formFromStoredSpec("container", "c1", stored).form;
    expect(form.networkProfileId).toBe("np-1");
    expect(buildAgentSpec(form, CATALOGS).network).toEqual({ profile_id: "np-1" });
  });

  it("a legacy inline network stays inline", () => {
    const form = formFromStoredSpec("container", "c1", {
      ...stored,
      network: { subnets: ["subnet-0a", "subnet-0b"], security_groups: ["sg-0a"] },
    }).form;
    expect(form.networkProfileId).toBe("");
    expect(buildAgentSpec(form, CATALOGS).network).toEqual({ subnets: ["subnet-0a", "subnet-0b"], security_groups: ["sg-0a"] });
  });
});

describe("network for every method (projects §12.6)", () => {
  it("every method carries a chosen profile; no profile is PUBLIC (omitted)", () => {
    for (const method of ["harness", "zip_runtime", "container"] as const) {
      const form = { ...emptyAgentForm(method), name: "net-a", systemPrompt: "hi" };
      expect(buildAgentSpec(form, CATALOGS).network).toBeUndefined();
      expect(buildAgentSpec({ ...form, networkProfileId: "np-1" }, CATALOGS).network).toEqual({ profile_id: "np-1" });
    }
    const byoc = { ...emptyAgentForm("byoc"), name: "net-b", byocUploadId: "u1", networkProfileId: "np-2" };
    expect(buildAgentSpec(byoc, CATALOGS).network).toEqual({ profile_id: "np-2" });
    const server = { ...byoc, protocol: "mcp" as const, mcpTargetName: "crm" };
    expect(buildMcpServerSpec(server).network).toEqual({ profile_id: "np-2" });
  });

  it("a stored profile round-trips on a harness re-publish, and leaving it drops the field", () => {
    const form = formFromStoredSpec("harness", "h1", { model_id: "m", network: { profile_id: "np-9" } }).form;
    expect(form.networkProfileId).toBe("np-9");
    expect(buildAgentSpec(form, CATALOGS).network).toEqual({ profile_id: "np-9" });
    expect(buildAgentSpec({ ...form, networkProfileId: "" }, CATALOGS).network).toBeUndefined();
  });

  it("VPC chosen without a profile blocks launch", () => {
    const form = { ...emptyAgentForm("harness"), name: "net-c", systemPrompt: "hi" };
    const opts = { knobIssues: [], byocUploading: false };
    expect(agentFormValid(form, CATALOGS, opts)).toBe(true);
    expect(agentFormValid({ ...form, networkVpc: true }, CATALOGS, opts)).toBe(false);
    expect(agentFormValid({ ...form, networkVpc: true, networkProfileId: "np-1" }, CATALOGS, opts)).toBe(true);
  });
});

describe("BYO mounts on every method (storage-mounts §3)", () => {
  const s3 = [{ arn: "arn:aws:s3files:us-east-1:1:ap/x", path: "/mnt/data" }];
  const efs = [{ arn: "arn:aws:elasticfilesystem:us-east-1:1:access-point/fsap-1", path: "/mnt/shared" }];
  const sent = {
    session_storage: { mount_path: "/mnt/workspace" },
    s3_files: [{ access_point_arn: s3[0].arn, mount_path: "/mnt/data" }],
    efs: [{ access_point_arn: efs[0].arn, mount_path: "/mnt/shared" }],
    mounts: [],
  };

  it("harness, zip_runtime, container, byoc and MCP servers send the same filesystem block", () => {
    for (const method of ["harness", "zip_runtime", "container"] as const) {
      const form = { ...emptyAgentForm(method), name: "fs-a", systemPrompt: "hi", s3Mounts: s3, efsMounts: efs, networkProfileId: "np-1" };
      expect(buildAgentSpec(form, CATALOGS).filesystem).toEqual(sent);
    }
    const byoc = { ...emptyAgentForm("byoc"), name: "fs-b", byocUploadId: "u1", s3Mounts: s3, efsMounts: efs, networkProfileId: "np-1" };
    expect(buildAgentSpec(byoc, CATALOGS).filesystem).toEqual(sent);
    expect(buildMcpServerSpec({ ...byoc, protocol: "mcp" as const, mcpTargetName: "crm" }).filesystem).toEqual(sent);
  });

  it("a mount without a profile needs one; only a container may use inline subnets", () => {
    for (const method of ["harness", "zip_runtime", "byoc"] as const) {
      const form = { ...emptyAgentForm(method), s3Mounts: s3, vpcSubnets: "subnet-1", vpcSgs: "sg-1" };
      expect(filesystemIssues(form).vpc).toBe(true);
      expect(filesystemIssues({ ...form, networkProfileId: "np-1" }).vpc).toBe(false);
    }
    const container = { ...emptyAgentForm("container"), s3Mounts: s3 };
    expect(filesystemIssues(container).vpc).toBe(true);
    expect(filesystemIssues({ ...container, vpcSubnets: "subnet-1", vpcSgs: "sg-1" }).vpc).toBe(false);
  });
});

describe("storage catalog mounts in the wizard (storage-mounts SM-2)", () => {
  const picks = [
    { storageId: "datasets", path: "/mnt/datasets", mode: "ro" as const },
    { storageId: "state", path: "/mnt/state", mode: "rw" as const },
  ];

  it("starts empty, sends `filesystem.mounts` and round-trips through a stored spec", () => {
    expect(emptyAgentForm("harness").catalogMounts).toEqual([]);
    const form = { ...emptyAgentForm("harness"), name: "fs-c", systemPrompt: "hi", catalogMounts: picks, networkProfileId: "np-1" };
    const spec = buildAgentSpec(form, CATALOGS);
    expect(spec.filesystem?.mounts).toEqual([
      { storage_id: "datasets", mount_path: "/mnt/datasets", mode: "ro" },
      { storage_id: "state", mount_path: "/mnt/state", mode: "rw" },
    ]);
    expect(formFromStoredSpec("harness", "fs-c", spec).form.catalogMounts).toEqual(picks);
  });

  it("counts catalog picks as BYO mounts: a profile is needed", () => {
    const form = { ...emptyAgentForm("zip_runtime"), catalogMounts: picks };
    expect(hasByoMounts(form)).toBe(true);
    expect(filesystemIssues(form).vpc).toBe(true);
    expect(filesystemIssues({ ...form, networkProfileId: "np-1" }).vpc).toBe(false);
  });

  it("checks catalog paths for duplicates and caps inline + catalog at four", () => {
    const base = { ...emptyAgentForm("container"), networkProfileId: "np-1" };
    const dup = { ...base, s3Mounts: [{ arn: "arn:aws:s3files:us-east-1:1:ap/x", path: "/mnt/datasets" }], catalogMounts: picks };
    expect(filesystemIssues(dup).duplicatePaths).toBe(true);
    const inline = [1, 2, 3].map((i) => ({ arn: `arn:aws:s3files:us-east-1:1:ap/${i}`, path: `/mnt/in${i}` }));
    expect(filesystemIssues({ ...base, s3Mounts: inline.slice(0, 2), catalogMounts: picks }).tooMany).toBe(false);
    expect(filesystemIssues({ ...base, s3Mounts: inline, catalogMounts: picks }).tooMany).toBe(true);
  });
});
