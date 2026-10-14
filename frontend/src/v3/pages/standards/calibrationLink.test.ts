import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import { candidateLabel, candidateRunsPath } from "../../../lib/api/calibration";
import {
  calibratableCriteria,
  calibrateBlocker,
  calibrateUrl,
  readPrefill,
  soleCriterion,
  withoutPrefill,
} from "./calibrationLink";

const here = dirname(fileURLToPath(import.meta.url));
const locale = (lang: string) =>
  JSON.parse(readFileSync(resolve(here, `../../../locales/${lang}/common.json`), "utf-8"));

const run = {
  kind: "run" as const,
  mode: "evaluators" as const,
  status: "completed" as const,
  agentId: "ag-1",
  evaluators: ["Builtin.Correctness"],
};

describe("calibrateUrl / readPrefill", () => {
  it("opens the calibration view prefilled, with the criterion only when given", () => {
    expect(calibrateUrl("ag-1", "089e6e909e36", "R1")).toBe(
      "/v3/standards?view=calibration&agent=ag-1&new=1&run=089e6e909e36&criterion=R1",
    );
    expect(calibrateUrl("ag-1", "089e6e909e36")).not.toContain("criterion");
  });

  it("reads the prefill back and drops it, leaving the rest", () => {
    const params = new URLSearchParams(calibrateUrl("ag-1", "run 1&x", "R1").split("?")[1]);
    expect(readPrefill(params)).toEqual({ runId: "run 1&x", criterion: "R1" });
    const rest = withoutPrefill(params);
    expect(readPrefill(rest)).toBeNull();
    expect(rest.toString()).toBe("view=calibration&agent=ag-1");
    expect(readPrefill(new URLSearchParams("run=abc"))).toBeNull();
  });
});

describe("calibrateBlocker", () => {
  it("lets a completed batch evaluator run with an agent through", () => {
    expect(calibrateBlocker(run)).toBeNull();
    expect(calibrateBlocker(run, ["R1"])).toBeNull();
  });

  it("names each condition of §3.1", () => {
    expect(calibrateBlocker({ ...run, kind: "online" })).toBe("notBatch");
    expect(calibrateBlocker({ ...run, mode: "insights" })).toBe("notBatch");
    expect(calibrateBlocker({ ...run, status: "running" })).toBe("notCompleted");
    expect(calibrateBlocker({ ...run, agentId: null })).toBe("noAgent");
    expect(calibrateBlocker({ ...run, evaluators: [] })).toBe("noEvaluatorResults");
    // with the verdict summary at hand, it is the criteria that decide
    expect(calibrateBlocker(run, [])).toBe("noEvaluatorResults");
  });
});

describe("calibratableCriteria", () => {
  it("keeps the criteria that would give samples", () => {
    const keys = calibratableCriteria([{ key: "R2", items: 3 }, { key: "R1", items: 2 }, { key: "R3", items: 0 }]);
    expect(keys).toEqual(["R1", "R2"]);
    expect(soleCriterion(keys)).toBeNull();
    expect(soleCriterion(calibratableCriteria([{ key: "R1", items: 15 }]))).toBe("R1");
    expect(calibratableCriteria(undefined)).toEqual([]);
  });
});

describe("candidate runs client", () => {
  it("sends only what is set", () => {
    expect(candidateRunsPath({ agentId: "ag-1", criterionKey: "R1" })).toBe(
      "/api/calibration/candidate-runs?agent_id=ag-1&criterion_key=R1",
    );
    const path = candidateRunsPath({
      agentId: "ag-1", criterionKey: "R1", q: " adp-r1 ", source: "gate", onlyEligible: false, limit: 20, offset: 40,
    });
    expect(path).toContain("q=adp-r1&source=gate&only_eligible=false&limit=20&offset=40");
  });

  it("labels a run by name, then dataset, then id", () => {
    expect(candidateLabel({ id: "r1", name: "release v7", dataset_name: "d" })).toBe("release v7");
    expect(candidateLabel({ id: "r1", name: " ", dataset_name: "golden" })).toBe("golden");
    expect(candidateLabel({ id: "r1", name: null, dataset_name: null })).toBe("r1");
  });
});

describe("copy", () => {
  it("has every picker and jump message in both languages, zh-CN with full-width punctuation", () => {
    const blockers = ["notBatch", "notCompleted", "noAgent", "noEvaluatorResults"];
    for (const lang of ["en", "zh-CN"]) {
      const cal = locale(lang).v2.dlc.calibration;
      for (const key of ["pickRun", "pickRunHint", "pasteRunId", "pickFromList", "listTitle", "onlyEligible", "searchRun",
        "ineligibleGroup", "runItems_other", "releaseTag", "noCandidateRuns", "pasteNotListed", "calibrateFromRegression"]) {
        expect(cal[key], `${lang} ${key}`).toBeTruthy();
      }
      expect(Object.keys(cal.ineligibleReason).sort()).toEqual(["no_results", "not_completed"]);
      const tasks = locale(lang).v3.tasks;
      for (const key of ["calibrate", "calibrateShort", "calibrateCriterion", "calibrateCriteria", "filterCalibratable"]) {
        expect(tasks[key], `${lang} ${key}`).toBeTruthy();
      }
      expect(Object.keys(tasks.calibrateBlocked).sort()).toEqual([...blockers].sort());
    }
    const zh = locale("zh-CN");
    for (const text of [...Object.values(zh.v3.tasks.calibrateBlocked), zh.v2.dlc.calibration.runNotFound,
      zh.v2.dlc.calibration.noCandidateRuns, zh.v2.dlc.calibration.pasteNotListed, zh.v3.tasks.calibrateCriteria]) {
      expect(text).not.toMatch(/[,;:()]/);
    }
    // the not-found message now points at the dropdown it sits beside
    expect(zh.v2.dlc.calibration.runNotFound).toContain("下拉框");
    expect(locale("en").v2.dlc.calibration.runNotFound).toContain("dropdown");
  });
});
