import { renderToStaticMarkup } from "react-dom/server";
import { MemoryRouter } from "react-router-dom";
import { beforeAll, describe, expect, it, vi } from "vitest";

import i18n from "../../../i18n";
import type { AgentInfo } from "../../../lib/api";
import type { AgentNetwork } from "../../../lib/api/agent-network";
import type { NetworkProfile } from "../../../lib/api/networkProfiles";
import { hostedGroups } from "../../nav";
import { NetworkCardView } from "../agent/NetworkCard";
import { azsOf } from "./model";
import { ProfileDetail } from "./ProfileDetail";

const agent = { id: "a1", name: "lab", method: "zip_runtime", status: "active", spec: {} } as unknown as AgentInfo;

const profile: NetworkProfile = {
  id: "np-0123456789ab",
  workspace_id: "default",
  name: "core-vpc",
  vpc_id: "vpc-0123456789abcdef0",
  subnet_ids: ["subnet-a", "subnet-b"],
  security_group_ids: ["sg-1"],
  purpose: "both",
  internet: "nat",
  reachable: [],
  status: "warn",
  check_results: [
    { check: "subnets.azs", severity: "ok", message: "subnets span 2 Availability Zones", details: { azs: ["usw2-az1", "usw2-az2"] } },
    { check: "endpoint.logs", severity: "ok", message: "logs is ready", details: { needed_by: "every agent" } },
    { check: "inbound.endpoint", severity: "info", message: "no PrivateLink endpoint", details: { endpoints: [] } },
    { check: "cost", severity: "info", message: "cost", details: { interface_endpoints: ["x"], availability_zones: 2, endpoint_az_hours: 2, nat_gateways: [], pricing: { privatelink: "https://aws.amazon.com/privatelink/pricing/" } } },
  ],
  checked_at: null,
  legacy: false,
  created_by: null,
  created_at: null,
  updated_at: null,
  archived_at: null,
  allowed: true,
  is_default: false,
  projects: [{ project_id: "risk", is_default: true }, { project_id: "collect", is_default: false }],
  agents: 3,
};

const render = (node: JSX.Element) => renderToStaticMarkup(<MemoryRouter>{node}</MemoryRouter>);

describe("network profiles page", () => {
  beforeAll(async () => {
    vi.stubGlobal("document", { documentElement: {} });
    await i18n.changeLanguage("en");
  });

  it("shows the endpoint matrix, inbound endpoint, cost and the shared warning", () => {
    const html = render(<ProfileDetail profile={profile} />);
    expect(html).toContain("Endpoint requirements");
    expect(html).toContain("no PrivateLink endpoint");
    expect(html).toContain("Depends on 1 interface endpoint(s) × 2 Availability Zone(s)");
    expect(html).toContain("https://aws.amazon.com/privatelink/pricing/");
    expect(html).toContain("Shared by 2 projects");
    expect(azsOf(profile)).toEqual(["usw2-az1", "usw2-az2"]);
  });

  it("is an administrator's Configure entry after Workspaces", () => {
    const admin = hostedGroups(true).flatMap((g) => g.items.map((i) => i.to));
    expect(admin).toContain("/v3/network");
    expect(admin.indexOf("/v3/network")).toBe(admin.indexOf("/v3/workspaces") + 1);
    expect(hostedGroups(false).flatMap((g) => g.items.map((i) => i.to))).not.toContain("/v3/network");
  });
});

describe("agent network card", () => {
  beforeAll(async () => {
    vi.stubGlobal("document", { documentElement: {} });
    await i18n.changeLanguage("en");
  });

  const vpc: AgentNetwork = {
    agent_id: "a1",
    mode: "VPC",
    profile: { id: "np-0123456789ab", name: "core-vpc", vpc_id: "vpc-1", status: "blocked", blocking: 1, warnings: 0, checked_at: null },
    vpc: { subnets: ["subnet-a", "subnet-b"], security_groups: ["sg-1"] },
    enis: { status: "ok", scope: "profile", count: 2, in_use: 1, interfaces: [] },
  };

  it("shows a public agent with the change link", () => {
    const html = render(<NetworkCardView agent={agent} loading={false} error={null} data={{ agent_id: "a1", mode: "PUBLIC", profile: null, vpc: null, enis: null }} gaps={[]} />);
    expect(html).toContain("Public (PUBLIC)");
    expect(html).toContain("/v2/agents?view=edit&amp;id=a1");
  });

  it("shows the profile, ENIs, preflight and the telemetry hint", () => {
    const html = render(<NetworkCardView agent={agent} loading={false} error={null} data={vpc} gaps={["logs"]} />);
    expect(html).toContain("core-vpc");
    expect(html).toContain("subnet-a, subnet-b");
    expect(html).toContain("2 in the profile&#x27;s subnets (1 in use)");
    expect(html).toContain("1 blocking");
    expect(html).toContain("cannot reach logs");
  });

  it("says when the ENIs cannot be read", () => {
    const html = render(<NetworkCardView agent={agent} loading={false} error={null}
      data={{ ...vpc, enis: { status: "unavailable", error: "UnauthorizedOperation", interfaces: [] } }} gaps={[]} />);
    expect(html).toContain("cannot read (UnauthorizedOperation)");
  });
});
