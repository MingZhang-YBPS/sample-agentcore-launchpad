import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import { ApiError } from "../../../lib/api";
import { runNamed, runRefusal } from "./calibrationRunError";

const here = dirname(fileURLToPath(import.meta.url));
const read = (p: string) => readFileSync(resolve(here, p), "utf-8");
const locale = (lang: string) => JSON.parse(read(`../../../locales/${lang}/common.json`));

describe("calibration: run refusals", () => {
  it("explains a pasted run name and asks for a same-named run", () => {
    const err = new ApiError("run.not_found", "x", { run_id: "adp-r1-calibration-v2-20261014" });
    expect(runRefusal(err, "ignored", "R1")).toEqual({
      key: "v2.dlc.calibration.runNotFound",
      vars: { id: "adp-r1-calibration-v2-20261014" },
      lookupName: "adp-r1-calibration-v2-20261014",
    });
  });

  it("falls back to what was typed when an older backend sends no detail", () => {
    const err = new ApiError("run.not_found", "run not found", null);
    expect(runRefusal(err, "abc", "R1")?.vars).toEqual({ id: "abc" });
  });

  it("names the agent another agent's run evaluated", () => {
    const err = new ApiError("annotation.run_mismatch", "x", { run_id: "r", agent_id: "a2", agent_name: "adp-order-query" });
    expect(runRefusal(err, "r", "R1")).toEqual({ key: "v2.dlc.calibration.runOtherAgent", vars: { agent: "adp-order-query" } });
  });

  it("names the criterion a run has no verdicts for", () => {
    const err = new ApiError("annotation.no_items", "x", { run_id: "r", criterion_key: "R1" });
    expect(runRefusal(err, "r", "S2")).toEqual({ key: "v2.dlc.calibration.noResultsForCriterion", vars: { key: "R1" } });
  });

  it("leaves other errors (and no_items without a run) to the generic message", () => {
    expect(runRefusal(new ApiError("annotation.no_items", "supply items", null), "", "R1")).toBeNull();
    expect(runRefusal(new ApiError("http.500", "boom", null), "r", "R1")).toBeNull();
    expect(runRefusal(new Error("net"), "r", "R1")).toBeNull();
  });

  it("finds the newest run carrying the typed name exactly", () => {
    const runs = [
      { id: "new", name: "release v7" },
      { id: "old", name: "release v7" },
      { id: "other", name: "release v7 · regression" },
    ];
    expect(runNamed(runs, " release v7 ")).toBe("new");
    expect(runNamed(runs, "release")).toBeNull();
    expect(runNamed([{ id: "x", name: null }], "")).toBeNull();
  });

  it("has every message in both languages, zh-CN with full-width punctuation", () => {
    for (const lang of ["en", "zh-CN"]) {
      const cal = locale(lang).v2.dlc.calibration;
      for (const key of ["runNotFound", "runNamedLike", "useThisRun", "runOtherAgent", "noResultsForCriterion", "fromRunHint"]) {
        expect(cal[key], `${lang} ${key}`).toBeTruthy();
      }
      const tasks = locale(lang).v3.tasks;
      for (const key of ["runId", "taskId", "copyId", "copied", "copiedTaskId"]) {
        expect(tasks[key], `${lang} ${key}`).toBeTruthy();
      }
    }
    const zh = locale("zh-CN").v2.dlc.calibration;
    expect(zh.runNotFound).toContain("不是运行名称");
    expect(zh.runNotFound).not.toMatch(/[,;:]/);
    expect(locale("en").v2.dlc.calibration.fromRunHint).toMatch(/not the run name/);
  });
});
