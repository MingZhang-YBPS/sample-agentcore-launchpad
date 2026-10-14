import { afterEach, describe, expect, it, vi } from "vitest";

import {
  type CheckResult,
  costSummary,
  countSeverities,
  emptyProfileForm,
  formToBody,
  groupResults,
  inboundEndpoints,
  networkApi,
  parseIds,
  parseReachable,
  profileToForm,
  telemetryGaps,
  validateProfile,
} from "./networkProfiles";
import type { NetworkProfile } from "./projects";

const RESULTS: CheckResult[] = [
  { check: "subnets.exist", severity: "ok", message: "every subnet exists" },
  { check: "subnets.free_ips", severity: "warn", message: "low" },
  { check: "endpoint.logs", severity: "block", message: "no logs endpoint" },
  { check: "endpoint.xray", severity: "ok", message: "ready" },
  {
    check: "inbound.endpoint",
    severity: "info",
    message: "private callers",
    details: { endpoints: [{ service: "bedrock-agentcore", endpoint_id: "vpce-1", private_dns: true, principals: ["*"], any_principal: true }] },
  },
  {
    check: "cost",
    severity: "info",
    message: "cost",
    details: { interface_endpoints: ["a", "b", "c"], availability_zones: 2, endpoint_az_hours: 6, nat_gateways: ["nat-1"], pricing: { privatelink: "https://x" } },
  },
  { check: "probe", severity: "ok", message: "running", status: "running", started_at: "t" },
];

const profile = (over: Partial<NetworkProfile> = {}): NetworkProfile => ({
  id: "np-0123456789ab",
  workspace_id: "default",
  name: "core-vpc",
  vpc_id: "vpc-0123456789abcdef0",
  subnet_ids: ["subnet-0aaaaaaaaaaaaaaa1", "subnet-0bbbbbbbbbbbbbbb2"],
  security_group_ids: ["sg-0ccccccccccccccc3"],
  purpose: "agents",
  internet: "none",
  reachable: [{ cidr: "10.1.0.0/16", port: 5432, note: "db" }, { host: "api.internal", port: 443 }],
  status: "ok",
  check_results: RESULTS,
  checked_at: null,
  legacy: false,
  created_by: null,
  created_at: null,
  updated_at: null,
  archived_at: null,
  allowed: false,
  is_default: false,
  ...over,
});

describe("network profile results", () => {
  it("groups the rows into checks, endpoints and the informational entries", () => {
    const g = groupResults(RESULTS);
    expect(g.checks.map((r) => r.check)).toEqual(["subnets.exist", "subnets.free_ips"]);
    expect(g.endpoints.map((r) => r.check)).toEqual(["endpoint.logs", "endpoint.xray"]);
    expect(g.inbound?.check).toBe("inbound.endpoint");
    expect(g.cost?.check).toBe("cost");
    expect(g.probe?.status).toBe("running");
  });

  it("counts blocking and warning rows, never the informational ones", () => {
    expect(countSeverities(RESULTS)).toEqual({ block: 1, warn: 1 });
  });

  it("reads the cost, inbound and telemetry facts", () => {
    expect(costSummary(RESULTS)).toEqual({ interfaceEndpoints: 3, availabilityZones: 2, endpointAzHours: 6, natGateways: 1, pricing: { privatelink: "https://x" } });
    expect(inboundEndpoints(RESULTS)[0].endpoint_id).toBe("vpce-1");
    expect(telemetryGaps(RESULTS)).toEqual(["logs"]);
    expect(costSummary([])).toBeNull();
  });
});

describe("the profile form", () => {
  it("round-trips a profile and parses targets", () => {
    const form = profileToForm(profile());
    expect(form.reachable).toBe("10.1.0.0/16:5432 db\napi.internal:443");
    const body = formToBody(form);
    expect(body.subnet_ids).toHaveLength(2);
    expect(body.reachable).toEqual([{ cidr: "10.1.0.0/16", port: 5432, note: "db" }, { host: "api.internal", port: 443 }]);
    expect(validateProfile(form)).toEqual({});
  });

  it("names the field that is wrong", () => {
    const errors = validateProfile({ ...emptyProfileForm(), name: "x", vpc_id: "vpc-zz", subnets: "subnet-0aaaaaaaaaaaaaaa1", security_groups: "", reachable: "no-port" });
    expect(Object.keys(errors).sort()).toEqual(["reachable", "security_groups", "subnets", "vpc_id"]);
    expect(parseReachable("host:70000")).toEqual([null]);
    expect(parseIds("a, b\nb  c")).toEqual(["a", "b", "c"]);
  });
});

describe("network API", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("posts the check and the probe of one profile", async () => {
    const fetchMock = vi.fn(async () => new Response(JSON.stringify(profile()), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    await networkApi.check("np-1");
    await networkApi.probe("np-1");
    await networkApi.putSettings({ network_public_allowed: false });
    const calls = fetchMock.mock.calls as unknown as [string, RequestInit][];
    expect(calls.map(([url, init]) => `${init.method} ${url}`)).toEqual([
      "POST /api/network-profiles/np-1/check",
      "POST /api/network-profiles/np-1/probe",
      "PUT /api/network-profiles/settings",
    ]);
  });
});
