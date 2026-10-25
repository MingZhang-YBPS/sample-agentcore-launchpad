// The new-agent chooser (storyline-ux §3.6): three questions → one recommended
// way to build, at most one alternative, and the defaults the matching wizard
// starts from. The decision table is a code constant, not backend data, so it
// ships with the console and is covered row by row in chooser.test.ts.

/** Every way /v3/create offers that the chooser can recommend. System presets
 *  and the template marketplace are "start from something ready", outside it. */
export type WayKey = "harness" | "strands" | "sdk" | "byoc" | "mcp" | "assistant" | "studio" | "import";

/** Q1 — what do you have right now. */
export type Have = "nothing" | "brief" | "code" | "mcp" | "runtime";
/** Q2 — what is it for. */
export type Use = "qa" | "tools" | "multi" | "tool";
/** Q3 — evaluation, A/B and canary later (the full Agent-DLC), or just trying. */
export type Dlc = "full" | "try";

export const HAVE: Have[] = ["nothing", "brief", "code", "mcp", "runtime"];
export const USE: Use[] = ["qa", "tools", "multi", "tool"];
export const DLC: Dlc[] = ["full", "try"];
export type Question = "have" | "use" | "dlc";
export const QUESTIONS: Question[] = ["have", "use", "dlc"];

export interface Answers {
  have?: Have;
  /** with `have: "code"` — the code is written with the Claude Agent SDK or another agent SDK */
  sdk?: boolean;
  use?: Use;
  dlc?: Dlc;
}

/** What the primary button hands the wizard as its starting values. */
export interface WayDefaults {
  /** a Q&A assistant answers from documents: preselect a knowledge base */
  kb: boolean;
  /** serving other agents: the Strands A2A protocol */
  protocol?: "a2a";
}

export type RowKey = "runtime" | "mcp" | "codeSdk" | "code" | "brief" | "multi" | "tool" | "full" | "try";

export interface Recommendation {
  /** the decision-table row that matched; its copy is v3.create.chooser.row.<row>.* */
  row: RowKey;
  primary: WayKey;
  alternative: WayKey | null;
  defaults: WayDefaults;
}

interface Row {
  key: RowKey;
  /** the questions this row needs answered before it can match */
  needs: Question[];
  match: (a: Answers) => boolean;
  primary: WayKey;
  alternative: WayKey | null;
}

/**
 * The design table, first match wins. What the member already has decides
 * first (code, an MCP server or a running Runtime each have one home); with
 * nothing in hand the purpose and the DLC question choose between the
 * no-code Harness and the platform-generated Strands agent.
 */
export const DECISION_TABLE: readonly Row[] = [
  { key: "runtime", needs: ["have"], match: (a) => a.have === "runtime", primary: "import", alternative: null },
  { key: "mcp", needs: ["have"], match: (a) => a.have === "mcp", primary: "mcp", alternative: null },
  { key: "codeSdk", needs: ["have"], match: (a) => a.have === "code" && !!a.sdk, primary: "sdk", alternative: null },
  { key: "code", needs: ["have"], match: (a) => a.have === "code", primary: "byoc", alternative: null },
  { key: "brief", needs: ["have"], match: (a) => a.have === "brief", primary: "assistant", alternative: "strands" },
  { key: "multi", needs: ["have", "use"], match: (a) => a.have === "nothing" && a.use === "multi", primary: "studio", alternative: "strands" },
  // not a row of the design table: with nothing in hand, a tool for other agents
  // is a generated Strands agent on A2A; writing an MCP server is the other road
  { key: "tool", needs: ["have", "use"], match: (a) => a.have === "nothing" && a.use === "tool", primary: "strands", alternative: "mcp" },
  { key: "full", needs: ["have", "use", "dlc"], match: (a) => a.have === "nothing" && a.dlc === "full", primary: "strands", alternative: "harness" },
  { key: "try", needs: ["have", "use", "dlc"], match: (a) => a.have === "nothing" && a.dlc === "try", primary: "harness", alternative: "assistant" },
];

const answered = (a: Answers, q: Question) => a[q] !== undefined;

/** The recommendation for the answers so far, or null while it is undecided. */
export function recommend(a: Answers): Recommendation | null {
  for (const row of DECISION_TABLE) {
    if (!row.needs.every((q) => answered(a, q))) continue;
    if (!row.match(a)) continue;
    return {
      row: row.key,
      primary: row.primary,
      alternative: row.alternative,
      defaults: { kb: a.use === "qa", ...(row.key === "tool" ? { protocol: "a2a" as const } : {}) },
    };
  }
  return null;
}

/** The questions whose answer can still change the recommended way. The page
 *  marks the others as "does not change the recommendation". */
export function relevant(a: Answers): Set<Question> {
  if (a.have && a.have !== "nothing") return new Set(["have"]);
  if (a.have === "nothing" && (a.use === "multi" || a.use === "tool")) return new Set(["have", "use"]);
  return new Set(QUESTIONS);
}

/** Where a way's button lands, carrying the answers as the wizard's defaults. */
export function wayHref(way: WayKey, defaults: WayDefaults = { kb: false }): string {
  const extra = (method: string) => {
    const q = new URLSearchParams({ view: "new", method });
    if (defaults.kb) q.set("kb", "1");
    if (defaults.protocol && method === "zip_runtime") q.set("protocol", defaults.protocol);
    return `/v2/agents?${q.toString()}`;
  };
  switch (way) {
    // the managed Harness quick form lives on /v3/create itself
    case "harness":
      return defaults.kb ? "/v3/create?quick=1&kb=1" : "/v3/create?quick=1";
    case "strands":
      return extra("zip_runtime");
    case "sdk":
      return extra("container");
    case "byoc":
      return "/v2/agents?view=new&method=byoc";
    case "mcp":
      return "/v2/agents?view=new&method=byoc&protocol=mcp";
    case "assistant":
      return "/v3/assistant";
    case "studio":
      return "/create/studio";
    case "import":
      return "/agents/import";
  }
}

/* ── capability comparison (workshop 20-build §0) ─────────────────────────── */

/** The four build methods the workshop compares. */
export type CapColumn = "harness" | "strands" | "sdk" | "byoc";
export const CAP_COLUMNS: CapColumn[] = ["harness", "strands", "sdk", "byoc"];
/** The workshop table's rows, then the Strands builder options (strands-builder
 *  §2, SB-1 and SB-2): what only a platform-generated Strands agent configures in
 *  the wizard — the other methods say "not supported" plus the way out, or that
 *  their own code has to do it. */
export type CapBaseRow = "artifact" | "kb" | "skills" | "ab" | "model" | "observability";
export type CapBuilderRow =
  | "builtinTools" | "modelParams" | "conversation" | "structured" | "promptTemplate"
  | "agentsAsTools" | "providers" | "toolHooks" | "retry";
export type CapRow = CapBaseRow | CapBuilderRow;
export const CAP_BASE_ROWS: CapBaseRow[] = ["artifact", "kb", "skills", "ab", "model", "observability"];
export const CAP_BUILDER_ROWS: CapBuilderRow[] = [
  // SB-1
  "builtinTools", "modelParams", "conversation", "structured", "promptTemplate",
  // SB-2
  "agentsAsTools", "providers", "toolHooks", "retry",
];
export const CAP_ROWS: CapRow[] = [...CAP_BASE_ROWS, ...CAP_BUILDER_ROWS];
/** yes · partial (works with limits) · no (the cell text names the alternative) · info (a fact, not a verdict) */
export type CapLevel = "yes" | "partial" | "no" | "info";

export const CAPABILITIES: Record<CapRow, Record<CapColumn, CapLevel>> = {
  artifact: { harness: "info", strands: "info", sdk: "info", byoc: "info" },
  kb: { harness: "yes", strands: "yes", sdk: "yes", byoc: "no" },
  skills: { harness: "yes", strands: "yes", sdk: "yes", byoc: "no" },
  ab: { harness: "no", strands: "yes", sdk: "no", byoc: "partial" },
  model: { harness: "yes", strands: "yes", sdk: "partial", byoc: "partial" },
  observability: { harness: "yes", strands: "yes", sdk: "yes", byoc: "no" },
  builtinTools: { harness: "no", strands: "yes", sdk: "no", byoc: "partial" },
  modelParams: { harness: "partial", strands: "yes", sdk: "no", byoc: "partial" },
  conversation: { harness: "no", strands: "yes", sdk: "no", byoc: "partial" },
  structured: { harness: "no", strands: "yes", sdk: "no", byoc: "partial" },
  promptTemplate: { harness: "no", strands: "yes", sdk: "no", byoc: "partial" },
  agentsAsTools: { harness: "no", strands: "yes", sdk: "no", byoc: "partial" },
  providers: { harness: "partial", strands: "yes", sdk: "no", byoc: "partial" },
  toolHooks: { harness: "no", strands: "yes", sdk: "no", byoc: "partial" },
  retry: { harness: "info", strands: "yes", sdk: "info", byoc: "partial" },
};

/** The comparison column a recommended way is built as (Studio generates
 *  Strands code, an MCP server is bring-your-own code); null when the way is not
 *  a build method of its own (the assistant proposes one, import adopts one). */
export function capColumn(way: WayKey): CapColumn | null {
  switch (way) {
    case "harness":
    case "strands":
    case "sdk":
    case "byoc":
      return way;
    case "studio":
      return "strands";
    case "mcp":
      return "byoc";
    default:
      return null;
  }
}

/* ── "skip, show all ways" ─────────────────────────────────────────────────── */

export const SKIP_KEY = "lp.v3.create.chooser";

/** Whether the member chose to skip the chooser on this browser. */
export function readSkipped(store: Pick<Storage, "getItem"> | null | undefined): boolean {
  try {
    return store?.getItem(SKIP_KEY) === "skip";
  } catch {
    return false;
  }
}

export function writeSkipped(store: Pick<Storage, "setItem" | "removeItem"> | null | undefined, skipped: boolean): void {
  try {
    if (skipped) store?.setItem(SKIP_KEY, "skip");
    else store?.removeItem(SKIP_KEY);
  } catch {
    /* private mode or a full quota: the choice just is not remembered */
  }
}

/** Every i18n key the chooser renders, for the locale completeness test. */
export function chooserKeys(): string[] {
  const p = "v3.create.chooser";
  const keys = [
    ...["title", "sub", "skip", "reopen", "reopenSub", "pending", "pendingSub", "recommended", "why", "cost", "alternative", "useThis", "notNeeded",
      "compare", "compareSub", "compareNone", "ready", "readySub", "presets", "presetsSub", "market", "marketSub", "sdk", "sdkSub",
      "capHead", "capBuilder", "capBuilderSub", "level.yes", "level.partial", "level.no", "kbPreset"].map((k) => `${p}.${k}`),
    ...QUESTIONS.map((q) => `${p}.q.${q}`),
    ...HAVE.flatMap((v) => [`${p}.have.${v}`, `${p}.have.${v}Sub`]),
    ...USE.flatMap((v) => [`${p}.use.${v}`, `${p}.use.${v}Sub`]),
    ...DLC.flatMap((v) => [`${p}.dlc.${v}`, `${p}.dlc.${v}Sub`]),
    ...DECISION_TABLE.flatMap((r) => [`${p}.row.${r.key}.why`, `${p}.row.${r.key}.cost`, ...(r.alternative ? [`${p}.row.${r.key}.alt`] : [])]),
    ...(["harness", "strands", "sdk", "byoc", "mcp", "assistant", "studio", "import"] as WayKey[]).map((w) => `${p}.way.${w}`),
    ...CAP_COLUMNS.map((c) => `${p}.col.${c}`),
    ...CAP_ROWS.flatMap((r) => [`${p}.cap.${r}`, ...CAP_COLUMNS.map((c) => `${p}.cell.${r}.${c}`)]),
  ];
  return keys;
}
