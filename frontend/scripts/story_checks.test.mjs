// The storyline consistency checks (scripts/story_checks.mjs, guided-console §7):
// the source parsers agree with the real modules, each check flags what it should,
// both baselines only shrink, and the repository passes as it stands.
import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, it } from "vitest";

import { DEFAULT_NEXT, NEXT_ACTIONS } from "../src/v3/story/actions";
import { AGENT_STORY, PLATFORM_STORY } from "../src/v3/story/stories";
import {
  checkActions, checkCopy, compareCounts, compareList, FRONTEND, missingErrorCopy, parseActionKeys,
  parseBackendNext, parseDefaultNext, parseErrorCodes, parseSteps,
} from "./story_checks.mjs";

const read = (rel) => readFileSync(resolve(FRONTEND, rel), "utf-8");

describe("the parsers read what the modules export", () => {
  it("every NEXT_ACTIONS key, in order", () => {
    expect(parseActionKeys(read("src/v3/story/actions.ts"))).toEqual(Object.keys(NEXT_ACTIONS));
  });
  it("every DEFAULT_NEXT action", () => {
    expect(parseDefaultNext(read("src/v3/story/actions.ts"))).toEqual(Object.values(DEFAULT_NEXT));
  });
  it("every storyline step with its action", () => {
    const steps = [...AGENT_STORY, ...PLATFORM_STORY].map((s) => ({ key: s.key, action: s.action }));
    expect(parseSteps(read("src/v3/story/stories.ts"))).toEqual(steps);
  });
  it("backend error codes: AppError, NotFoundError, a subclass's super(), across lines; not f-strings", () => {
    const src = [
      'raise AppError("release.no_criteria", "x", status_code=409)',
      "raise NotFoundError(",
      '    "golden.not_found", "no such set")',
      'super().__init__("kb.attached", "attached")',
      'raise AppError(f"aws.{code}", "computed")',
      'log("not.a_code")',
    ].join("\n");
    expect(parseErrorCodes(src)).toEqual(["release.no_criteria", "golden.not_found", "kb.attached"]);
  });
  it("backend next actions", () => {
    expect(parseBackendNext('next=[next_step("criteria.publish", agent_id=a), next_step( "golden.open")]'))
      .toEqual(["criteria.publish", "golden.open"]);
  });
});

describe("the checks", () => {
  const steps = [{ key: "build.create", action: "agent.create" }];
  it("an action not in the table fails, from a step, DEFAULT_NEXT or the backend", () => {
    expect(checkActions({ actions: ["agent.create"], steps, defaultNext: [], backendNext: [] })).toEqual([]);
    const problems = checkActions({
      actions: ["agent.create"],
      steps: [...steps, { key: "x.y", action: "nope.step" }],
      defaultNext: ["nope.default"],
      backendNext: ["nope.backend"],
    });
    expect(problems).toHaveLength(3);
    expect(problems.join("\n")).toMatch(/nope\.step[\s\S]*nope\.default[\s\S]*nope\.backend/);
  });
  it("an action or step without en or zh-CN copy fails", () => {
    const full = { apiNext: { "agent.create": "Create" }, story: { step: { "build.create": "Create the agent" } } };
    expect(checkCopy({ actions: ["agent.create"], steps, locales: { en: full, "zh-CN": full } })).toEqual([]);
    const problems = checkCopy({ actions: ["agent.create"], steps, locales: { en: full, "zh-CN": { apiNext: {}, story: {} } } });
    expect(problems).toEqual(["zh-CN: apiNext.agent.create is missing", "zh-CN: story.step.build.create is missing"]);
  });
  it("a code missing copy in either language counts", () => {
    const locales = { en: { apiErrors: { "a.b": "x", "a.c": "y" } }, "zh-CN": { apiErrors: { "a.b": "x" } } };
    expect(missingErrorCopy(new Set(["a.b", "a.c", "a.d"]), locales)).toEqual(["a.c", "a.d"]);
  });
});

describe("the baselines only shrink", () => {
  it("error codes: a new gap fails, and so does a listed code that has its copy now", () => {
    expect(compareList(["a.b"], ["a.b"])).toEqual({ added: [], fixed: [] });
    expect(compareList(["a.b", "a.new"], ["a.b"])).toEqual({ added: ["a.new"], fixed: [] });
    expect(compareList([], ["a.b"])).toEqual({ added: [], fixed: ["a.b"] });
  });
  it("disabled controls: a grown or unlisted file fails, a shrunk file must lower its entry", () => {
    expect(compareCounts({ "a.tsx": 2 }, { "a.tsx": 2 })).toEqual({ grown: [], shrunk: [] });
    expect(compareCounts({ "a.tsx": 3, "b.tsx": 1 }, { "a.tsx": 2 }).grown).toEqual([
      { file: "a.tsx", count: 3, allowed: 2 },
      { file: "b.tsx", count: 1, allowed: 0 },
    ]);
    expect(compareCounts({ "a.tsx": 1 }, { "a.tsx": 2, "gone.tsx": 1 }).shrunk).toEqual([
      { file: "a.tsx", count: 1, allowed: 2 },
      { file: "gone.tsx", count: 0, allowed: 1 },
    ]);
  });
});

describe("the repository as it stands", () => {
  it("passes every check (the baselines match exactly)", () => {
    const out = execFileSync("node", [resolve(FRONTEND, "scripts/story_checks.mjs")], { cwd: FRONTEND, encoding: "utf-8" });
    expect(out).toContain("story checks: OK");
  }, 120_000);
});
