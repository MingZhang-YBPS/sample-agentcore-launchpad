/**
 * Golden-set items as a table (golden-import §2–§4): parse a CSV / Excel sheet, a
 * JSON array or `id | question | expected` lines into the `items` the seed / items
 * endpoints already take, check every row before anything is sent, and turn items
 * back into a table (templates, export, error report).
 *
 * Parsing happens here, in the browser, so the API contract does not change (D-1).
 * The row rules mirror the server's G4 checks (`golden.empty_input`,
 * `golden.empty_expected`, `golden.unknown_criteria`, `backend/app/dlc/golden.py`)
 * plus the ones only a person can act on before the holdout is sealed (missing
 * column, duplicate id, no case tier, a criterion with too few items).
 *
 * Only single-turn rows are tabular (D-2): one row = one item with one turn; multi-
 * turn items stay JSON. SheetJS (`xlsx`, Apache-2.0) is imported on demand, only when
 * an `.xlsx` file is read or written; CSV goes through `papaparse` (MIT).
 */
import Papa from "papaparse";

import { CASE_TIERS, type CaseTier, type GoldenItem, type GoldenSplit } from "./dlc";

/** `MAX_ITEMS` in `backend/app/dlc/golden.py`: a split holds at most this many items. */
export const MAX_SPLIT_ITEMS = 200;
/** Request caps of `GoldenSeedIn` / `GoldenItemsIn` (`backend/app/routers/dlc.py`). */
export const MAX_SEED_ITEMS = 600;
export const MAX_ADD_ITEMS = 200;
/** Fewer items than this and a criterion's rate is noise (the coverage `thin` rule). */
export const THIN_BELOW = 3;
/** The server's default seed shares, dev / regression / holdout. */
export const SEED_SHARES: [number, number, number] = [0.5, 0.3, 0.2];
const SPLITS: GoldenSplit[] = ["dev", "regression", "holdout"];

/* ── columns ─────────────────────────────────────────────────────────────── */

export type Column =
  | "scenario_id"
  | "input"
  | "expected_response"
  | "case_tier"
  | "criteria_ids"
  | "split"
  | "expected_source";

/** §2.1 — recognised by name (English or Chinese), never by position. */
export const COLUMNS: { key: Column; zh: string; required: boolean }[] = [
  { key: "scenario_id", zh: "编号", required: true },
  { key: "input", zh: "问题", required: true },
  { key: "expected_response", zh: "期望答案", required: true },
  { key: "case_tier", zh: "用例档位", required: true },
  { key: "criteria_ids", zh: "考察判据", required: false },
  { key: "split", zh: "切分", required: false },
  { key: "expected_source", zh: "答案来源", required: false },
];
const COLUMN_OF = new Map<string, Column>(
  COLUMNS.flatMap((c) => [[c.key, c.key] as const, [c.zh, c.key] as const]),
);

const TIER_OF = new Map<string, CaseTier>([
  ...CASE_TIERS.map((t) => [t, t] as const),
  ["已知正例", "known_good"],
  ["已知反例", "known_bad"],
  ["模糊样本", "ambiguous"],
  ["对抗样本", "adversarial"],
]);
const SPLIT_OF = new Map<string, GoldenSplit>([
  ...SPLITS.map((s) => [s, s] as const),
  ["开发集", "dev"],
  ["回归集", "regression"],
  ["留出集", "holdout"],
  ["保留集", "holdout"], // the earlier name of 留出集, still accepted in uploads
]);
/** Sheet names read in preference to the first sheet. */
const SAMPLE_SHEETS = ["样本", "samples"];

const norm = (s: string) => s.trim().toLowerCase();
const columnOf = (header: string): Column | undefined => COLUMN_OF.get(norm(header)) ?? COLUMN_OF.get(header.trim());

/** Criteria ids are separated by `;`, `,` or a newline (full-width forms too). */
export function splitList(value: string, separators = /[;,；，\r\n]+/): string[] {
  return value.split(separators).map((s) => s.trim()).filter(Boolean);
}

/* ── parsing ─────────────────────────────────────────────────────────────── */

export type ImportFormat = "json" | "text" | "csv" | "xlsx";

export interface ImportRow {
  /** Where the row came from: the sheet row (header = 1), the text line, or the JSON position (1-based). */
  line: number;
  item: GoldenItem;
  /** The row's cells as read, for the error report (tables only). */
  cells?: string[];
  /** A case-tier / split cell that maps to nothing. */
  badTier?: string;
  badSplit?: string;
  /** A JSON element that is not an item object. */
  notObject?: boolean;
}

export interface ParsedImport {
  format: ImportFormat;
  /** Tables only: how the bytes were decoded (CSV) or which sheet was read (Excel). */
  encoding?: string;
  sheet?: string;
  header?: string[];
  rows: ImportRow[];
  missingColumns: Column[];
  /** The whole file is unusable (merged cells in a sheet). */
  fileError?: "mergedCells";
}

/** UTF-8 (BOM stripped) first; bytes that are not UTF-8 are read as GB18030 —
 *  what Windows Excel writes when it saves a Chinese CSV. */
export function decodeBytes(bytes: Uint8Array): { text: string; encoding: "UTF-8" | "GB18030" } {
  try {
    return { text: new TextDecoder("utf-8", { fatal: true }).decode(bytes), encoding: "UTF-8" };
  } catch {
    return { text: new TextDecoder("gb18030").decode(bytes), encoding: "GB18030" };
  }
}

/** RFC 4180 (quoted commas and newlines); the delimiter is guessed, so a TSV pasted from a sheet works too. */
export function parseCsvText(text: string): string[][] {
  const out = Papa.parse<string[]>(text.replace(/^\uFEFF/, ""), { skipEmptyLines: "greedy" });
  return out.data.map((row) => row.map((c) => (c == null ? "" : String(c))));
}

const blankRow = (row: string[]) => row.every((c) => String(c ?? "").trim() === "");

/** Rows of cells, header first, into items. Unknown columns go to `metadata.<name>`
 *  (a value with `;` becomes a list); `origin` is always `manual`. */
export function tableToImport(
  table: string[][],
  format: "csv" | "xlsx",
  extra: { encoding?: string; sheet?: string } = {},
): ParsedImport {
  const start = table.findIndex((r) => !blankRow(r));
  const header = start < 0 ? [] : table[start].map((h) => String(h ?? "").trim());
  const mapped = header.map(columnOf);
  const missingColumns = start < 0 ? COLUMNS.filter((c) => c.required).map((c) => c.key)
    : COLUMNS.filter((c) => c.required && !mapped.includes(c.key)).map((c) => c.key);
  const rows: ImportRow[] = [];
  for (let i = start + 1; start >= 0 && i < table.length; i++) {
    const cells = table[i].map((c) => String(c ?? ""));
    if (blankRow(cells)) continue;
    const get = (col: Column) => {
      const at = mapped.indexOf(col);
      return at < 0 ? "" : (cells[at] ?? "").trim();
    };
    const dlc: Record<string, unknown> = { origin: "manual", expected_source: get("expected_source") || "annotator" };
    const row: ImportRow = { line: i + 1, cells, item: { scenario_id: "", turns: [] } };
    const tier = get("case_tier");
    if (tier) {
      const known = TIER_OF.get(norm(tier)) ?? TIER_OF.get(tier);
      if (known) dlc.case_tier = known;
      else row.badTier = tier;
    }
    const criteria = splitList(get("criteria_ids"));
    if (criteria.length) dlc.criteria_ids = criteria;
    const metadata: Record<string, unknown> = {};
    header.forEach((name, at) => {
      const value = (cells[at] ?? "").trim();
      if (mapped[at] || !name || !value) return;
      metadata[name] = value.includes(";") ? splitList(value, /;/) : value;
    });
    row.item = {
      scenario_id: get("scenario_id"),
      turns: [{ input: get("input"), expected_response: get("expected_response") }],
      metadata: { ...metadata, dlc },
    };
    const split = get("split");
    if (split) {
      const known = SPLIT_OF.get(norm(split)) ?? SPLIT_OF.get(split);
      if (known) row.item.split = known;
      else row.badSplit = split;
    }
    rows.push(row);
  }
  return { format, ...extra, header, rows, missingColumns };
}

/** A header line that names at least two §2.1 columns — the text is a pasted table. */
function looksTabular(firstLine: string): boolean {
  const cells = firstLine.includes("\t") ? firstLine.split("\t") : firstLine.includes(",") ? firstLine.split(",") : [];
  return new Set(cells.map(columnOf).filter(Boolean)).size >= 2;
}

/** The text box: a JSON array, a table pasted from a sheet (CSV / TSV with a header),
 *  or one item per line, `scenario_id | question | expected answer`. Throws on bad JSON. */
export function parseText(text: string): ParsedImport {
  const trimmed = text.trim();
  if (trimmed.startsWith("[")) {
    const data = JSON.parse(trimmed) as unknown;
    if (!Array.isArray(data)) throw new Error("expected a JSON array of items");
    return {
      format: "json",
      missingColumns: [],
      rows: data.map((entry, index) => {
        const ok = entry !== null && typeof entry === "object" && !Array.isArray(entry);
        return ok
          ? { line: index + 1, item: entry as GoldenItem }
          : { line: index + 1, item: { scenario_id: "", turns: [] }, notObject: true };
      }),
    };
  }
  if (looksTabular(trimmed.split(/\r?\n/, 1)[0] ?? "")) return tableToImport(parseCsvText(trimmed), "csv");
  const rows: ImportRow[] = [];
  text.split("\n").forEach((raw, index) => {
    const line = raw.trim();
    if (!line) return;
    const [id = "", question = "", expected = ""] = line.split("|").map((p) => p.trim());
    rows.push({ line: index + 1, item: { scenario_id: id, turns: [{ input: question, expected_response: expected }] } });
  });
  return { format: "text", rows, missingColumns: [] };
}

/** The SheetJS module — imported lazily, so only a user who picks an `.xlsx` downloads it. */
type Xlsx = typeof import("xlsx");
export const loadXlsx = (): Promise<Xlsx> => import("xlsx");

/** An Excel workbook: the sheet named "样本" / "samples" if any, else the first one;
 *  cells as displayed text (dates and numbers as shown). Merged cells are refused —
 *  a merged block reads as one value and blank cells, which silently drops data. */
export function readWorkbook(XLSX: Xlsx, data: ArrayBuffer | Uint8Array): ParsedImport {
  const book = XLSX.read(data, { type: "array" });
  const name = book.SheetNames.find((n) => SAMPLE_SHEETS.includes(norm(n))) ?? book.SheetNames[0];
  const sheet = name ? book.Sheets[name] : undefined;
  if (!sheet) return { format: "xlsx", rows: [], missingColumns: COLUMNS.filter((c) => c.required).map((c) => c.key) };
  if ((sheet["!merges"] ?? []).length > 0) {
    return { format: "xlsx", sheet: name, rows: [], missingColumns: [], fileError: "mergedCells" };
  }
  const table = XLSX.utils.sheet_to_json<string[]>(sheet, { header: 1, raw: false, defval: "", blankrows: false });
  return tableToImport(table, "xlsx", { sheet: name });
}

export const isTableFile = (name: string) => /\.(csv|tsv|xlsx|xlsm|xls)$/i.test(name);

/** A file from the picker: `.xlsx` through SheetJS, `.csv` decoded and parsed, anything
 *  else returned as text for the text box. */
export async function readFile(file: File): Promise<{ parsed: ParsedImport } | { text: string }> {
  const bytes = new Uint8Array(await file.arrayBuffer());
  if (/\.(xlsx|xlsm|xls)$/i.test(file.name)) return { parsed: readWorkbook(await loadXlsx(), bytes) };
  const { text, encoding } = decodeBytes(bytes);
  if (/\.(csv|tsv)$/i.test(file.name)) return { parsed: tableToImport(parseCsvText(text), "csv", { encoding }) };
  return { text };
}

/* ── checking ────────────────────────────────────────────────────────────── */

export type IssueCode =
  | "emptyId"
  | "duplicateId"
  | "emptyInput" // server: golden.empty_input
  | "emptyExpected" // server: golden.empty_expected
  | "noTier"
  | "badTier"
  | "badSplit"
  | "unknownCriteria" // server: golden.unknown_criteria
  | "notObject"
  | "noCriteria"
  | "sameAsInput";
export type FileIssueCode = "missingColumns" | "mergedCells" | "noRows" | "tooMany" | "splitFull" | "thin";

export interface Issue<C extends string = IssueCode> {
  code: C;
  level: "error" | "warn";
  /** What to name in the message: the bad value, the unknown ids, the split, … */
  detail?: string;
}

export interface CheckedRow extends ImportRow {
  issues: Issue[];
  /** The item as it will be sent: dialog tier filled in, `split` only when seeding. */
  final: GoldenItem;
  tier: CaseTier | null;
}

export interface ImportCheck {
  rows: CheckedRow[];
  fileIssues: Issue<FileIssueCode>[];
  ok: number;
  errorRows: number;
  warnRows: number;
  /** Seeding only: how many items each split would receive (the server's stratification). */
  projected: Record<GoldenSplit, number> | null;
  /** Any error at all — the submit button stays disabled. */
  blocked: boolean;
  items: GoldenItem[];
}

export interface CheckOptions {
  mode: "seed" | "add";
  /** The dialog's case tier, for items that name none — JSON and `|` text only; a table must say. */
  fallbackTier: CaseTier;
  /** Criterion keys of the served table; `null` = not known (no lineage) — not checked. */
  known: Set<string> | null;
  /** Adding: items already in the target split (retired ones count towards the cap too). */
  existingItems?: number;
  /** Adding: items each criterion already has in the set, for the "too few" warning. */
  existingCriteria?: Record<string, number>;
}

const blank = (v: unknown) => v == null || (typeof v === "string" && v.trim() === "");
const turnInput = (v: unknown) => (typeof v === "string" ? v : v == null ? "" : JSON.stringify(v));

/**
 * The item a dialog fallback tier produces. An item that already has its own
 * `metadata.dlc` (a golden-set JSON file: case_tier, criteria_ids, split) keeps it —
 * overwriting it put every item of a seeded file in one tier and dropped its criteria
 * links, so the coverage matrix and the stratified holdout were wrong.
 */
export function withCaseTier(item: GoldenItem, caseTier: CaseTier): GoldenItem {
  const meta = (item.metadata ?? {}) as Record<string, unknown>;
  const dlc = (meta.dlc ?? {}) as Record<string, unknown>;
  return { ...item, metadata: { ...meta, dlc: { case_tier: caseTier, ...dlc } } };
}

/** The server's seed stratification, counted: named splits as named, the rest of each
 *  case tier split by `shares` (largest share first, by position). */
export function projectSeed(items: GoldenItem[], shares: [number, number, number] = SEED_SHARES): Record<GoldenSplit, number> {
  const counts: Record<GoldenSplit, number> = { dev: 0, regression: 0, holdout: 0 };
  const byTier = new Map<string, number>();
  for (const item of items) {
    if (item.split && SPLITS.includes(item.split)) counts[item.split] += 1;
    else {
      const tier = String(item.metadata?.dlc?.case_tier || "known_good");
      byTier.set(tier, (byTier.get(tier) ?? 0) + 1);
    }
  }
  const order = SPLITS.map((s, i) => [s, shares[i]] as const).sort((a, b) => b[1] - a[1]).map(([s]) => s);
  for (const n of byTier.values()) {
    for (let index = 0; index < n; index++) {
      const position = (index + 0.5) / n;
      let cumulative = 0;
      for (const name of order) {
        cumulative += shares[SPLITS.indexOf(name)];
        if (position < cumulative || name === order[order.length - 1]) {
          counts[name] += 1;
          break;
        }
      }
    }
  }
  return counts;
}

/** Every row against §4's rules, plus the whole-file ones. Nothing is sent from here. */
export function checkImport(parsed: ParsedImport, opts: CheckOptions): ImportCheck {
  const tabular = parsed.format !== "json" && parsed.format !== "text";
  const seen = new Map<string, number>();
  for (const row of parsed.rows) {
    const id = String(row.item.scenario_id ?? "").trim();
    if (id) seen.set(id, (seen.get(id) ?? 0) + 1);
  }
  const rows: CheckedRow[] = parsed.rows.map((row) => {
    const issues: Issue[] = [];
    const err = (code: IssueCode, detail?: string) => issues.push({ code, level: "error", detail });
    const warn = (code: IssueCode, detail?: string) => issues.push({ code, level: "warn", detail });
    if (row.notObject) {
      err("notObject");
      return { ...row, issues, final: row.item, tier: null };
    }
    const id = String(row.item.scenario_id ?? "").trim();
    if (!id) err("emptyId");
    else if ((seen.get(id) ?? 0) > 1) err("duplicateId", id);
    const turns = Array.isArray(row.item.turns) ? row.item.turns : [];
    if (turns.length === 0 || turns.some((t) => t == null || typeof t !== "object" || blank(t.input))) err("emptyInput");
    else if (turns.some((t) => blank(t.expected_response))) err("emptyExpected");
    else if (turns.some((t) => turnInput(t.input).trim() === String(t.expected_response).trim())) warn("sameAsInput");
    // a table must name the tier; a JSON item or a `|` line falls back to the dialog's
    let final = tabular ? row.item : withCaseTier(row.item, opts.fallbackTier);
    const rawTier = final.metadata?.dlc?.case_tier;
    let tier: CaseTier | null = null;
    if (row.badTier) err("badTier", row.badTier);
    else if (blank(rawTier)) err("noTier");
    else if (!(CASE_TIERS as readonly string[]).includes(String(rawTier))) err("badTier", String(rawTier));
    else tier = rawTier as CaseTier;
    if (row.badSplit) err("badSplit", row.badSplit);
    else if (opts.mode === "seed" && final.split != null && !SPLITS.includes(final.split)) err("badSplit", String(final.split));
    const criteria = (final.metadata?.dlc?.criteria_ids ?? []).map(String);
    if (criteria.length === 0) warn("noCriteria");
    else if (opts.known) {
      const unknown = criteria.filter((k) => !opts.known!.has(k));
      if (unknown.length) err("unknownCriteria", unknown.join(", "));
    }
    if (opts.mode === "add" && "split" in final) {
      // the target split is the dialog's; a `split` column only means something when seeding
      const { split: _drop, ...rest } = final;
      void _drop;
      final = rest;
    }
    return { ...row, issues, final, tier };
  });

  const fileIssues: Issue<FileIssueCode>[] = [];
  if (parsed.fileError) fileIssues.push({ code: parsed.fileError, level: "error" });
  if (parsed.missingColumns.length) {
    const names = parsed.missingColumns.map((k) => `${k} / ${COLUMNS.find((c) => c.key === k)!.zh}`);
    fileIssues.push({ code: "missingColumns", level: "error", detail: names.join(", ") });
  }
  if (!parsed.fileError && rows.length === 0) fileIssues.push({ code: "noRows", level: "error" });
  const cap = opts.mode === "seed" ? MAX_SEED_ITEMS : MAX_ADD_ITEMS;
  if (rows.length > cap) fileIssues.push({ code: "tooMany", level: "error", detail: String(cap) });
  const items = rows.map((r) => r.final);
  const projected = opts.mode === "seed" ? projectSeed(items) : null;
  if (projected) {
    for (const name of SPLITS) {
      if (projected[name] > MAX_SPLIT_ITEMS) fileIssues.push({ code: "splitFull", level: "error", detail: name });
    }
  } else if ((opts.existingItems ?? 0) + rows.length > MAX_SPLIT_ITEMS) {
    fileIssues.push({ code: "splitFull", level: "error" });
  }
  // a criterion with fewer than three items reports noise, not a rate
  const perCriterion = new Map<string, number>(Object.entries(opts.existingCriteria ?? {}));
  for (const r of rows) {
    for (const k of new Set((r.final.metadata?.dlc?.criteria_ids ?? []).map(String))) {
      if (!opts.known || opts.known.has(k)) perCriterion.set(k, (perCriterion.get(k) ?? 0) + 1);
    }
  }
  const thin = [...perCriterion].filter(([, n]) => n > 0 && n < THIN_BELOW).map(([k]) => k).sort();
  if (thin.length) fileIssues.push({ code: "thin", level: "warn", detail: thin.join(", ") });

  const errorRows = rows.filter((r) => r.issues.some((i) => i.level === "error")).length;
  const warnRows = rows.filter((r) => r.issues.length > 0 && r.issues.every((i) => i.level === "warn")).length;
  return {
    rows,
    fileIssues,
    ok: rows.length - errorRows,
    errorRows,
    warnRows,
    projected,
    blocked: errorRows > 0 || fileIssues.some((i) => i.level === "error"),
    items,
  };
}

/* ── items back into a table ─────────────────────────────────────────────── */

export interface Table {
  header: string[];
  rows: string[][];
}

const DLC_COLUMNS: Column[] = ["scenario_id", "input", "expected_response", "case_tier", "criteria_ids", "split", "expected_source"];

/** A list cell. One- and zero-element lists keep a `;` so they read back as lists. */
function listCell(values: unknown[]): string {
  const parts = values.map((v) => String(v));
  return parts.length < 2 ? `${parts.join("")};` : parts.join(";");
}

function cellOf(value: unknown): string {
  if (value == null) return "";
  if (Array.isArray(value)) return listCell(value);
  return typeof value === "object" ? JSON.stringify(value) : String(value);
}

/** Items as the §2.1 table — the template, the export, and what imports back to the same
 *  items. Column names are Chinese or English (`zh`); either reads back. Multi-turn
 *  items export their first turn only (tables are single-turn, D-2). */
export function itemsToTable(items: (GoldenItem & { split?: GoldenSplit })[], opts: { zh?: boolean; withSplit?: boolean } = {}): Table {
  const cols = DLC_COLUMNS.filter((c) => c !== "split" || opts.withSplit);
  const extra = [...new Set(items.flatMap((i) => Object.keys(i.metadata ?? {}).filter((k) => k !== "dlc")))].sort();
  const name = (c: Column) => (opts.zh ? COLUMNS.find((x) => x.key === c)!.zh : c);
  const rows = items.map((item) => {
    const dlc = item.metadata?.dlc ?? {};
    const turn = item.turns?.[0] ?? {};
    const value: Record<Column, string> = {
      scenario_id: String(item.scenario_id ?? ""),
      input: turnInput(turn.input),
      expected_response: String(turn.expected_response ?? ""),
      case_tier: String(dlc.case_tier ?? ""),
      criteria_ids: (dlc.criteria_ids ?? []).join(";"),
      split: String(item.split ?? ""),
      expected_source: String(dlc.expected_source ?? ""),
    };
    const meta = (item.metadata ?? {}) as Record<string, unknown>;
    return [...cols.map((c) => value[c]), ...extra.map((k) => cellOf(meta[k]))];
  });
  return { header: [...cols.map(name), ...extra], rows };
}

/** A sheet to write: cells, optional per-header comments, column widths. */
export interface SheetSpec {
  name: string;
  table: Table;
  /** Header text → comment shown on that header cell. */
  notes?: Record<string, string>;
  widths?: number[];
}

/** Write an `.xlsx` and download it. Every cell is text (`@`), so Excel does not turn
 *  an id like `1-1` into a date. */
export async function downloadXlsx(filename: string, sheets: SheetSpec[]): Promise<void> {
  const XLSX = await loadXlsx();
  XLSX.writeFile(buildWorkbook(XLSX, sheets), filename);
}

export function buildWorkbook(XLSX: Xlsx, sheets: SheetSpec[]) {
  const book = XLSX.utils.book_new();
  for (const spec of sheets) {
    const sheet = XLSX.utils.aoa_to_sheet([spec.table.header, ...spec.table.rows]);
    for (const address of Object.keys(sheet)) {
      if (address.startsWith("!")) continue;
      const cell = sheet[address] as { t: string; z?: string };
      if (cell.t === "s") cell.z = "@";
    }
    spec.table.header.forEach((h, col) => {
      const note = spec.notes?.[h];
      if (!note) return;
      const cell = sheet[XLSX.utils.encode_cell({ r: 0, c: col })] as { c?: { a?: string; t: string }[] & { hidden?: boolean } };
      cell.c = Object.assign([{ a: "Launchpad", t: note }], { hidden: true });
    });
    if (spec.widths) sheet["!cols"] = spec.widths.map((wch) => ({ wch }));
    XLSX.utils.book_append_sheet(book, sheet, spec.name);
  }
  return book;
}

/** The error report: the rows as read plus one column saying what is wrong. Tables keep
 *  their own columns; JSON and `|` text are rendered as the §2.1 table. */
export function errorReport(parsed: ParsedImport, check: ImportCheck, describe: (issue: Issue) => string, errorColumn: string): Table {
  const text = (r: CheckedRow) => r.issues.map(describe).join("; ");
  if (parsed.header && parsed.header.length) {
    return {
      header: ["#", ...parsed.header, errorColumn],
      rows: check.rows.map((r) => [String(r.line), ...parsed.header!.map((_, i) => r.cells?.[i] ?? ""), text(r)]),
    };
  }
  const table = itemsToTable(check.rows.map((r) => r.final), { withSplit: true });
  return {
    header: ["#", ...table.header, errorColumn],
    rows: table.rows.map((cells, i) => [String(check.rows[i].line), ...cells, text(check.rows[i])]),
  };
}
