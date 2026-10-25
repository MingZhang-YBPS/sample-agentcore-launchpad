import { afterEach, describe, expect, it, vi } from "vitest";

import { agentNetworkPath, getAgentNetwork, networkChoices, RUNTIME_NETWORK_PURPOSES } from "./agent-network";
import type { NetworkProfile } from "./projects";

const profile = (id: string, over: Partial<NetworkProfile> = {}): NetworkProfile => ({
  id,
  workspace_id: "default",
  name: id,
  vpc_id: "vpc-1",
  subnet_ids: ["subnet-a", "subnet-b"],
  security_group_ids: ["sg-1"],
  purpose: "agents",
  internet: "none",
  reachable: [],
  status: "ok",
  check_results: null,
  checked_at: null,
  legacy: false,
  created_by: null,
  created_at: null,
  updated_at: null,
  archived_at: null,
  allowed: true,
  is_default: false,
  ...over,
});

describe("agent network API", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("encodes the agent id and reads the route", async () => {
    expect(agentNetworkPath("a/b")).toBe("/api/agents/a%2Fb/network");
    const body = { agent_id: "a1", mode: "PUBLIC", profile: null, vpc: null, enis: null };
    const fetchMock = vi.fn(async () => new Response(JSON.stringify(body), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    expect(await getAgentNetwork("a1")).toEqual(body);
    expect((fetchMock.mock.calls[0] as unknown as [string])[0]).toBe("/api/agents/a1/network");
  });
});

describe("networkChoices", () => {
  it("offers the allowed live profiles, default first, blocked ones disabled", () => {
    const choices = networkChoices([
      profile("np-b", { name: "beta", status: "warn" }),
      profile("np-x", { name: "other", allowed: false }),
      profile("np-a", { name: "alpha", status: "blocked" }),
      profile("np-d", { name: "zeta", is_default: true }),
      profile("np-z", { name: "gone", archived_at: "2026-10-01T00:00:00Z" }),
    ]);
    expect(choices.map((c) => c.profile.id)).toEqual(["np-d", "np-a", "np-b"]);
    expect(choices.find((c) => c.profile.id === "np-a")?.disabled).toBe(true);
    expect(choices.find((c) => c.profile.id === "np-b")?.caution).toBe(true);
    expect(choices.find((c) => c.profile.id === "np-d")?.caution).toBe(false);
  });

  it("keeps the agent's current profile listed, even when not allowed or blocked", () => {
    const choices = networkChoices([profile("np-x", { allowed: false, status: "blocked" })], "np-x");
    expect(choices).toHaveLength(1);
    expect(choices[0].disabled).toBe(false);
  });

  it("offers runtimes only agents / both profiles, keeping the current one", () => {
    const list = [
      profile("np-a", { purpose: "agents" }),
      profile("np-t", { purpose: "tools" }),
      profile("np-b", { purpose: "both" }),
    ];
    expect(networkChoices(list, "", RUNTIME_NETWORK_PURPOSES).map((c) => c.profile.id)).toEqual(["np-a", "np-b"]);
    expect(networkChoices(list, "np-t", RUNTIME_NETWORK_PURPOSES).map((c) => c.profile.id)).toEqual(["np-a", "np-b", "np-t"]);
    expect(networkChoices(list).map((c) => c.profile.id)).toEqual(["np-a", "np-b", "np-t"]);
  });
});
