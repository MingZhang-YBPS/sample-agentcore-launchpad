import { type AgentForm, strandsContext } from "../../../lib/agent-spec";
import {
  emptyStrandsForm,
  isClaudeModel,
  type Sb2IssueKey,
  sb2Issues,
  sb3Issues,
  splitList,
  type StrandsForm,
  type StrandsIssueKey,
  strandsIssues,
} from "../../../lib/strands-builder";

/** The Strands configure step's groups and what each holds (StrandsSections). */

export type StrandsGroupKey = "basics" | "tools" | "behaviour" | "output" | "advanced";
export const STRANDS_GROUPS: StrandsGroupKey[] = ["basics", "tools", "behaviour", "output", "advanced"];

/** Which group an issue opens when the member tries to continue. */
export const ISSUE_GROUP: Record<StrandsIssueKey, StrandsGroupKey> = {
  httpHosts: "tools",
  retrieve: "tools",
  temperature: "behaviour",
  topP: "behaviour",
  maxTokens: "behaviour",
  stopSequences: "behaviour",
  thinkingBudget: "behaviour",
  thinkingTemperature: "behaviour",
  thinkingTopP: "behaviour",
  thinkingMaxTokens: "behaviour",
  claudeBoth: "behaviour",
  variables: "behaviour",
  fewShot: "behaviour",
  windowSize: "output",
  outputSchema: "output",
  outputSchemaA2a: "output",
  outputSchemaGuardrail: "output",
};

/** Which group an SB-2 issue opens: agents-as-tools and tool-call guards sit in
 *  the Tools group, the model provider and throttling retry in Model behaviour. */
export const SB2_ISSUE_GROUP: Record<Sb2IssueKey, StrandsGroupKey> = {
  agentToolName: "tools",
  agentToolDuplicate: "tools",
  agentToolDescription: "tools",
  agentToolCount: "tools",
  approvalTools: "tools",
  maxToolCalls: "tools",
  maskArgs: "tools",
  providerConnection: "behaviour",
  providerBaseUrl: "behaviour",
  providerConverse: "behaviour",
  retryAttempts: "behaviour",
  retryInitial: "behaviour",
  retryMax: "behaviour",
};

/** Issues about a value not typed yet: shown once the member tried to continue. */
export const ISSUE_KEYS_SHOWN_ON_CONTINUE: StrandsIssueKey[] = ["httpHosts", "retrieve", "fewShot"];

export const sb = (form: AgentForm): StrandsForm => form.strands ?? emptyStrandsForm();

export const groupIssues = (form: AgentForm): StrandsGroupKey[] => {
  const ctx = strandsContext(form);
  const { issues } = strandsIssues(form.strands, ctx);
  return [...new Set([
    ...(Object.keys(issues) as StrandsIssueKey[]).map((k) => ISSUE_GROUP[k]),
    ...(Object.keys(sb2Issues(form.strands, ctx)) as Sb2IssueKey[]).map((k) => SB2_ISSUE_GROUP[k]),
    // SB-3: custom tools and sandboxed tools both live in the Tools group
    ...(Object.keys(sb3Issues(form.strands)).length ? ["tools" as const] : []),
  ])];
};


/** Mono readouts for the closed group heads: what each group holds. */
export function strandsReadouts(form: AgentForm): Partial<Record<StrandsGroupKey, string>> {
  const s = sb(form);
  const behaviour = [
    s.temperature.trim() && `temp ${s.temperature.trim()}`,
    s.topP.trim() && `top_p ${s.topP.trim()}`,
    s.maxTokens.trim() && `max ${s.maxTokens.trim()}`,
    s.thinking && isClaudeModel(form.modelId) && "thinking",
    s.promptCaching && "cache",
    s.variables.some((v) => v.name.trim()) && `{{${s.variables.filter((v) => v.name.trim()).length}}}`,
    s.fewShot.length > 0 && `${s.fewShot.length} shot`,
    s.provider !== "bedrock" && s.provider,
    s.retry && `retry ${s.retryAttempts.trim() || "?"}`,
  ].filter(Boolean);
  const output = [
    s.conversation !== "default" && `${s.conversation === "summarizing" ? "summary" : "window"} ${s.windowSize.trim()}`,
    s.outputSchema.trim() && "schema",
  ].filter(Boolean);
  const tools = [
    ...s.tools,
    ...s.sandboxTools,
    ...form.toolkits,
    form.selectedGateway.length > 0 && `gw ${form.selectedGateway.length}`,
    form.selectedKbs.length > 0 && `kb ${form.selectedKbs.length}`,
    s.agentTools.length > 0 && `agents ${s.agentTools.length}`,
    splitList(s.approvalTools).length > 0 && `approve ${splitList(s.approvalTools).length}`,
    s.customTools.length > 0 && `py ${s.customTools.length}`,
  ].filter(Boolean);
  return {
    basics: [form.modelId.split(".").slice(-2).join("."), form.protocol === "a2a" ? "a2a" : ""].filter(Boolean).join(" · "),
    tools: tools.length > 3 ? `${tools.slice(0, 3).join(" · ")} +${tools.length - 3}` : tools.join(" · "),
    behaviour: behaviour.join(" · "),
    output: output.join(" · "),
  };
}
