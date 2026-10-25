import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import i18next from "i18next";
import { describe, expect, it } from "vitest";

import { checkImport, itemsToTable, parseCsvText, tableToImport, type FileIssueCode, type IssueCode } from "../../../../lib/goldenTable";
import { fileStem, templateItems } from "./tables";

const here = dirname(fileURLToPath(import.meta.url));
const read = (lang: string) => JSON.parse(readFileSync(resolve(here, `../../../../locales/${lang}/common.json`), "utf-8"));
const block = (lang: string) => read(lang).v2.dlc.golden.import;

const keys = (o: Record<string, unknown>, prefix = ""): string[] =>
  Object.entries(o).flatMap(([k, v]) => (v && typeof v === "object" ? keys(v as Record<string, unknown>, `${prefix}${k}.`) : [`${prefix}${k}`]));

const ROW: IssueCode[] = ["emptyId", "duplicateId", "emptyInput", "emptyExpected", "noTier", "badTier", "badSplit", "unknownCriteria", "notObject", "noCriteria", "sameAsInput"];
const FILE: FileIssueCode[] = ["missingColumns", "mergedCells", "noRows", "tooMany", "splitFull", "thin"];

async function tFor(lang: string) {
  const inst = i18next.createInstance();
  await inst.init({ lng: lang, resources: { [lang]: { translation: read(lang) } }, keySeparator: ".", nsSeparator: false, interpolation: { escapeValue: false } });
  return inst.t.bind(inst);
}

describe("golden import copy", () => {
  it("has the same keys in both languages and a message for every rule", () => {
    expect(keys(block("zh-CN")).sort()).toEqual(keys(block("en")).sort());
    for (const lang of ["en", "zh-CN"]) {
      for (const code of ROW) expect(block(lang).rule[code], `${lang} rule.${code}`).toBeTruthy();
      for (const code of FILE) expect(block(lang).file[code], `${lang} file.${code}`).toBeTruthy();
    }
  });

  it("uses full-width punctuation in Chinese", () => {
    const zh = keys(block("zh-CN")).map((k) => k.split(".").reduce((o, p) => o[p], block("zh-CN")) as string);
    for (const text of zh) expect(text, text).not.toMatch(/[一-鿿][,:;?!]|[一-鿿]\.$/);
  });
});

describe("templates", () => {
  const criteria = { known: new Set(["C1", "P1", "K1"]), published: [{ key: "C1", text: "准确" }, { key: "P1", text: "政策" }, { key: "K1", text: "知识" }] };

  it("import back without an error, in both languages and with no criteria table", async () => {
    for (const lang of ["en", "zh-CN"]) {
      const t = await tFor(lang);
      for (const served of [criteria, { known: null, published: [] }]) {
        const table = itemsToTable(templateItems(t as never, served), { zh: lang === "zh-CN", withSplit: true });
        const csv = [table.header, ...table.rows].map((r) => r.map((c) => `"${c.replace(/"/g, '""')}"`).join(",")).join("\n");
        const check = checkImport(tableToImport(parseCsvText(csv), "csv"), { mode: "seed", fallbackTier: "known_bad", known: served.known });
        expect(check.blocked, `${lang}`).toBe(false);
        expect(check.items.map((i) => i.metadata?.dlc?.case_tier)).toEqual(["known_good", "known_bad", "adversarial"]);
      }
    }
  });

  it("names files after the set", () => {
    expect(fileStem("客服 黄金集 v1")).toBe("客服-黄金集-v1");
    expect(fileStem(" / ")).toBe("golden-set");
  });
});
