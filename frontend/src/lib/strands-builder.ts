/**
 * Strands builder options (design strands-builder SB-1 / SB-2) — the zip_runtime form
 * slice behind the wizard's "Tools / Model behaviour / Conversation & output"
 * groups, its spec mapping and the same checks the backend runs
 * (`backend/app/schemas/strands_builder.py`). Pure: nothing renders here.
 *
 * Every field is optional on the spec and omitted while unset, so an agent that
 * uses none of them posts exactly the spec it posted before SB-1.
 *
 * SB-2 adds agents-as-tools, the Anthropic API / LiteLLM gateway providers (the key
 * stays behind an Identity Connection), tool-call guards and throttling retry. Its
 * checks live in `sb2Issues` (own keys, so the SB-1 group map stays exhaustive);
 * `strandsValid` covers both.
 */
import type { AgentInfo } from "./api";
import type { ModelSource } from "./models";

/** The curated first batch of `strands-agents-tools` (D-1). shell / python_repl /
 *  file_write / use_aws need a sandbox and arrive with SB-3. */
export const STRANDS_TOOLS = [
  "current_time",
  "calculator",
  "http_request",
  "think",
  "retrieve",
  "image_reader",
  "journal",
] as const;
export type StrandsTool = (typeof STRANDS_TOOLS)[number];

export type ConversationChoice = "default" | "sliding_window" | "summarizing";

export interface FewShotRow {
  question: string;
  answer: string;
}

export interface VariableRow {
  name: string;
  value: string;
}

/** One other agent offered as a tool. Only id / name / description are posted:
 *  the runtime ARN and protocol are server-owned (`services/agent_tools.py`). */
export interface AgentToolRow {
  agentId: string;
  /** the agent's label when picked (display only) */
  label: string;
  toolName: string;
  description: string;
  protocol: "http" | "a2a";
  /** the project that lent it (§11.2); null for this project's own */
  sharedFrom: string | null;
}

export type ProviderChoice = "bedrock" | "anthropic" | "litellm";
export const PROVIDERS: ProviderChoice[] = ["bedrock", "anthropic", "litellm"];

/** Free text throughout, so a half-typed number is validated rather than lost. */
export interface StrandsForm {
  tools: StrandsTool[];
  /** http_request allowed hosts, comma / whitespace separated */
  httpHosts: string;
  temperature: string;
  topP: string;
  maxTokens: string;
  /** one stop sequence per line */
  stopSequences: string;
  thinking: boolean;
  thinkingBudget: string;
  promptCaching: boolean;
  conversation: ConversationChoice;
  windowSize: string;
  summaryModel: string;
  /** JSON Schema text; blank ⇒ no structured output */
  outputSchema: string;
  fewShot: FewShotRow[];
  variables: VariableRow[];
  /* ── SB-2 ── */
  agentTools: AgentToolRow[];
  provider: ProviderChoice;
  /** an api_key Connection name */
  providerConnection: string;
  providerBaseUrl: string;
  /** tool names / * patterns, comma or whitespace separated */
  approvalTools: string;
  maxToolCalls: string;
  logToolCalls: boolean;
  /** argument names, comma or whitespace separated */
  maskArgs: string;
  retry: boolean;
  retryAttempts: string;
  retryInitial: string;
  retryMax: string;
}

/** Strands' own ModelRetryStrategy defaults (6 attempts, 4 s doubling to 240 s). */
export const RETRY_DEFAULTS = { attempts: 6, initial: 4, max: 240 } as const;
export const MAX_AGENT_TOOLS = 10;

export const DEFAULT_WINDOW = 40;
export const DEFAULT_THINKING_BUDGET = 4096;

export const emptyStrandsForm = (): StrandsForm => ({
  tools: [],
  httpHosts: "",
  temperature: "",
  topP: "",
  maxTokens: "",
  stopSequences: "",
  thinking: false,
  thinkingBudget: String(DEFAULT_THINKING_BUDGET),
  promptCaching: false,
  conversation: "default",
  windowSize: String(DEFAULT_WINDOW),
  summaryModel: "",
  outputSchema: "",
  fewShot: [],
  variables: [],
  agentTools: [],
  provider: "bedrock",
  providerConnection: "",
  providerBaseUrl: "",
  approvalTools: "",
  maxToolCalls: "",
  logToolCalls: false,
  maskArgs: "",
  retry: false,
  retryAttempts: String(RETRY_DEFAULTS.attempts),
  retryInitial: String(RETRY_DEFAULTS.initial),
  retryMax: String(RETRY_DEFAULTS.max),
});

/** The spec fields this slice owns (all optional). */
export interface StrandsSpecFields {
  strands_tools?: StrandsTool[];
  strands_tool_config?: { http_request?: { allowed_hosts: string[] } | null } | null;
  model_params?: {
    temperature?: number | null;
    top_p?: number | null;
    max_tokens?: number | null;
    stop_sequences?: string[];
    thinking_budget_tokens?: number | null;
    prompt_caching?: boolean;
  } | null;
  conversation?: { strategy?: "sliding_window" | "summarizing"; window_size?: number; summary_model?: string | null } | null;
  output_schema?: Record<string, unknown> | null;
  few_shot_examples?: FewShotRow[];
  prompt_variables?: Record<string, string>;
  agent_tools?: {
    agent_id: string;
    tool_name: string;
    description: string;
    /* server-owned, read back on edit */
    protocol?: "http" | "a2a";
    shared_from?: string | null;
  }[];
  model_provider?: { kind: "anthropic" | "litellm"; connection: string; base_url?: string | null } | null;
  tool_hooks?: {
    approval_tools?: string[];
    max_tool_calls_per_turn?: number | null;
    log_tool_calls?: boolean;
    mask_args?: string[];
  } | null;
  retry?: { max_attempts?: number; initial_delay_s?: number; max_delay_s?: number } | null;
}

const HOST_RE = /^(\*\.)?(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$/;
export const PROMPT_VARIABLE_RE = /^[A-Za-z_][A-Za-z0-9_]{0,63}$/;
const SCHEMA_PROPERTY_RE = /^[A-Za-z][A-Za-z0-9_]{0,63}$/;
const SCHEMA_SCALARS = new Set(["string", "integer", "number", "boolean"]);
const SCHEMA_MAX_DEPTH = 5;
const SCHEMA_MAX_CHARS = 20000;
const PLACEHOLDER_RE = /\{\{\s*([A-Za-z_][A-Za-z0-9_]{0,63})\s*\}\}/g;

export const isClaudeModel = (modelId: string) =>
  modelId.includes("anthropic.claude") || modelId.toLowerCase().includes("claude-");

export const splitHosts = (s: string) =>
  s
    .split(/[\s,]+/)
    .map((h) => h.trim().toLowerCase().replace(/\.$/, ""))
    .filter(Boolean);

const num = (s: string): number | null => {
  const t = s.trim();
  if (!t) return null;
  const n = Number(t);
  return Number.isFinite(n) ? n : Number.NaN;
};

/** `{{name}}` placeholders in a prompt, first-seen order, deduplicated. */
export function promptPlaceholders(prompt: string): string[] {
  const out: string[] = [];
  for (const m of prompt.matchAll(PLACEHOLDER_RE)) if (!out.includes(m[1])) out.push(m[1]);
  return out;
}

/* ── spec ⇄ form ─────────────────────────────────────────────────────────── */

export function strandsFormFromSpec(raw: unknown): StrandsForm {
  const spec = (raw ?? {}) as StrandsSpecFields;
  const base = emptyStrandsForm();
  const params = spec.model_params ?? {};
  const conv = spec.conversation;
  return {
    ...base,
    tools: (spec.strands_tools ?? []).filter((t): t is StrandsTool => (STRANDS_TOOLS as readonly string[]).includes(t)),
    httpHosts: (spec.strands_tool_config?.http_request?.allowed_hosts ?? []).join(", "),
    temperature: params.temperature == null ? "" : String(params.temperature),
    topP: params.top_p == null ? "" : String(params.top_p),
    maxTokens: params.max_tokens == null ? "" : String(params.max_tokens),
    stopSequences: (params.stop_sequences ?? []).join("\n"),
    thinking: params.thinking_budget_tokens != null,
    thinkingBudget: String(params.thinking_budget_tokens ?? DEFAULT_THINKING_BUDGET),
    promptCaching: Boolean(params.prompt_caching),
    conversation: conv ? (conv.strategy ?? "sliding_window") : "default",
    windowSize: String(conv?.window_size ?? DEFAULT_WINDOW),
    summaryModel: conv?.summary_model ?? "",
    outputSchema: spec.output_schema ? JSON.stringify(spec.output_schema, null, 2) : "",
    fewShot: (spec.few_shot_examples ?? []).map((e) => ({ question: e.question ?? "", answer: e.answer ?? "" })),
    variables: Object.entries(spec.prompt_variables ?? {}).map(([name, value]) => ({ name, value })),
    ...sb2FormFromSpec(spec),
  };
}

function sb2FormFromSpec(spec: StrandsSpecFields): Partial<StrandsForm> {
  const hooks = spec.tool_hooks ?? {};
  const retry = spec.retry;
  return {
    agentTools: (spec.agent_tools ?? []).map((t) => ({
      agentId: t.agent_id,
      label: t.agent_id,
      toolName: t.tool_name ?? "",
      description: t.description ?? "",
      protocol: t.protocol === "a2a" ? "a2a" : "http",
      sharedFrom: t.shared_from ?? null,
    })),
    provider: spec.model_provider?.kind ?? "bedrock",
    providerConnection: spec.model_provider?.connection ?? "",
    providerBaseUrl: spec.model_provider?.base_url ?? "",
    approvalTools: (hooks.approval_tools ?? []).join(", "),
    maxToolCalls: hooks.max_tool_calls_per_turn == null ? "" : String(hooks.max_tool_calls_per_turn),
    logToolCalls: Boolean(hooks.log_tool_calls),
    maskArgs: (hooks.mask_args ?? []).join(", "),
    retry: retry != null,
    retryAttempts: String(retry?.max_attempts ?? RETRY_DEFAULTS.attempts),
    retryInitial: String(retry?.initial_delay_s ?? RETRY_DEFAULTS.initial),
    retryMax: String(retry?.max_delay_s ?? RETRY_DEFAULTS.max),
  };
}

interface SpecContext {
  protocol: "http" | "a2a" | "mcp";
  modelId: string;
  modelSource: ModelSource;
}

/** The fields to post — only what is set. Expects a valid form (`strandsIssues`). */
export function strandsSpec(form: StrandsForm | undefined, ctx: SpecContext): StrandsSpecFields {
  if (!form) return {};
  const out: StrandsSpecFields = {};
  if (form.tools.length) out.strands_tools = [...form.tools];
  if (form.tools.includes("http_request")) {
    out.strands_tool_config = { http_request: { allowed_hosts: splitHosts(form.httpHosts) } };
  }
  const params: NonNullable<StrandsSpecFields["model_params"]> = {};
  const temperature = num(form.temperature);
  const topP = num(form.topP);
  const maxTokens = num(form.maxTokens);
  if (temperature !== null) params.temperature = temperature;
  if (topP !== null) params.top_p = topP;
  if (maxTokens !== null) params.max_tokens = maxTokens;
  const stops = stopList(form.stopSequences);
  if (stops.length && bedrockOnlyApplies(ctx)) params.stop_sequences = stops;
  if (thinkingApplies(form, ctx)) params.thinking_budget_tokens = num(form.thinkingBudget) ?? DEFAULT_THINKING_BUDGET;
  if (form.promptCaching && bedrockOnlyApplies(ctx)) params.prompt_caching = true;
  if (Object.keys(params).length) out.model_params = params;
  if (form.conversation !== "default") {
    out.conversation = {
      strategy: form.conversation,
      window_size: num(form.windowSize) ?? DEFAULT_WINDOW,
      ...(form.conversation === "summarizing" && form.summaryModel.trim() && bedrockOnlyApplies(ctx)
        ? { summary_model: form.summaryModel.trim() }
        : {}),
    };
  }
  if (form.outputSchema.trim() && ctx.protocol !== "a2a") {
    out.output_schema = JSON.parse(form.outputSchema) as Record<string, unknown>;
  }
  const shots = form.fewShot.filter((e) => e.question.trim() || e.answer.trim());
  if (shots.length) out.few_shot_examples = shots.map((e) => ({ question: e.question.trim(), answer: e.answer.trim() }));
  const vars = form.variables.filter((v) => v.name.trim());
  if (vars.length) out.prompt_variables = Object.fromEntries(vars.map((v) => [v.name.trim(), v.value]));
  return { ...out, ...sb2Spec(form, ctx) };
}

/** SB-2's fields — again only what is set. */
function sb2Spec(form: StrandsForm, ctx: SpecContext): StrandsSpecFields {
  const out: StrandsSpecFields = {};
  // older drafts (saved before SB-2) carry no SB-2 slice at all
  const f = { ...emptyStrandsForm(), ...form };
  if (f.agentTools.length) {
    out.agent_tools = f.agentTools.map((t) => ({
      agent_id: t.agentId,
      tool_name: t.toolName.trim(),
      description: t.description.trim(),
    }));
  }
  if (f.provider !== "bedrock" && providerApplies(ctx)) {
    out.model_provider = {
      kind: f.provider,
      connection: f.providerConnection.trim(),
      ...(f.providerBaseUrl.trim() ? { base_url: f.providerBaseUrl.trim() } : {}),
    };
  }
  const hooks: NonNullable<StrandsSpecFields["tool_hooks"]> = {};
  const approval = splitList(f.approvalTools);
  if (approval.length) hooks.approval_tools = approval;
  const cap = num(f.maxToolCalls);
  if (cap !== null) hooks.max_tool_calls_per_turn = cap;
  if (f.logToolCalls) hooks.log_tool_calls = true;
  const mask = splitList(f.maskArgs).map((m) => m.toLowerCase());
  if (mask.length && (f.logToolCalls || approval.length)) hooks.mask_args = mask;
  if (Object.keys(hooks).length) out.tool_hooks = hooks;
  if (f.retry) {
    out.retry = {
      max_attempts: num(f.retryAttempts) ?? RETRY_DEFAULTS.attempts,
      initial_delay_s: num(f.retryInitial) ?? RETRY_DEFAULTS.initial,
      max_delay_s: num(f.retryMax) ?? RETRY_DEFAULTS.max,
    };
  }
  return out;
}

export const splitList = (s: string) =>
  [...new Set(s.split(/[\s,]+/).map((x) => x.trim()).filter(Boolean))];

/** A provider replaces the Bedrock Converse model: HTTP agents on Bedrock only
 *  (the A2A template runs on Bedrock; Mantle is a Bedrock surface of its own). */
export const providerApplies = (ctx: Pick<SpecContext, "modelSource" | "protocol">) =>
  ctx.protocol === "http" && ctx.modelSource === "bedrock";

/** The model-call delays a retry setting produces (seconds, one per retry). */
export function backoffDelays(attempts: number, initial: number, max: number): number[] {
  const out: number[] = [];
  for (let i = 0; i < Math.max(0, Math.min(attempts, 10) - 1); i++) out.push(Math.min(initial * 2 ** i, max));
  return out;
}

/** A tool name suggested for an agent: `ask_<slug>`. */
export const agentToolName = (agentName: string) =>
  `ask_${agentName.toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "")}`.slice(0, 64);

export type AgentToolBlock = "self" | "not_runtime" | "not_deployed" | "protocol" | "jwt_inbound";

/** Why an agent cannot be offered as a tool (the backend's `eligibility`), or null. */
export function agentToolBlock(agent: AgentInfo, selfName: string): AgentToolBlock | null {
  if (selfName && agent.name === selfName) return "self";
  if (!["zip_runtime", "studio", "container", "byoc", "discovered_runtime"].includes(agent.method)) return "not_runtime";
  if (agent.status !== "active" || !agent.arn) return "not_deployed";
  const protocol = (agent.spec?.protocol as string | undefined) ?? "http";
  if (protocol !== "http" && protocol !== "a2a") return "protocol";
  if (agent.inbound_auth_mode === "jwt") return "jwt_inbound";
  return null;
}

/** A row for a freshly picked agent. */
export function agentToolRow(agent: AgentInfo): AgentToolRow {
  const label = agent.display_name || agent.name;
  return {
    agentId: agent.id,
    label,
    toolName: agentToolName(agent.name),
    description: `Ask the ${label} agent. Send a complete, self-contained request.`,
    protocol: (agent.spec?.protocol as string | undefined) === "a2a" ? "a2a" : "http",
    sharedFrom: null,
  };
}

const stopList = (s: string) =>
  s
    .split("\n")
    .map((x) => x.trim())
    .filter(Boolean);

/** Extended thinking is a Claude-on-Bedrock option; the form keeps the switch
 *  for a model change but only sends it where it applies. */
export const thinkingApplies = (form: StrandsForm, ctx: Pick<SpecContext, "modelId" | "modelSource" | "protocol">) =>
  form.thinking && isClaudeModel(ctx.modelId) && bedrockOnlyApplies(ctx);

/** Converse-only options (stop / thinking / caching / summary model): never Mantle.
 *  The A2A template always runs on Bedrock. */
export const bedrockOnlyApplies = (ctx: Pick<SpecContext, "modelSource" | "protocol">) =>
  ctx.modelSource === "bedrock" || ctx.protocol === "a2a";

/** How many of the builder fields are set — the group headers' readout. */
export function strandsSetCount(form: StrandsForm | undefined) {
  if (!form) return { tools: 0, behaviour: 0, output: 0 };
  const behaviour =
    [form.temperature, form.topP, form.maxTokens, form.stopSequences].filter((s) => s.trim()).length +
    (form.thinking ? 1 : 0) +
    (form.promptCaching ? 1 : 0) +
    form.variables.filter((v) => v.name.trim()).length +
    form.fewShot.filter((e) => e.question.trim()).length;
  const output = (form.conversation !== "default" ? 1 : 0) + (form.outputSchema.trim() ? 1 : 0);
  return { tools: form.tools.length, behaviour, output };
}

/* ── validation ──────────────────────────────────────────────────────────── */

export type StrandsIssueKey =
  | "httpHosts"
  | "retrieve"
  | "temperature"
  | "topP"
  | "maxTokens"
  | "stopSequences"
  | "thinkingBudget"
  | "thinkingTemperature"
  | "thinkingTopP"
  | "thinkingMaxTokens"
  | "claudeBoth"
  | "windowSize"
  | "outputSchema"
  | "outputSchemaA2a"
  | "outputSchemaGuardrail"
  | "variables"
  | "fewShot";

export interface StrandsIssues {
  /** issue key → detail (the schema checker's message, a host, …; "" when none) */
  issues: Partial<Record<StrandsIssueKey, string>>;
}

interface IssueContext extends SpecContext {
  hasKbs: boolean;
  guardrail: boolean;
}

const outOf = (n: number | null, lo: number, hi: number, int = false) =>
  n !== null && (Number.isNaN(n) || n < lo || n > hi || (int && !Number.isInteger(n)));

export function strandsIssues(form: StrandsForm | undefined, ctx: IssueContext): StrandsIssues {
  const issues: StrandsIssues["issues"] = {};
  if (!form) return { issues };
  if (form.tools.includes("http_request")) {
    const hosts = splitHosts(form.httpHosts);
    const bad = hosts.find((h) => !HOST_RE.test(h));
    if (!hosts.length) issues.httpHosts = "";
    else if (bad) issues.httpHosts = bad;
  }
  if (form.tools.includes("retrieve") && !ctx.hasKbs) issues.retrieve = "";
  const temperature = num(form.temperature);
  const topP = num(form.topP);
  const maxTokens = num(form.maxTokens);
  if (outOf(temperature, 0, 1)) issues.temperature = "";
  if (outOf(topP, 0, 1)) issues.topP = "";
  if (outOf(maxTokens, 1, 131072, true)) issues.maxTokens = "";
  const stops = stopList(form.stopSequences);
  if (stops.length > 4 || stops.some((s) => s.length > 100)) issues.stopSequences = "";
  const claude = isClaudeModel(ctx.modelId);
  if (thinkingApplies(form, ctx)) {
    const budget = num(form.thinkingBudget);
    if (budget === null || outOf(budget, 1024, 131072, true)) issues.thinkingBudget = "";
    if (temperature !== null) issues.thinkingTemperature = "";
    if (topP !== null && topP < 0.95) issues.thinkingTopP = "";
    if (maxTokens !== null && budget !== null && maxTokens <= budget) issues.thinkingMaxTokens = "";
  }
  if (claude && temperature !== null && topP !== null) issues.claudeBoth = "";
  if (form.conversation !== "default" && outOf(num(form.windowSize) ?? DEFAULT_WINDOW, 2, 1000, true)) {
    issues.windowSize = "";
  }
  if (form.outputSchema.trim()) {
    if (ctx.protocol === "a2a") issues.outputSchemaA2a = "";
    else if (ctx.guardrail) issues.outputSchemaGuardrail = "";
    else {
      const problem = outputSchemaProblem(form.outputSchema);
      if (problem !== null) issues.outputSchema = problem;
    }
  }
  const names = form.variables.map((v) => v.name.trim()).filter(Boolean);
  const badName = names.find((n) => !PROMPT_VARIABLE_RE.test(n));
  if (badName !== undefined) issues.variables = badName;
  else if (new Set(names).size !== names.length || names.length > 20) issues.variables = "";
  if (form.fewShot.some((e) => (e.question.trim() || e.answer.trim()) && !(e.question.trim() && e.answer.trim())) ||
      form.fewShot.length > 10) {
    issues.fewShot = "";
  }
  return { issues };
}

export const strandsValid = (form: StrandsForm | undefined, ctx: IssueContext) =>
  Object.keys(strandsIssues(form, ctx).issues).length === 0 && Object.keys(sb2Issues(form, ctx)).length === 0;

/* ── SB-2 validation ──────────────────────────────────────────────────── */

export type Sb2IssueKey =
  | "agentToolName"
  | "agentToolDuplicate"
  | "agentToolDescription"
  | "agentToolCount"
  | "providerConnection"
  | "providerBaseUrl"
  | "providerConverse"
  | "approvalTools"
  | "maxToolCalls"
  | "maskArgs"
  | "retryAttempts"
  | "retryInitial"
  | "retryMax";

/** Issues about a value not typed yet: shown once the member tried to continue. */
export const SB2_KEYS_SHOWN_ON_CONTINUE: Sb2IssueKey[] = ["agentToolDescription", "providerConnection", "providerBaseUrl"];

const AGENT_TOOL_NAME_RE = /^[A-Za-z][A-Za-z0-9_]{0,63}$/;
const TOOL_PATTERN_RE = /^[A-Za-z0-9_.*?-]{1,128}$/;
const ARG_NAME_RE = /^[A-Za-z0-9_.-]{1,64}$/;
const CONNECTION_RE = /^[a-zA-Z0-9\-_]{1,128}$/;
const RESERVED_TOOL_NAMES = ["calculator", "current_utc_time", "kb_search", "kb_deep_search"];

function urlProblem(raw: string): boolean {
  try {
    const u = new URL(raw);
    return !["http:", "https:"].includes(u.protocol) || !u.hostname || Boolean(u.username || u.password || u.search || u.hash);
  } catch {
    return true;
  }
}

/** issue key → detail (a name, a pattern; "" when none). Mirrors `_check_sb2`. */
export function sb2Issues(form: StrandsForm | undefined, ctx: IssueContext): Partial<Record<Sb2IssueKey, string>> {
  const issues: Partial<Record<Sb2IssueKey, string>> = {};
  if (!form) return issues;
  const f = { ...emptyStrandsForm(), ...form };
  const names = f.agentTools.map((t) => t.toolName.trim());
  const bad = names.find((n) => !AGENT_TOOL_NAME_RE.test(n));
  if (bad !== undefined) issues.agentToolName = bad;
  const taken = new Set([...f.tools, ...RESERVED_TOOL_NAMES]);
  const dup = names.find((n, i) => names.indexOf(n) !== i || taken.has(n as StrandsTool));
  if (dup !== undefined && bad === undefined) issues.agentToolDuplicate = dup;
  if (f.agentTools.some((t) => !t.description.trim() || t.description.trim().length > 1000)) issues.agentToolDescription = "";
  if (f.agentTools.length > MAX_AGENT_TOOLS) issues.agentToolCount = String(MAX_AGENT_TOOLS);
  if (f.provider !== "bedrock" && providerApplies(ctx)) {
    if (!CONNECTION_RE.test(f.providerConnection.trim())) issues.providerConnection = "";
    const url = f.providerBaseUrl.trim();
    if (f.provider === "litellm" && !url) issues.providerBaseUrl = "";
    else if (url && urlProblem(url)) issues.providerBaseUrl = url;
    if (f.promptCaching || (f.thinking && isClaudeModel(ctx.modelId))) issues.providerConverse = "";
  }
  const badPattern = splitList(f.approvalTools).find((p) => !TOOL_PATTERN_RE.test(p));
  if (badPattern !== undefined) issues.approvalTools = badPattern;
  if (outOf(num(f.maxToolCalls), 1, 500, true)) issues.maxToolCalls = "";
  const badArg = splitList(f.maskArgs).find((a) => !ARG_NAME_RE.test(a));
  if (badArg !== undefined) issues.maskArgs = badArg;
  if (f.retry) {
    const attempts = num(f.retryAttempts);
    const initial = num(f.retryInitial);
    const max = num(f.retryMax);
    if (attempts === null || outOf(attempts, 1, 10, true)) issues.retryAttempts = "";
    if (initial === null || outOf(initial, 1, 60, true)) issues.retryInitial = "";
    if (max === null || outOf(max, 1, 600, true)) issues.retryMax = "";
    else if (initial !== null && !Number.isNaN(initial) && max < initial) issues.retryMax = "order";
  }
  return issues;
}

/** The JSON Schema subset the generated code turns into a pydantic model; null when
 *  fine, else a short reason. Mirrors `validate_output_schema` on the backend. */
export function outputSchemaProblem(text: string): string | null {
  if (text.length > SCHEMA_MAX_CHARS) return `longer than ${SCHEMA_MAX_CHARS} characters`;
  let schema: unknown;
  try {
    schema = JSON.parse(text);
  } catch (e) {
    return e instanceof Error ? e.message : "not JSON";
  }
  try {
    checkObject(schema, "$", 1);
    return null;
  } catch (e) {
    return e instanceof Error ? e.message : String(e);
  }
}

type Json = Record<string, unknown>;
const isObj = (v: unknown): v is Json => typeof v === "object" && v !== null && !Array.isArray(v);

function checkObject(node: unknown, path: string, depth: number) {
  if (!isObj(node) || node.type !== "object") throw new Error(`${path} must be "type": "object"`);
  if (depth > SCHEMA_MAX_DEPTH) throw new Error(`nests deeper than ${SCHEMA_MAX_DEPTH} objects`);
  const props = node.properties;
  if (!isObj(props) || Object.keys(props).length === 0) throw new Error(`${path} needs at least one property`);
  if (Object.keys(props).length > 50) throw new Error(`${path} has more than 50 properties`);
  for (const [name, child] of Object.entries(props)) {
    if (!SCHEMA_PROPERTY_RE.test(name)) throw new Error(`property "${name}" must be a letter then letters, digits or _`);
    checkValue(child, `${path}.${name}`, depth);
  }
  const required = node.required ?? [];
  if (!Array.isArray(required) || required.some((r) => typeof r !== "string" || !(r in props))) {
    throw new Error(`${path}.required must list declared properties only`);
  }
}

function checkValue(node: unknown, path: string, depth: number) {
  if (!isObj(node)) throw new Error(`${path} must be a schema object`);
  const kind = node.type;
  if (kind === "object") return checkObject(node, path, depth + 1);
  if (kind === "array") {
    const items = node.items;
    if (!isObj(items) || items.type === "array") throw new Error(`${path}.items must be a scalar or object schema`);
    return checkValue(items, `${path}[]`, depth);
  }
  if (typeof kind !== "string" || !SCHEMA_SCALARS.has(kind)) {
    throw new Error(`${path}.type must be string, integer, number, boolean, array or object`);
  }
  if ("enum" in node) {
    const e = node.enum;
    if (!Array.isArray(e) || e.length === 0 || e.length > 100 || e.some((v) => !["string", "number", "boolean"].includes(typeof v))) {
      throw new Error(`${path}.enum must be a non-empty list of scalars`);
    }
  }
}

/** Top-level property names of a valid schema (the editor's readout). */
export function schemaFields(text: string): string[] {
  try {
    const schema = JSON.parse(text) as Json;
    return isObj(schema.properties) ? Object.keys(schema.properties) : [];
  } catch {
    return [];
  }
}

export const EXAMPLE_OUTPUT_SCHEMA = JSON.stringify(
  {
    type: "object",
    title: "TicketTriage",
    properties: {
      category: { type: "string", enum: ["billing", "bug", "question"], description: "What the ticket is about" },
      priority: { type: "integer", description: "1 (urgent) to 4 (low)" },
      summary: { type: "string" },
    },
    required: ["category", "priority"],
  },
  null,
  2,
);
