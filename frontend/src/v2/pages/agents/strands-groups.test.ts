import { describe, expect, it } from "vitest";

import { emptyAgentForm } from "../../../lib/agent-spec";
import { emptyStrandsForm } from "../../../lib/strands-builder";
import { groupIssues, strandsReadouts } from "./strandsGroups";

const withStrands = (patch: Partial<ReturnType<typeof emptyStrandsForm>>) => ({
  ...emptyAgentForm(),
  strands: { ...emptyStrandsForm(), ...patch },
});

describe("strandsGroups SB-2", () => {
  it("adds agents-as-tools and the approval list to the Tools readout", () => {
    const form = withStrands({
      agentTools: [{ agentId: "a1", label: "Billing", toolName: "ask_billing", description: "Billing questions", protocol: "http", sharedFrom: null }],
      approvalTools: "send_email, delete_*",
    });
    const tools = strandsReadouts(form).tools ?? "";
    expect(tools).toContain("agents 1");
    expect(tools).toContain("approve 2");
  });

  it("adds the provider and retry to the Model behaviour readout", () => {
    const form = withStrands({ provider: "anthropic", retry: true, retryAttempts: "4" });
    expect(strandsReadouts(form).behaviour).toBe("anthropic · retry 4");
    expect(strandsReadouts(withStrands({})).behaviour).toBe("");
  });

  it("flags the group that holds an SB-2 problem", () => {
    expect(groupIssues(withStrands({ maxToolCalls: "-3" }))).toContain("tools");
    expect(groupIssues(withStrands({ retry: true, retryAttempts: "99" }))).toContain("behaviour");
    expect(groupIssues(withStrands({}))).toEqual([]);
  });
});

describe("strandsGroups SB-3", () => {
  const tool = { name: "parse_invoice", description: "Parse an invoice", code: "from strands import tool\n", requirements: "", execution: "runtime" as const };

  it("adds sandboxed tools and the custom tool count to the Tools readout", () => {
    const tools = strandsReadouts(withStrands({ sandboxTools: ["python_repl"], customTools: [tool] })).tools ?? "";
    expect(tools).toContain("python_repl");
    expect(tools).toContain("py 1");
  });

  it("flags the Tools group for a custom tool problem", () => {
    expect(groupIssues(withStrands({ customTools: [{ ...tool, name: "Bad Name" }] }))).toContain("tools");
    expect(groupIssues(withStrands({ customTools: [tool] }))).toEqual([]);
  });
});
