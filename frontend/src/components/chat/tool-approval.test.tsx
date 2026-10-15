import { renderToStaticMarkup } from "react-dom/server";
import { beforeAll, describe, expect, it, vi } from "vitest";

import i18n from "../../i18n";
import {
  approvalActionable,
  approvalPhase,
  approvalPrompt,
  decideApproval,
  liveApproval,
  maskedCount,
  maskedInputPieces,
  restoredApproval,
  type ToolApproval,
  approvedToolsField,
} from "../../lib/tool-approval";
import { ToolApprovalCard } from "./ToolApprovalCard";

beforeAll(async () => {
  vi.stubGlobal("document", { documentElement: {} });
  await i18n.changeLanguage("en");
});

const paused = (over: Partial<ToolApproval> = {}): ToolApproval => ({
  tool: "send_email", toolUseId: "tu-1", input: { to: "a@example.com", token: "***" }, ...over,
});

describe("tool approval state", () => {
  it("reads an approval_required payload, keeping only an object input", () => {
    expect(liveApproval({ tool: "send_email", tool_use_id: "tu-1", input: { to: "x" } })).toEqual({
      tool: "send_email", toolUseId: "tu-1", input: { to: "x" },
    });
    expect(liveApproval({ tool: "t", input: ["no"] as never }, true)).toEqual({
      tool: "t", toolUseId: "", input: {}, refused: true,
    });
    expect(restoredApproval({ name: "send_email" }).restored).toBe(true);
    // the ledger keeps the masked input as JSON in the row's text
    expect(restoredApproval({ name: "send_email", text: '{"to":"***"}' }).input).toEqual({ to: "***" });
    expect(restoredApproval({ name: "send_email", text: "not json" }).input).toEqual({});
  });

  it("is actionable only on the latest turn and until answered", () => {
    const msgs = [
      { kind: "user" },
      { kind: "approval", approval: paused() },
    ];
    expect(approvalActionable(msgs, 1)).toBe(true);
    expect(approvalActionable([...msgs, { kind: "user" }], 1)).toBe(false);
    expect(approvalActionable(decideApproval(msgs, 1, "declined"), 1)).toBe(false);
    expect(approvalActionable([{ kind: "approval", approval: paused({ refused: true }) }], 0)).toBe(false);
    expect(approvalActionable([{ kind: "approval", approval: paused({ restored: true }) }], 0)).toBe(false);
  });

  it("maps a card to its phase", () => {
    expect(approvalPhase(paused(), true)).toBe("pending");
    expect(approvalPhase(paused(), false)).toBe("expired");
    expect(approvalPhase(paused({ decision: "approved" }), false)).toBe("approved");
    expect(approvalPhase(paused({ refused: true }), true)).toBe("refused");
    expect(approvalPhase(paused({ restored: true }), true)).toBe("expired");
  });

  it("sends approved_tools only when something was approved", () => {
    expect(approvedToolsField(undefined)).toBeUndefined();
    expect(approvedToolsField([""])).toBeUndefined();
    expect(approvedToolsField(["send_email", "send_email"])).toEqual(["send_email"]);
    expect(approvalPrompt(i18n.t.bind(i18n), "send_email")).toBe("Approved — go ahead with send_email.");
  });

  it("splits the masked input so each masked value is its own piece", () => {
    const pieces = maskedInputPieces({ a: "***", nested: { b: "***", c: 1 } });
    expect(pieces.filter((p) => p.masked)).toHaveLength(2);
    expect(pieces.map((p) => p.text).join("")).toBe(JSON.stringify({ a: "***", nested: { b: "***", c: 1 } }, null, 2));
    expect(maskedCount({ a: 1 })).toBe(0);
  });
});

describe("ToolApprovalCard", () => {
  it("shows the tool, the masked input and both buttons while pending", () => {
    const html = renderToStaticMarkup(<ToolApprovalCard approval={paused()} phase="pending" />);
    expect(html).toContain('data-phase="pending"');
    expect(html).toContain("send_email");
    expect(html).toContain("a@example.com");
    expect(html).toContain("lp-approval-redact");
    expect(html).toContain("1 argument masked");
    expect(html).toContain('data-testid="tool-approval-approve"');
    expect(html).toContain('data-testid="tool-approval-decline"');
  });

  it("replaces the buttons with a stamp once answered, and shows none when refused", () => {
    const approved = renderToStaticMarkup(<ToolApprovalCard approval={paused({ decision: "approved" })} phase="approved" />);
    expect(approved).not.toContain("tool-approval-approve");
    expect(approved).toContain("may run once");
    const refused = renderToStaticMarkup(<ToolApprovalCard approval={paused({ refused: true })} phase="refused" />);
    expect(refused).toContain("Approval unavailable");
    expect(refused).not.toContain("tool-approval-approve");
  });
});
