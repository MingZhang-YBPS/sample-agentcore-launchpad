import { renderToStaticMarkup } from "react-dom/server";
import { beforeAll, describe, expect, it, vi } from "vitest";

import i18n from "../../../i18n";
import type { GovernancePolicyTestResult } from "../../../lib/api";
import { PolicyTestResultView } from "./PolicyTest";

const BASE: GovernancePolicyTestResult = {
  principal: "demo@hr-analyst",
  tool: "hr-database___create_payout",
  outcome: "ALLOW",
  detail: "{'content': []}",
  policy_id: null,
  decision_id: null,
  recorded: false,
  enforced: false,
  policy_engine_id: null,
  note: "Not enforced",
};

beforeAll(async () => {
  vi.stubGlobal("document", { documentElement: {} });
  await i18n.changeLanguage("en");
});

describe("policy-test result", () => {
  it("says an ALLOW without a Policy Engine was not enforced and offers to attach one", () => {
    const html = renderToStaticMarkup(<PolicyTestResultView result={BASE} onAttachEngine={() => undefined} />);
    expect(html).toContain("Not enforced — no Policy Engine attached");
    expect(html).toContain("v2-governance-policy-test-not-enforced");
    expect(html).toContain("Attach a Policy Engine");
    expect(html).toContain("Not recorded");
  });

  it("an enforced decision names its engine and carries no warning", () => {
    const html = renderToStaticMarkup(
      <PolicyTestResultView
        result={{ ...BASE, enforced: true, recorded: true, decision_id: "d-1", policy_engine_id: "launchpad_pe-1", note: null }}
        onAttachEngine={() => undefined}
      />,
    );
    expect(html).not.toContain("v2-governance-policy-test-not-enforced");
    expect(html).toContain("launchpad_pe-1");
    expect(html).toContain("Recorded");
  });

  it("reads in Chinese too", async () => {
    await i18n.changeLanguage("zh-CN");
    const html = renderToStaticMarkup(<PolicyTestResultView result={BASE} />);
    expect(html).toContain("未执行策略——未挂载 Policy Engine");
    expect(html).not.toContain("v2-governance-policy-test-attach-engine");
    await i18n.changeLanguage("en");
  });
});
