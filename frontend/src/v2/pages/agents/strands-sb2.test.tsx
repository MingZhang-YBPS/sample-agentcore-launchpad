import { renderToStaticMarkup } from "react-dom/server";
import { beforeAll, describe, expect, it, vi } from "vitest";

import i18n from "../../../i18n";
import { emptyAgentForm } from "../../../lib/agent-spec";
import { emptyStrandsForm, type StrandsForm } from "../../../lib/strands-builder";
import { ModelParamsCard, StrandsToolPicker } from "./StrandsSections";

const form = (over: Partial<StrandsForm>) => ({
  ...emptyAgentForm("zip_runtime"),
  name: "caller",
  systemPrompt: "Be brief.",
  modelId: "claude-sonnet-4-5",
  modelSource: "bedrock" as const,
  strands: { ...emptyStrandsForm(), ...over },
});
const set = () => undefined;

beforeAll(async () => {
  vi.stubGlobal("document", { documentElement: {} });
  await i18n.changeLanguage("en");
});

describe("SB-2 wizard panels", () => {
  it("draws the provider's key path and the retry ladder in the model card", () => {
    const html = renderToStaticMarkup(
      <ModelParamsCard form={form({ provider: "anthropic", providerConnection: "anthropic-key", retry: true })} set={set} touched={false} />,
    );
    expect(html).toContain('data-testid="sb-model-provider"');
    expect(html).toContain("anthropic-key");
    expect(html).toContain("GetResourceApiKey");
    expect(html).toContain("never stored in the agent&#x27;s spec");
    // 6 attempts → 5 rungs, 4 s doubling
    expect(html.match(/class="sb2-rung"/g)).toHaveLength(5);
    expect(html).toContain("64s");
    expect(html).toContain("124 s");
  });

  it("keeps Bedrock quiet and says so for A2A", () => {
    const quiet = renderToStaticMarkup(<ModelParamsCard form={form({})} set={set} touched={false} />);
    expect(quiet).not.toContain("GetResourceApiKey");
    expect(quiet).not.toContain("sb2-rung");
    const a2a = renderToStaticMarkup(
      <ModelParamsCard form={{ ...form({ provider: "litellm" }), protocol: "a2a" }} set={set} touched={false} />,
    );
    expect(a2a).toContain("HTTP agents on Bedrock only");
  });

  it("shows picked agents as relays and the masked log sample", () => {
    const html = renderToStaticMarkup(
      <StrandsToolPicker
        form={form({
          agentTools: [{ agentId: "a1", label: "Billing desk", toolName: "ask_billing", description: "Billing.",
                         protocol: "a2a", sharedFrom: null }],
          approvalTools: "send_email",
          logToolCalls: true,
          maskArgs: "card_number",
        })}
        set={set}
        touched={false}
      />,
    );
    expect(html).toContain('data-testid="sb-agent-tool-a1"');
    expect(html).toContain('value="ask_billing"');
    expect(html).toContain(">A2A<");
    expect(html).toContain("[launchpad-tool]");
    expect(html).toContain("&quot;card_number&quot;:&quot;***&quot;");
    expect(html).toContain('class="sb-chip mono sb2-pause"');
  });
});
