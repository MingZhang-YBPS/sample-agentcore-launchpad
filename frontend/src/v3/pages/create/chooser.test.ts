import { describe, expect, it } from "vitest";

import en from "../../../locales/en/common.json";
import zh from "../../../locales/zh-CN/common.json";
import {
  type Answers,
  CAP_COLUMNS,
  CAP_ROWS,
  CAPABILITIES,
  capColumn,
  chooserKeys,
  DECISION_TABLE,
  DLC,
  HAVE,
  readSkipped,
  recommend,
  relevant,
  SKIP_KEY,
  USE,
  type WayKey,
  wayHref,
  writeSkipped,
} from "./chooser";

// storyline-ux §3.6 decision table, one case per row (plus the A2A gap row)
const ROWS: { name: string; answers: Answers; primary: WayKey; alternative: WayKey | null; row: string }[] = [
  { name: "nothing + just trying → Harness, alt assistant", answers: { have: "nothing", use: "tools", dlc: "try" }, primary: "harness", alternative: "assistant", row: "try" },
  { name: "nothing + full DLC → Strands, alt Harness", answers: { have: "nothing", use: "tools", dlc: "full" }, primary: "strands", alternative: "harness", row: "full" },
  { name: "a requirement description → assistant, alt Strands", answers: { have: "brief" }, primary: "assistant", alternative: "strands", row: "brief" },
  { name: "nothing + multi-agent flow → Studio, alt Strands", answers: { have: "nothing", use: "multi" }, primary: "studio", alternative: "strands", row: "multi" },
  { name: "existing code → BYOC", answers: { have: "code" }, primary: "byoc", alternative: null, row: "code" },
  { name: "agent-SDK code → other Agent SDK", answers: { have: "code", sdk: true }, primary: "sdk", alternative: null, row: "codeSdk" },
  { name: "an MCP server → MCP server", answers: { have: "mcp", use: "tool" }, primary: "mcp", alternative: null, row: "mcp" },
  { name: "a running Runtime → import", answers: { have: "runtime" }, primary: "import", alternative: null, row: "runtime" },
  { name: "nothing + tool for other agents → Strands (A2A), alt MCP", answers: { have: "nothing", use: "tool" }, primary: "strands", alternative: "mcp", row: "tool" },
];

describe("recommend", () => {
  it.each(ROWS)("$name", ({ answers, primary, alternative, row }) => {
    const rec = recommend(answers);
    expect(rec).not.toBeNull();
    expect(rec?.row).toBe(row);
    expect(rec?.primary).toBe(primary);
    expect(rec?.alternative).toBe(alternative);
  });

  it("the workshop developer (nothing yet, full DLC) lands on Strands for every purpose but flows and tools", () => {
    for (const use of ["qa", "tools"] as const) {
      expect(recommend({ have: "nothing", use, dlc: "full" })?.primary).toBe("strands");
    }
  });

  it("whatever is in hand decides alone, whatever the other answers", () => {
    for (const use of USE) {
      for (const dlc of DLC) {
        expect(recommend({ have: "runtime", use, dlc })?.primary).toBe("import");
        expect(recommend({ have: "mcp", use, dlc })?.primary).toBe("mcp");
        expect(recommend({ have: "code", use, dlc })?.primary).toBe("byoc");
        expect(recommend({ have: "brief", use, dlc })?.primary).toBe("assistant");
      }
    }
  });

  it("stays undecided until the questions a row needs are answered", () => {
    expect(recommend({})).toBeNull();
    expect(recommend({ have: "nothing" })).toBeNull();
    expect(recommend({ have: "nothing", use: "qa" })).toBeNull();
    expect(recommend({ have: "nothing", dlc: "full" })).toBeNull();
  });

  it("decides every complete set of answers, with at most one alternative that differs from the pick", () => {
    for (const have of HAVE) {
      for (const use of USE) {
        for (const dlc of DLC) {
          const rec = recommend({ have, use, dlc });
          expect(rec, `${have}/${use}/${dlc}`).not.toBeNull();
          expect(rec?.alternative).not.toBe(rec?.primary);
        }
      }
    }
  });

  it("carries the answers as wizard defaults: Q&A preselects a knowledge base, a tool on Strands is A2A", () => {
    expect(recommend({ have: "nothing", use: "qa", dlc: "full" })?.defaults).toEqual({ kb: true });
    expect(recommend({ have: "nothing", use: "tools", dlc: "full" })?.defaults).toEqual({ kb: false });
    expect(recommend({ have: "nothing", use: "tool" })?.defaults).toEqual({ kb: false, protocol: "a2a" });
  });

  it("every table row is reachable", () => {
    const hit = new Set(ROWS.map((r) => r.row));
    expect(DECISION_TABLE.map((r) => r.key).filter((k) => !hit.has(k))).toEqual([]);
  });
});

describe("relevant", () => {
  it("only Q1 matters once something is in hand", () => {
    expect([...relevant({ have: "code" })]).toEqual(["have"]);
  });
  it("the DLC question does not change a flow or a tool", () => {
    expect(relevant({ have: "nothing", use: "multi" }).has("dlc")).toBe(false);
    expect(relevant({ have: "nothing", use: "qa" }).has("dlc")).toBe(true);
  });
});

describe("wayHref", () => {
  it("enters the matching wizard with the answers as defaults", () => {
    expect(wayHref("strands", { kb: true })).toBe("/v2/agents?view=new&method=zip_runtime&kb=1");
    expect(wayHref("strands", { kb: false, protocol: "a2a" })).toBe("/v2/agents?view=new&method=zip_runtime&protocol=a2a");
    expect(wayHref("sdk")).toBe("/v2/agents?view=new&method=container");
    expect(wayHref("harness", { kb: true })).toBe("/v3/create?quick=1&kb=1");
    expect(wayHref("harness")).toBe("/v3/create?quick=1");
    expect(wayHref("mcp")).toBe("/v2/agents?view=new&method=byoc&protocol=mcp");
    expect(wayHref("byoc", { kb: true })).toBe("/v2/agents?view=new&method=byoc");
    expect(wayHref("import")).toBe("/agents/import");
    expect(wayHref("studio")).toBe("/create/studio");
    expect(wayHref("assistant")).toBe("/v3/assistant");
  });
});

describe("capability comparison", () => {
  it("covers the workshop 20-build §0 rows for the four methods", () => {
    expect(CAP_ROWS).toEqual(["artifact", "kb", "skills", "ab", "model", "observability"]);
    for (const r of CAP_ROWS) expect(Object.keys(CAPABILITIES[r]).sort()).toEqual([...CAP_COLUMNS].sort());
    expect(CAPABILITIES.ab.strands).toBe("yes");
    expect(CAPABILITIES.ab.harness).toBe("no");
    expect(CAPABILITIES.observability.byoc).toBe("no");
  });

  it("every 'not supported' cell names the alternative", () => {
    for (const lang of [en, zh]) {
      const cells = (lang as { v3: { create: { chooser: { cell: Record<string, Record<string, string>> } } } }).v3.create.chooser.cell;
      for (const r of CAP_ROWS) {
        for (const c of CAP_COLUMNS) {
          if (CAPABILITIES[r][c] !== "no") continue;
          expect(cells[r][c], `${r}.${c}`).toMatch(/Strands|Harness|SDK|ADOT|code|代码/);
          expect(cells[r][c].split(/—|——/).length, `${r}.${c} says the way out`).toBeGreaterThan(1);
        }
      }
    }
  });

  it("lights the column a way is built as", () => {
    expect(capColumn("studio")).toBe("strands");
    expect(capColumn("mcp")).toBe("byoc");
    expect(capColumn("import")).toBeNull();
    expect(capColumn("harness")).toBe("harness");
  });
});

describe("skip memory", () => {
  const store = () => {
    const m = new Map<string, string>();
    return {
      getItem: (k: string) => m.get(k) ?? null,
      setItem: (k: string, v: string) => void m.set(k, v),
      removeItem: (k: string) => void m.delete(k),
      m,
    };
  };
  it("remembers and forgets a skip", () => {
    const s = store();
    expect(readSkipped(s)).toBe(false);
    writeSkipped(s, true);
    expect(s.m.get(SKIP_KEY)).toBe("skip");
    expect(readSkipped(s)).toBe(true);
    writeSkipped(s, false);
    expect(readSkipped(s)).toBe(false);
  });
  it("a storage that throws or is missing reads as not skipped", () => {
    const broken = { getItem: () => { throw new Error("denied"); }, setItem: () => { throw new Error("quota"); }, removeItem: () => {} };
    expect(readSkipped(broken)).toBe(false);
    expect(() => writeSkipped(broken, true)).not.toThrow();
    expect(readSkipped(null)).toBe(false);
  });
});

describe("locales", () => {
  const lookup = (tree: unknown, key: string) =>
    key.split(".").reduce<unknown>((node, part) => (node && typeof node === "object" ? (node as Record<string, unknown>)[part] : undefined), tree);
  it.each([["en", en], ["zh-CN", zh]])("%s has every chooser string", (_lang, tree) => {
    const missing = chooserKeys().filter((k) => typeof lookup(tree, k) !== "string");
    expect(missing).toEqual([]);
  });
});
