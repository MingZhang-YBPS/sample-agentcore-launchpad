import { beforeAll, describe, expect, it, vi } from "vitest";

import i18n from "../i18n";
import { agentNotRunning, firstReason, permReason, reasonText, stepReason } from "./reasons";
import { NEXT_ACTIONS } from "./story/actions";

const t = (key: string, opts?: Record<string, unknown>) => i18n.t(key, opts);

beforeAll(async () => {
  vi.stubGlobal("document", { documentElement: {} });
  await i18n.changeLanguage("en");
});

describe("disabled reasons (guided-console §4)", () => {
  it("a missing story step names the step and links its action", () => {
    const r = stepReason(t, "evaluate.criteria.sign", { agent_id: "ag-1", lineage_id: undefined });
    expect(reasonText(r)).toBe("Complete first: Sign the criteria table");
    expect(typeof r === "string" ? null : r.next).toEqual({ action: "criteria.sign", params: { agent_id: "ag-1" } });
  });

  it("every step a reason can name has an action in the table", () => {
    for (const key of ["build.create", "evaluate.golden.seal", "release.api_key", "platform.models"]) {
      const r = stepReason(t, key);
      expect(typeof r !== "string" && r.next && r.next.action in NEXT_ACTIONS, key).toBe(true);
    }
    expect(() => stepReason(t, "no.such_step")).toThrow(/unknown story step/);
  });

  it("a missing permission says who usually holds it; only an administrator gets the Users link", () => {
    expect(permReason(t, "perm:golden.admit")).toBe("Needs the golden.admit permission, usually held by biz-owner.");
    expect(permReason(t, "custom.key")).toBe("Needs the custom.key permission.");
    const admin = permReason(t, "release.sign", true);
    expect(typeof admin !== "string" && admin.next).toEqual({ action: "users.open", params: {} });
    expect(permReason(t, "admin")).toBe("Only an administrator can do this.");
  });

  it("the first reason that applies, in the order a user fixes them", () => {
    expect(firstReason(false, null, "b", "c")).toBe("b");
    expect(firstReason(false, undefined)).toBeNull();
  });

  it("an agent that is not running points at the agent", () => {
    const r = agentNotRunning(t, "ag-9");
    expect(typeof r !== "string" && r.next).toEqual({ action: "agent.open", params: { agent_id: "ag-9" } });
  });

  it("zh-CN has every reason, in full-width punctuation", () => {
    const en = i18n.getResourceBundle("en", "common").story.reason as Record<string, string>;
    const zh = i18n.getResourceBundle("zh-CN", "common").story.reason as Record<string, string>;
    expect(Object.keys(zh).sort()).toEqual(Object.keys(en).sort());
    for (const [k, v] of Object.entries(zh)) expect(v, k).not.toMatch(/[,;:](\s|$)|\.$/);
  });
});
