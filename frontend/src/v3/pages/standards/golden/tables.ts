/**
 * Templates and exports for golden-set items (golden-import §3). The Excel template has
 * the samples sheet (§2.1 header, three example rows, the case tiers and the current
 * published criteria in header notes) and a sheet that explains every column; the CSV
 * and JSON templates carry the same three rows. Templates are built at download time,
 * so the criteria ids are the table's current ones, and they import without an error.
 */
import type { TFunction } from "i18next";

import type { CaseTier, GoldenItem, GoldenSplit } from "../../../../lib/dlc";
import { CASE_TIERS } from "../../../../lib/dlc";
import { COLUMNS, downloadXlsx, itemsToTable, type SheetSpec, type Table } from "../../../../lib/goldenTable";
import { downloadCsv } from "../../../../v2/format";
import type { ServedCriteria } from "./served";

export type TableFormat = "xlsx" | "csv" | "json";

const K = "v2.dlc.golden.import";

/** A file name from the set's name, keeping CJK and dropping what file systems refuse. */
export function fileStem(name: string): string {
  return name.trim().replace(/[\\/:*?"<>|\s]+/g, "-").replace(/^-+|-+$/g, "") || "golden-set";
}

/** Three example rows — a known good, a known bad and an adversarial case. */
export function templateItems(t: TFunction, criteria: ServedCriteria): GoldenItem[] {
  const keys = criteria.published.map((c) => c.key);
  const sample = (id: string, tier: CaseTier, part: string, ids: string[]): GoldenItem => ({
    scenario_id: id,
    turns: [{ input: t(`${K}.sample.${part}.input`), expected_response: t(`${K}.sample.${part}.expected`) }],
    metadata: {
      dlc: {
        case_tier: tier,
        ...(ids.length ? { criteria_ids: ids } : {}),
        origin: "manual",
        expected_source: "annotator",
      },
    },
  });
  return [
    sample("example-good-01", "known_good", "good", keys.slice(0, 2)),
    sample("example-bad-01", "known_bad", "bad", keys.slice(0, 1)),
    sample("example-adv-01", "adversarial", "adversarial", keys.slice(-1)),
  ];
}

function guideSheet(t: TFunction, zh: boolean): Table {
  return {
    header: [t(`${K}.guide.column`), t(`${K}.guide.required`), t(`${K}.guide.meaning`)],
    rows: [
      ...COLUMNS.map((c) => [`${c.key} / ${c.zh}`, t(c.required ? `${K}.guide.yes` : `${K}.guide.no`), t(`${K}.guide.${c.key}`)]),
      [zh ? "其他列" : "other columns", t(`${K}.guide.no`), t(`${K}.guide.other`)],
      ["", "", t(`${K}.guide.multiTurn`)],
    ],
  };
}

function samplesSheet(t: TFunction, table: Table, criteria: ServedCriteria, zh: boolean): SheetSpec {
  const tierHeader = zh ? "用例档位" : "case_tier";
  const criteriaHeader = zh ? "考察判据" : "criteria_ids";
  const tiers = CASE_TIERS.map((c) => `${c} — ${t(`v2.dlc.caseTier.${c}`)}`).join("\n");
  const listed = criteria.published.map((c) => `${c.key} — ${c.text}`).join("\n");
  return {
    name: t(`${K}.sheetSamples`),
    table,
    notes: {
      [tierHeader]: `${t(`${K}.tierNote`)}\n${tiers}`,
      [criteriaHeader]: listed ? `${t(`${K}.criteriaNote`)}\n${listed}` : t(`${K}.noCriteriaNote`),
    },
    widths: table.header.map((h) => (/input|问题|expected|期望/.test(h) ? 48 : 16)),
  };
}

function downloadText(filename: string, text: string, type: string) {
  const url = URL.createObjectURL(new Blob([text], { type }));
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

async function downloadAs(format: TableFormat, stem: string, items: GoldenItem[], t: TFunction, criteria: ServedCriteria, zh: boolean, withSplit: boolean) {
  if (format === "json") {
    downloadText(`${stem}.json`, `${JSON.stringify(items, null, 2)}\n`, "application/json");
    return;
  }
  const table = itemsToTable(items, { zh, withSplit });
  if (format === "csv") {
    downloadCsv(`${stem}.csv`, table.header, table.rows);
    return;
  }
  await downloadXlsx(`${stem}.xlsx`, [samplesSheet(t, table, criteria, zh), { name: t(`${K}.sheetGuide`), table: guideSheet(t, zh), widths: [28, 10, 80] }]);
}

/** `<set name>-template.<ext>` — the template rows, plus the split column empty (stratify). */
export function downloadTemplate(format: TableFormat, setName: string, t: TFunction, criteria: ServedCriteria, zh: boolean) {
  return downloadAs(format, `${fileStem(setName)}-template`, templateItems(t, criteria), t, criteria, zh, true);
}

/** The set's current items in the same format — read-only, to compare against when adding. */
export function downloadItems(format: TableFormat, setName: string, items: (GoldenItem & { split: GoldenSplit })[], t: TFunction, criteria: ServedCriteria, zh: boolean) {
  return downloadAs(format, `${fileStem(setName)}-items`, items, t, criteria, zh, true);
}
