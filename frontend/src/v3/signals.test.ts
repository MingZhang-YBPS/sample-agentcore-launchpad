import { describe, expect, it } from "vitest";

import { score } from "./score";
import {
  agentSignal,
  attentionFor,
  conversationSignal,
  gateSignal,
  kbSignal,
  registrySignal,
  releaseSignal,
  resourceSignal,
  sortAttention,
} from "./signals";

describe("signals map every status onto one meaning", () => {
  it("an agent is ok only when active", () => {
    expect(agentSignal({ status: "active" })).toBe("ok");
    expect(agentSignal({ status: "deploying" })).toBe("wait");
    expect(agentSignal({ status: "failed" })).toBe("act");
    expect(agentSignal({ status: "draft" })).toBe("off");
  });

  it("INVALID waits (the evidence needs fixing) rather than reading as a failure", () => {
    expect(gateSignal("PASS")).toBe("ok");
    expect(gateSignal("BLOCKED")).toBe("act");
    expect(gateSignal("INVALID")).toBe("wait");
    expect(gateSignal(null)).toBe("off");
  });

  it("a release needing a decision waits, a refused one needs you", () => {
    expect(releaseSignal("released")).toBe("ok");
    expect(releaseSignal("pending")).toBe("wait");
    expect(releaseSignal("blocked")).toBe("act");
    expect(releaseSignal("superseded")).toBe("off");
  });
});

describe("the needs-you queue", () => {
  const agent = { id: "a1", name: "shop-cs", display_name: null, status: "active" as const, updated_at: "2026-10-07T00:00:00Z" };

  it("ranks a failed deploy above a blocked gate above a decision waiting", () => {
    const failed = attentionFor({ ...agent, id: "f", status: "failed" }, null);
    const blocked = attentionFor({ ...agent, id: "b" }, { decision: "blocked", created_at: "x",
      gate_report: { verdict: "BLOCKED" } as never });
    const waiting = attentionFor({ ...agent, id: "w" }, { decision: "pending", created_at: "x", gate_report: {} });
    const order = sortAttention([...waiting, ...blocked, ...failed]).map((a) => a.kind);
    expect(order).toEqual(["deploy_failed", "gate_blocked", "release_waiting"]);
  });

  it("a quiet agent asks for nothing", () => {
    expect(attentionFor(agent, null)).toEqual([]);
  });

  it("INVALID is its own item — fix the evidence, not the agent", () => {
    const [item] = attentionFor(agent, { decision: "invalid", created_at: "x", gate_report: { verdict: "INVALID" } as never });
    expect(item.kind).toBe("gate_invalid");
    expect(item.signal).toBe("wait");
  });
});

describe("command palette scoring", () => {
  it("finds a subsequence and prefers word starts", () => {
    expect(score("shcs", "shop-cs")).toBeGreaterThan(0);
    expect(score("cs", "shop-cs")).toBeGreaterThan(score("cs", "discs"));
    expect(score("xyz", "shop-cs")).toBe(0);
  });

  it("a substring beats a scattered match", () => {
    expect(score("shop", "shop-cs")).toBeGreaterThan(score("shcs", "shop-cs"));
  });

  it("an empty query matches everything", () => {
    expect(score("", "anything")).toBe(1);
  });
});

describe("module signals", () => {
  it("maps registry states", () => {
    expect(registrySignal("APPROVED")).toBe("ok");
    expect(registrySignal("PENDING_APPROVAL")).toBe("wait");
    expect(registrySignal("REJECTED")).toBe("act");
    expect(registrySignal("DRAFT")).toBe("off");
    expect(registrySignal("DEPRECATED")).toBe("off");
  });

  it("maps knowledge-base and ingestion states", () => {
    expect(kbSignal("ACTIVE")).toBe("ok");
    expect(kbSignal("creating")).toBe("wait");
    expect(kbSignal("FAILED")).toBe("act");
    expect(resourceSignal("COMPLETE")).toBe("ok");
    expect(resourceSignal("IN_PROGRESS")).toBe("wait");
    expect(resourceSignal("FAILED")).toBe("act");
    expect(resourceSignal("whatever")).toBe("off");
  });

  it("puts a streaming conversation ahead of its proposal", () => {
    expect(conversationSignal({ proposal_status: "approved", turn_in_progress: 3 })).toBe("info");
    expect(conversationSignal({ proposal_status: "draft", turn_in_progress: null })).toBe("wait");
    expect(conversationSignal({ proposal_status: "approved", turn_in_progress: null })).toBe("ok");
    expect(conversationSignal({ proposal_status: "invalid", turn_in_progress: null })).toBe("act");
    expect(conversationSignal({ proposal_status: null, turn_in_progress: null })).toBe("info");
  });
});
