import { type AgentForm, strandsContext } from "../../../lib/agent-spec";
import {
  emptyStrandsForm,
  isClaudeModel,
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

/** Issues about a value not typed yet: shown once the member tried to continue. */
export const ISSUE_KEYS_SHOWN_ON_CONTINUE: StrandsIssueKey[] = ["httpHosts", "retrieve", "fewShot"];

export const sb = (form: AgentForm): StrandsForm => form.strands ?? emptyStrandsForm();

export const groupIssues = (form: AgentForm): StrandsGroupKey[] => {
  const { issues } = strandsIssues(form.strands, strandsContext(form));
  return [...new Set((Object.keys(issues) as StrandsIssueKey[]).map((k) => ISSUE_GROUP[k]))];
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
  ].filter(Boolean);
  const output = [
    s.conversation !== "default" && `${s.conversation === "summarizing" ? "summary" : "window"} ${s.windowSize.trim()}`,
    s.outputSchema.trim() && "schema",
  ].filter(Boolean);
  const tools = [
    ...s.tools,
    ...form.toolkits,
    form.selectedGateway.length > 0 && `gw ${form.selectedGateway.length}`,
    form.selectedKbs.length > 0 && `kb ${form.selectedKbs.length}`,
  ].filter(Boolean);
  return {
    basics: [form.modelId.split(".").slice(-2).join("."), form.protocol === "a2a" ? "a2a" : ""].filter(Boolean).join(" · "),
    tools: tools.length > 3 ? `${tools.slice(0, 3).join(" · ")} +${tools.length - 3}` : tools.join(" · "),
    behaviour: behaviour.join(" · "),
    output: output.join(" · "),
  };
}
