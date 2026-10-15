import type { TFunction } from "i18next";

import type { AgentForm } from "../../../lib/agent-spec";
import { type StrandsSpecFields, strandsSpec } from "../../../lib/strands-builder";

/**
 * The review step's lines for the Strands builder options (SB-1): read off the spec
 * that will be posted, so what the review lists is exactly what is deployed. Only set
 * options get a line — an agent that uses none reviews as it always did. The picked
 * tools themselves join the review's Tools line (`strands_tools`).
 */
export function strandsReviewItems(spec: StrandsSpecFields, t: TFunction): { label: string; value: string }[] {
  const items: { label: string; value: string }[] = [];
  const hosts = spec.strands_tool_config?.http_request?.allowed_hosts;
  if (hosts?.length) items.push({ label: t("strandsBuilder.tools.hostsLabel"), value: hosts.join(", ") });
  const p = spec.model_params;
  if (p) {
    const parts = [
      p.temperature != null ? `temperature ${p.temperature}` : null,
      p.top_p != null ? `top_p ${p.top_p}` : null,
      p.max_tokens != null ? `max_tokens ${p.max_tokens}` : null,
      p.stop_sequences?.length ? t("strandsBuilder.review.stops", { count: p.stop_sequences.length }) : null,
      p.thinking_budget_tokens != null ? t("strandsBuilder.review.thinking", { budget: p.thinking_budget_tokens }) : null,
      p.prompt_caching ? t("strandsBuilder.params.caching") : null,
    ].filter((x): x is string => !!x);
    if (parts.length) items.push({ label: t("strandsBuilder.params.title"), value: parts.join(" · ") });
  }
  const c = spec.conversation;
  if (c?.strategy) {
    items.push({
      label: t("strandsBuilder.output.conversation"),
      value: [
        t(`strandsBuilder.output.strategy.${c.strategy}`),
        t("strandsBuilder.review.window", { count: c.window_size ?? 40 }),
        c.summary_model ? c.summary_model : null,
      ].filter(Boolean).join(" · "),
    });
  }
  if (spec.output_schema) {
    const props = spec.output_schema.properties;
    const fields = props && typeof props === "object" ? Object.keys(props) : [];
    items.push({
      label: t("strandsBuilder.output.schema"),
      value: t("strandsBuilder.review.schema", { count: fields.length, fields: fields.join(", ") }),
    });
  }
  const vars = Object.keys(spec.prompt_variables ?? {});
  if (vars.length) items.push({ label: t("strandsBuilder.prompt.variables"), value: vars.map((v) => `{{${v}}}`).join(", ") });
  if (spec.few_shot_examples?.length) {
    items.push({
      label: t("strandsBuilder.prompt.fewShot"),
      value: t("strandsBuilder.review.examples", { count: spec.few_shot_examples.length }),
    });
  }
  return items;
}

/** The spec slice the review reads; a form the checks have not passed yet reads as none. */
export function strandsReviewSpec(form: AgentForm): StrandsSpecFields {
  try {
    return strandsSpec(form.strands, { protocol: form.protocol, modelId: form.modelId.trim(), modelSource: form.modelSource });
  } catch {
    return {};
  }
}
