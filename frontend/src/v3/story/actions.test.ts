import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import { DEFAULT_NEXT, errorNext, NEXT_ACTIONS, resolveNext } from "./actions";

const here = dirname(fileURLToPath(import.meta.url));
const read = (rel: string) => readFileSync(resolve(here, rel), "utf-8");
const locale = (lang: string) => JSON.parse(read(`../../locales/${lang}/common.json`));

/** The `/v3/<path>` routes App.tsx serves. */
function v3Routes(): Set<string> {
  const app = read("../../App.tsx");
  const section = app.slice(app.indexOf('path="v3"'), app.indexOf('path="v2"'));
  return new Set([...section.matchAll(/<Route path="([a-z/-]+)"/g)].map((m) => `/v3/${m[1]}`));
}

const ALL = { agent_id: "ag-1", lineage_id: "lin-1", dataset_id: "gs-1", task_id: "t-1", workspace_id: "ws-1", criterion: "C1" };

describe("NEXT_ACTIONS", () => {
  it("every action opens a V3 route the console serves", () => {
    const routes = v3Routes();
    expect(routes.size).toBeGreaterThan(20);
    for (const [key, spec] of Object.entries(NEXT_ACTIONS)) {
      const path = spec.to(ALL).split("?")[0];
      expect(routes.has(path), `${key} → ${path}`).toBe(true);
    }
  });

  it("every action has en + zh-CN copy under apiNext", () => {
    for (const lang of ["en", "zh-CN"]) {
      const copy = locale(lang).apiNext;
      for (const [key, spec] of Object.entries(NEXT_ACTIONS)) {
        expect(spec.label).toBe(`apiNext.${key}`);
        expect(copy[key], `${lang} ${key}`).toBeTruthy();
      }
    }
  });

  it("builds the deep links the pages read, leaving out what is missing", () => {
    expect(NEXT_ACTIONS["criteria.publish"].to({ agent_id: "ag-1" })).toBe("/v3/standards?agent=ag-1&view=criteria");
    expect(NEXT_ACTIONS["golden.open"].to({ agent_id: "ag-1", dataset_id: "gs-1" })).toBe("/v3/standards?agent=ag-1&view=golden&golden=gs-1");
    expect(NEXT_ACTIONS["gate.open"].to({ agent_id: "a b" })).toBe("/v3/gate?agent=a+b");
    expect(NEXT_ACTIONS["eval.new"].to({ agent_id: "ag-1" })).toBe("/v3/tasks?view=new&agent=ag-1");
    expect(NEXT_ACTIONS["agent.create"].to({})).toBe("/v3/create");
    // the feedback and observe steps carry the agent, so the page's position line shows
    expect(NEXT_ACTIONS["issues.open"].to({ agent_id: "ag-1" })).toBe("/v3/issues?agent=ag-1");
    expect(NEXT_ACTIONS["issues.open"].to({})).toBe("/v3/issues");
    expect(NEXT_ACTIONS["traces.open"].to({ agent_id: "ag-1" })).toBe("/v3/observability?tab=traces&agent=ag-1");
  });

  it("drops an action the console does not know (frontend and backend ship apart)", () => {
    const out = resolveNext([
      { action: "criteria.publish", params: { agent_id: "ag-1" } },
      { action: "doc.flywheel_evaluate", params: {} },
    ]);
    expect(out.map((a) => a.key)).toEqual(["criteria.publish"]);
  });

  it("the page's own params fill in under the backend's", () => {
    const [a] = resolveNext([{ action: "golden.open", params: { dataset_id: "gs-9" } }], { agent_id: "ag-1", dataset_id: "x" });
    expect(a.to).toBe("/v3/standards?agent=ag-1&view=golden&golden=gs-9");
  });

  it("a code without a backend next falls back to DEFAULT_NEXT; a backend next wins", () => {
    expect(errorNext({ code: "aws.access_denied", next: [] }).map((a) => a.key)).toEqual(["workspace.fix_role"]);
    expect(errorNext({ code: "aws.access_denied", next: [{ action: "agent.create", params: {} }] }).map((a) => a.key)).toEqual(["agent.create"]);
    expect(errorNext({ code: "release.self_sign", next: [] })).toEqual([]);
    for (const action of Object.values(DEFAULT_NEXT)) expect(NEXT_ACTIONS[action]).toBeDefined();
  });

  it("every next action the backend names is in the table", () => {
    const files = ["dlc/releases.py", "dlc/criteria.py", "dlc/golden.py", "dlc/admission.py", "dlc/calibration.py", "routers/dlc.py"];
    const named = new Set<string>();
    for (const f of files) {
      for (const m of read(`../../../../backend/app/${f}`).matchAll(/next_step\("([a-z_.]+)"/g)) named.add(m[1]);
    }
    expect(named.size).toBeGreaterThan(8);
    for (const action of named) expect(NEXT_ACTIONS[action as keyof typeof NEXT_ACTIONS], action).toBeDefined();
  });
});

describe("first batch of error codes (guided-console §3.5)", () => {
  const FAMILIES = ["release", "golden", "criteria", "admission", "waiver", "calibration"];
  it("every Agent-DLC error code the backend raises has en + zh-CN copy", () => {
    const files = ["dlc/releases.py", "dlc/criteria.py", "dlc/golden.py", "dlc/admission.py", "dlc/calibration.py", "routers/dlc.py"];
    const codes = new Set<string>();
    for (const f of files) {
      const src = read(`../../../../backend/app/${f}`);
      for (const m of src.matchAll(/(?:AppError|NotFoundError)\(\s*"([a-z_]+\.[a-z_]+)"/g)) {
        if (FAMILIES.includes(m[1].split(".")[0])) codes.add(m[1]);
      }
    }
    expect(codes.size).toBeGreaterThan(40);
    for (const lang of ["en", "zh-CN"]) {
      const errors = locale(lang).apiErrors;
      for (const code of codes) expect(errors[code], `${lang} apiErrors.${code}`).toBeTruthy();
    }
  });

  it("the reasons come in both languages, and zh-CN uses full-width punctuation", () => {
    const en = locale("en").apiErrors;
    const zh = locale("zh-CN").apiErrors;
    const whys = Object.keys(en).filter((k) => k.endsWith("_why") && FAMILIES.includes(k.split(".")[0]));
    expect(whys.length).toBeGreaterThan(10);
    for (const k of whys) {
      expect(zh[k], k).toBeTruthy();
      expect(zh[k], k).not.toMatch(/[,;:](\s|$)|\.$/);
    }
  });
});
