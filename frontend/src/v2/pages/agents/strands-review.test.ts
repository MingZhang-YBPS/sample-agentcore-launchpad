import { beforeAll, describe, expect, it, vi } from "vitest";

import i18n from "../../../i18n";
import { emptyAgentForm } from "../../../lib/agent-spec";
import { emptyStrandsForm } from "../../../lib/strands-builder";
import { strandsReviewItems, strandsReviewSpec } from "./strandsReview";

beforeAll(async () => {
  vi.stubGlobal("document", { documentElement: {} });
  await i18n.changeLanguage("en");
});

const t = i18n.t.bind(i18n);

describe("strandsReviewItems", () => {
  it("lists nothing for an agent that uses no builder option", () => {
    expect(strandsReviewItems({}, t)).toEqual([]);
  });

  it("lists every set option, read off the spec", () => {
    const items = strandsReviewItems({
      strands_tools: ["http_request"],
      strands_tool_config: { http_request: { allowed_hosts: ["api.example.com", "*.example.org"] } },
      model_params: { temperature: 0.2, max_tokens: 2048, stop_sequences: ["END"], thinking_budget_tokens: 4096, prompt_caching: true },
      conversation: { strategy: "summarizing", window_size: 12, summary_model: "us.amazon.nova-lite-v1:0" },
      output_schema: { type: "object", properties: { city: { type: "string" }, temp: { type: "number" } } },
      few_shot_examples: [{ question: "q", answer: "a" }],
      prompt_variables: { tone: "formal" },
    }, t);
    const byLabel = Object.fromEntries(items.map((i) => [i.label, i.value]));
    expect(byLabel["Allowed hosts for http_request"]).toBe("api.example.com, *.example.org");
    expect(byLabel["Inference parameters"]).toBe(
      "temperature 0.2 · max_tokens 2048 · 1 stop sequence · thinking 4096 tokens · Prompt caching",
    );
    expect(byLabel["Conversation history"]).toBe("Summarizing · 12 messages kept · us.amazon.nova-lite-v1:0");
    expect(byLabel["Structured output (JSON Schema)"]).toBe("2 fields: city, temp");
    expect(byLabel["Template variables"]).toBe("{{tone}}");
    expect(byLabel["Few-shot examples"]).toBe("1 example");
  });
});

describe("strandsReviewSpec", () => {
  it("reads the form the way the spec is built, and a half-typed schema as none", () => {
    const form = { ...emptyAgentForm("zip_runtime"), strands: { ...emptyStrandsForm(), tools: ["calculator" as const] } };
    expect(strandsReviewSpec(form).strands_tools).toEqual(["calculator"]);
    const broken = { ...form, strands: { ...form.strands, outputSchema: "{ not json" } };
    expect(strandsReviewSpec(broken)).toEqual({});
  });
});
