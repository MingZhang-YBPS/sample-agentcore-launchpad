import { afterEach, describe, expect, it, vi } from "vitest";

import type { ApiKeyInfo } from "../api";
import {
  canWriteKeys,
  invokeAccessApi,
  keysForAgent,
  keyState,
  policyFileName,
  policyText,
  v1Sample,
} from "./invokeAccess";

const key = (over: Partial<ApiKeyInfo>): ApiKeyInfo => ({ id: "k", name: "k", prefix: "lp_live_ab", enabled: true, ...over });

afterEach(() => vi.unstubAllGlobals());

describe("invoke-access client", () => {
  it("sends qualifier and include only when set", async () => {
    const fetch = vi.fn(async () => new Response("{}", { status: 200, headers: { "Content-Type": "application/json" } }));
    vi.stubGlobal("fetch", fetch);
    await invokeAccessApi.agent("a 1");
    await invokeAccessApi.agent("a1", { qualifier: "live", include: ["stop_session", "memory"] });
    await invokeAccessApi.project("p1", { agents: ["a1", "a2"], qualifier: "live" });
    const urls = fetch.mock.calls.map((c) => String((c as unknown[])[0]));
    expect(urls[0]).toBe("/api/agents/a%201/invoke-access");
    expect(urls[1]).toBe("/api/agents/a1/invoke-access?qualifier=live&include=stop_session%2Cmemory");
    expect(urls[2]).toBe("/api/projects/p1/invoke-access?agents=a1%2Ca2&qualifier=live");
  });

  it("exports with the server's file name", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response("agent_id\n", {
      status: 200, headers: { "Content-Disposition": 'attachment; filename="p1-agent-arns.csv"' },
    })));
    const out = await invokeAccessApi.export("p1", "csv");
    expect(out.name).toBe("p1-agent-arns.csv");
    expect(await out.blob.text()).toBe("agent_id\n");
  });
});

describe("invoke-access helpers", () => {
  it("copies the policy as two-space JSON, identical to the backend document", () => {
    const policy = { Version: "2012-10-17", Statement: [{ Sid: "S", Effect: "Allow" as const, Action: ["a"], Resource: ["r"] }] };
    expect(JSON.parse(policyText(policy))).toEqual(policy);
    expect(policyText(policy)).toContain('\n  "Statement"');
  });

  it("names a downloaded policy safely", () => {
    expect(policyFileName("lab bot/1", "live")).toBe("lab-bot-1-live-invoke-policy.json");
    expect(policyFileName("///", null)).toBe("agent-invoke-policy.json");
  });

  it("keeps the keys that may call the agent: scoped to it or to every agent", () => {
    const keys = [key({ id: "all", agent_ids: [] }), key({ id: "mine", agent_ids: ["a1"] }),
      key({ id: "other", agent_ids: ["a2"] }), key({ id: "legacy" })];
    expect(keysForAgent(keys, "a1").map((k) => k.id)).toEqual(["all", "mine", "legacy"]);
  });

  it("states a key by its most blocking condition", () => {
    expect(keyState(key({}))).toBe("enabled");
    expect(keyState(key({ enabled: false }))).toBe("disabled");
    expect(keyState(key({ expired: true }))).toBe("expired");
    expect(keyState(key({ enabled: false, expired: true, creator_offboarded: "deleted" }))).toBe("offboarded");
  });

  it("lets project.write, bypass and admins mint keys", () => {
    expect(canWriteKeys(true, { permissions: [] })).toBe(true);
    expect(canWriteKeys(false, null)).toBe(true);
    expect(canWriteKeys(false, { permissions: ["project.use"] })).toBe(false);
    expect(canWriteKeys(false, { permissions: ["project.use", "project.write"] })).toBe(true);
    expect(canWriteKeys(false, { permissions: [], bypass: true })).toBe(true);
  });

  it("fills the /v1 sample with the new key", () => {
    const s = v1Sample("https://lp.example.com", "a1", "lp_live_secret");
    expect(s).toContain("https://lp.example.com/v1/agents/a1/invoke-stream");
    expect(s).toContain('x-api-key: lp_live_secret');
  });
});

describe("invoke-access / API keys copy", () => {
  it("has the same keys in English and Chinese, with full-width punctuation in Chinese", async () => {
    const en = (await import("../../locales/en/common.json")).default as { v3: Record<string, unknown> };
    const zh = (await import("../../locales/zh-CN/common.json")).default as { v3: Record<string, unknown> };
    const flat = (node: unknown, prefix = ""): Record<string, string> =>
      typeof node === "string"
        ? { [prefix]: node }
        : Object.assign({}, ...Object.entries(node as Record<string, unknown>).map(([k, v]) => flat(v, prefix ? `${prefix}.${k}` : k)));
    for (const ns of ["invokeAccess", "apiKeys"]) {
      const e = flat(en.v3[ns]);
      const z = flat(zh.v3[ns]);
      expect(Object.keys(z).sort()).toEqual(Object.keys(e).sort());
      for (const [k, text] of Object.entries(z)) {
        // ASCII , ; ? ! after a CJK character is half-width punctuation in Chinese copy
        expect(text, k).not.toMatch(/[一-鿿][,;?!]/);
      }
    }
  });
});
