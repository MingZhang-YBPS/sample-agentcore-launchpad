/**
 * What an agent can call, read from its spec (projects §18): the detail pages'
 * "Tools & capabilities" card (V3 overview, V2 detail) and its count. Pure — the
 * live facts (registry status, gateway policies, the live version's spec) come from
 * `lib/api/agent-tools.ts` and are folded in by `gatewayNotices` / `diffTools`.
 *
 * The derivation follows what the deployers do with a spec: Gateway tools reach
 * "record___tool", or every tool of the record ("record___*") when config.tools
 * is absent; a generated Strands runtime ships calculator / current_utc_time unless a
 * toolkit replaces them (the wizard's `templateToolsHint`), zip / container KBs are
 * the baked-in `kb_search` / `kb_deep_search`, a Harness KB is the
 * `launchpad_kb_gw` gateway (`@launchpad_kb_gw` in `allowedTools`).
 */
import { skillNameFromPath, toolkitToolNames } from "./agent-spec";
import type { AgentInfo, GovernancePolicy, Toolkit } from "./api";

/** The template tools of a generated Strands runtime (backend `templates/`). */
export const TEMPLATE_TOOLS = ["calculator", "current_utc_time"] as const;
/** The retrieval tools a zip / container runtime gets per mounted KB set (`kb_support.py`). */
export const KB_CODE_TOOLS = ["kb_search", "kb_deep_search"] as const;
/** The Harness tool name of the managed KB gateway (`deployer/harness.py`). */
export const KB_GATEWAY_TOOL = "launchpad_kb_gw";

export type ToolGroup = "gateway" | "mcp" | "builtin" | "rest" | "kb" | "skill" | "toolkit" | "native" | "template";

/** One callable thing, keyed so two versions' sets can be compared. */
export interface ToolItem {
  key: string;
  group: ToolGroup;
  label: string;
}

export interface GatewayTool {
  /** the Registry record name — the Gateway target prefix */
  name: string;
  recordId: string | null;
  gatewayId: string | null;
  /** the narrowed `<target>___<tool>` names; null = every tool of the record */
  tools: string[] | null;
  /** `<name>___*` — what a config-less attachment reaches */
  scope: string;
}

export interface RemoteMcp {
  name: string;
  url: string | null;
  /** outbound auth kind (oauth2 / api_key) and acting mode — never a credential */
  auth: { kind: string; mode: string } | null;
}

export interface AllowedPattern {
  pattern: string;
  /** where an `@<server>` pattern points, when the spec says */
  source: "kb" | "mcp" | null;
}

export interface ToolsModel {
  gateway: GatewayTool[];
  mcp: RemoteMcp[];
  builtin: string[];
  rest: { name: string; host: string | null }[];
  kbs: { kbId: string; name: string }[];
  /** tools the KBs add to the generated code (zip / container) */
  kbTools: string[];
  skills: { name: string; path: string }[];
  toolkits: { name: string; tools: string[] }[];
  native: string[];
  template: string[];
  /** Harness `allowed_tools` as written; null = derived (or not a Harness) */
  allowed: AllowedPattern[] | null;
  /** a Harness whose `allowed_tools` is derived from its attachments */
  allowedDerived: boolean;
  /** BYOC / imported runtime: the tools live in the code, the platform records none */
  codeDefined: boolean;
  items: ToolItem[];
}

interface SpecTool {
  type?: string;
  name?: string;
  config?: Record<string, unknown>;
  auth?: { kind?: string; mode?: string } | null;
}

interface ToolSpec {
  protocol?: string;
  tools?: SpecTool[];
  toolkits?: string[];
  skills?: string[];
  native_tools?: string[];
  allowed_tools?: string[] | null;
  knowledge_bases?: { kb_id: string; name?: string }[];
  code?: string | null;
  code_bundle?: Record<string, string> | null;
}

const hostOf = (url: unknown): string | null => {
  if (typeof url !== "string" || !url) return null;
  try {
    return new URL(url).host || null;
  } catch {
    return null;
  }
};

const str = (value: unknown): string | null => (typeof value === "string" && value ? value : null);

/** The agent's tools and capabilities as the card lists them. */
export function toolsModel(agent: Pick<AgentInfo, "method">, rawSpec: unknown): ToolsModel {
  const spec = (rawSpec ?? {}) as ToolSpec;
  const method = agent.method;
  const tools = Array.isArray(spec.tools) ? spec.tools : [];
  const gateway: GatewayTool[] = tools
    .filter((tool) => tool.type === "gateway" && tool.name)
    .map((tool) => {
      const config = tool.config ?? {};
      const narrowed = Array.isArray(config.tools) && config.tools.length ? config.tools.map(String) : null;
      return {
        name: tool.name!,
        recordId: str(config.record_id),
        gatewayId: str(config.gateway_id),
        tools: narrowed,
        scope: `${tool.name}___*`,
      };
    });
  const mcp: RemoteMcp[] = tools
    .filter((tool) => tool.type === "mcp" && tool.name)
    .map((tool) => ({
      name: tool.name!,
      url: str(tool.config?.url),
      auth: tool.auth?.kind ? { kind: tool.auth.kind, mode: tool.auth.mode ?? "as_agent" } : null,
    }));
  const builtin = tools.filter((tool) => tool.type === "builtin" && tool.name).map((tool) => tool.name!);
  const rest = tools
    .filter((tool) => tool.type === "rest" && tool.name)
    .map((tool) => ({ name: tool.name!, host: hostOf(tool.config?.url) }));
  const kbs = (spec.knowledge_bases ?? []).map((kb) => ({ kbId: kb.kb_id, name: kb.name || kb.kb_id }));
  const kbTools = kbs.length && (method === "zip_runtime" || method === "container") ? [...KB_CODE_TOOLS] : [];
  const skills = (spec.skills ?? []).map((path) => ({ name: skillNameFromPath(path), path }));
  const kits = (spec.toolkits ?? []) as Toolkit[];
  const toolkits = kits.map((kit) => ({ name: kit, tools: toolkitToolNames([kit]) }));
  const native = method === "harness" ? [...(spec.native_tools ?? [])] : [];
  // a generated Strands runtime only: custom code (studio canvas, a converted
  // bundle) decides its own tools, and a toolkit replaces the template's pair
  const generated = method === "zip_runtime" && !spec.code && !spec.code_bundle;
  const template = generated && !toolkitToolNames(kits).length ? [...TEMPLATE_TOOLS] : [];
  const mcpNames = new Set(mcp.map((server) => server.name));
  const allowed =
    method === "harness" && Array.isArray(spec.allowed_tools)
      ? spec.allowed_tools.map((pattern): AllowedPattern => {
          const server = pattern.startsWith("@") ? pattern.slice(1).split("/")[0] : null;
          return {
            pattern,
            source: server === KB_GATEWAY_TOOL ? "kb" : server && mcpNames.has(server) ? "mcp" : null,
          };
        })
      : null;
  const codeDefined = (method === "byoc" && spec.protocol !== "mcp") || method === "discovered_runtime";

  const items: ToolItem[] = [
    ...gateway.flatMap((gw) =>
      (gw.tools ?? [gw.scope]).map((tool) => ({ key: `gateway:${gw.name}:${tool}`, group: "gateway" as const, label: tool })),
    ),
    ...mcp.map((server) => ({ key: `mcp:${server.name}`, group: "mcp" as const, label: server.name })),
    ...builtin.map((name) => ({ key: `builtin:${name}`, group: "builtin" as const, label: name })),
    ...rest.map((api) => ({ key: `rest:${api.name}`, group: "rest" as const, label: api.name })),
    ...kbs.map((kb) => ({ key: `kb:${kb.kbId}`, group: "kb" as const, label: kb.name })),
    ...skills.map((skill) => ({ key: `skill:${skill.path}`, group: "skill" as const, label: skill.name })),
    ...toolkits.map((kit) => ({ key: `toolkit:${kit.name}`, group: "toolkit" as const, label: kit.name })),
    ...native.map((name) => ({ key: `native:${name}`, group: "native" as const, label: name })),
    ...template.map((name) => ({ key: `template:${name}`, group: "template" as const, label: name })),
  ];
  return {
    gateway,
    mcp,
    builtin,
    rest,
    kbs,
    kbTools,
    skills,
    toolkits,
    native,
    template,
    allowed,
    allowedDerived: method === "harness" && !Array.isArray(spec.allowed_tools),
    codeDefined,
    items,
  };
}

/** The number the stats row shows: every listed tool and capability. */
export const toolsCount = (agent: Pick<AgentInfo, "method" | "spec">) => toolsModel(agent, agent.spec).items.length;

export interface ToolsDiff {
  added: ToolItem[];
  removed: ToolItem[];
}

/** What the candidate version adds to / removes from the live one. */
export function diffTools(candidate: ToolsModel, live: ToolsModel): ToolsDiff {
  const liveKeys = new Set(live.items.map((item) => item.key));
  const candidateKeys = new Set(candidate.items.map((item) => item.key));
  return {
    added: candidate.items.filter((item) => !liveKeys.has(item.key)),
    removed: live.items.filter((item) => !candidateKeys.has(item.key)),
  };
}

// a statement constrains the action when it compares it (`action == …` / `action in [...]`)
const ACTION_CLAUSE = /\baction\s*(==|in\b)/;
const stripComments = (statement: string) => statement.replace(/\/\/[^\n]*/g, "");

/**
 * Whether an ACTIVE `permit` lets the tool through an ENFORCE gateway. A statement
 * with no action clause permits every action; otherwise it must name the tool
 * (`AgentCore::Action::"<target>___<tool>"`), or — for a whole-record attachment —
 * some tool of the record. A heuristic over the Cedar text, not an evaluation:
 * principal / resource conditions are not checked, so it errs towards "covered"
 * and the warning only fires when no policy could possibly allow the call.
 */
export function policyCovers(policy: Pick<GovernancePolicy, "statement" | "enforcement_mode">, tool: string): boolean {
  if (policy.enforcement_mode !== "ACTIVE") return false;
  const statement = stripComments(policy.statement ?? "");
  if (!/^\s*permit\s*\(/.test(statement)) return false;
  if (!ACTION_CLAUSE.test(statement)) return true;
  if (tool.endsWith("___*")) return statement.includes(`"${tool.slice(0, -1)}`);
  return statement.includes(`"${tool}"`);
}

/** One gateway's governance facts as `lib/api/agent-tools.ts` loads them. */
export interface GatewayFacts {
  name: string;
  mode: "ENFORCE" | "LOG_ONLY" | null;
  policies: Pick<GovernancePolicy, "statement" | "enforcement_mode">[];
}

export interface GatewayNotice {
  gateway: GatewayTool;
  /** ENFORCE and no ACTIVE permit covers these tools: calls are denied by default */
  uncovered: string[];
  /** the record is no longer APPROVED (withdrawn, deprecated, deleted) */
  recordStatus: string | null;
}

/** The availability warnings of the Gateway tools (projects §18.3-3). */
export function gatewayNotices(
  model: ToolsModel,
  gateways: Record<string, GatewayFacts | undefined>,
  records: Record<string, string | undefined>,
): GatewayNotice[] {
  return model.gateway.flatMap((gw) => {
    const facts = gw.gatewayId ? gateways[gw.gatewayId] : undefined;
    const uncovered =
      facts?.mode === "ENFORCE"
        ? (gw.tools ?? [gw.scope]).filter((tool) => !facts.policies.some((policy) => policyCovers(policy, tool)))
        : [];
    const status = gw.recordId ? records[gw.recordId] : undefined;
    const recordStatus = status && status !== "APPROVED" ? status : null;
    return uncovered.length || recordStatus ? [{ gateway: gw, uncovered, recordStatus }] : [];
  });
}
