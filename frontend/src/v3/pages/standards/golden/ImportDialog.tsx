/**
 * Seed / add items (golden-import G1–G3). A file or the text box is parsed in the
 * browser, every row is checked and shown before anything is sent, and seeding asks a
 * second time — with the projected split sizes — because the holdout it writes is
 * sealed for good. JSON and `id | question | expected` text go through the same
 * preview, so the old formats get the checks too.
 */
import { Download } from "lucide-react";
import { type ReactNode, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";

import { ErrorCallout } from "../../../../components/ErrorCallout";
import { errorMessage } from "../../../../lib/api";
import { CASE_TIERS, type CaseTier, type GoldenItem, type GoldenSet, type GoldenSplit, type WritableSplit } from "../../../../lib/dlc";
import {
  checkImport,
  errorReport,
  type FileIssueCode,
  type Issue,
  type ParsedImport,
  parseText,
  readFile,
} from "../../../../lib/goldenTable";
import { downloadCsv } from "../../../../v2/format";
import { useFailure } from "../../../failure";
import { useLoad } from "../../../hooks";
import { Btn, Dialog, Notice } from "../../../ui";
import { NO_CRITERIA, loadGoldenItems, loadServedCriteria } from "./served";
import { downloadItems, downloadTemplate, fileStem, type TableFormat } from "./tables";

const K = "v2.dlc.golden.import";
const clip = (s: string, n = 48) => (s.length > n ? `${s.slice(0, n)}…` : s);

export function ImportDialog({
  mode,
  split,
  goldenSet,
  title,
  hint,
  busy,
  extra,
  onClose,
  onSubmit,
}: {
  mode: "seed" | "add";
  /** Adding: the target split (its size counts towards the 200-item cap). */
  split?: WritableSplit;
  goldenSet: GoldenSet;
  title: string;
  hint: string;
  busy: boolean;
  extra?: ReactNode;
  onClose: () => void;
  /** Items as they should be sent: dialog tier filled in, rows checked. */
  onSubmit: (items: GoldenItem[]) => void;
}) {
  const { t, i18n } = useTranslation();
  const failed = useFailure();
  const zh = (i18n.resolvedLanguage ?? "en").startsWith("zh");
  const lineage = goldenSet.criteria_lineage_id;
  const served = useLoad(() => loadServedCriteria(lineage), `v3-golden-criteria:${lineage ?? ""}`);
  const criteria = served.data ?? NO_CRITERIA;
  const [text, setText] = useState("");
  const [file, setFile] = useState<{ name: string; parsed: ParsedImport } | null>(null);
  const [caseTier, setCaseTier] = useState<CaseTier>("known_bad");
  const [onlyErrors, setOnlyErrors] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [sealChecked, setSealChecked] = useState(false);
  const [downloading, setDownloading] = useState(false);

  const source = useMemo((): { parsed: ParsedImport | null; error: string | null } => {
    if (file) return { parsed: file.parsed, error: null };
    if (!text.trim()) return { parsed: null, error: null };
    try {
      return { parsed: parseText(text), error: null };
    } catch (err) {
      return { parsed: null, error: errorMessage(err) };
    }
  }, [file, text]);

  const existingCriteria = useMemo(
    () => Object.fromEntries((goldenSet.coverage?.criteria ?? []).map((row) => [row.key, row.total])),
    [goldenSet.coverage],
  );
  const check = useMemo(
    () =>
      source.parsed &&
      checkImport(source.parsed, {
        mode,
        fallbackTier: caseTier,
        // still loading: do not flag every id as unknown; the server checks again anyway
        known: served.loading ? null : criteria.known,
        existingItems: split ? goldenSet.splits[split]?.items ?? 0 : 0,
        existingCriteria: mode === "add" ? existingCriteria : {},
      }),
    [source.parsed, mode, caseTier, served.loading, criteria.known, split, goldenSet.splits, existingCriteria],
  );

  const splitLabel = (name: string | undefined) => (name ? t(`v2.dlc.split.${name}`) : t(`v2.dlc.split.${split ?? "dev"}`));
  const describe = (issue: Issue) => t(`${K}.rule.${issue.code}`, { detail: issue.detail ?? "" });
  const describeFile = (issue: Issue<FileIssueCode>) =>
    t(`${K}.file.${issue.code}`, { detail: issue.code === "splitFull" ? splitLabel(issue.detail) : issue.detail ?? "" });

  const download = (fn: () => Promise<void>) => {
    setDownloading(true);
    fn().catch((err: unknown) => failed.fail(err)).finally(() => setDownloading(false));
  };
  const template = (format: TableFormat) => download(() => downloadTemplate(format, goldenSet.name, t, criteria, zh));
  const hasItems = Object.values(goldenSet.splits).some((s) => (s?.items ?? 0) > 0);
  const exportItems = (format: TableFormat) =>
    download(async () => {
      const items = await loadGoldenItems(goldenSet);
      if (items.length === 0) throw new Error(t(`${K}.exportEmpty`));
      await downloadItems(format, goldenSet.name, items, t, criteria, zh);
    });

  const report = () => {
    if (!source.parsed || !check) return;
    const table = errorReport(source.parsed, check, describe, t(`${K}.errorColumn`));
    downloadCsv(`${fileStem(goldenSet.name)}-errors.csv`, table.header, table.rows);
  };

  const rows = check ? (onlyErrors ? check.rows.filter((r) => r.issues.some((i) => i.level === "error")) : check.rows) : [];
  const projected = check?.projected;
  const projectedText = projected && t(`${K}.projected`, { dev: projected.dev, regression: projected.regression, holdout: projected.holdout });

  if (confirming && check && projected) {
    return (
      <Dialog
        title={t(`${K}.sealConfirmTitle`)}
        onClose={onClose}
        foot={
          <>
            <Btn kind="ghost" onClick={() => setConfirming(false)}>{t(`${K}.back`)}</Btn>
            <Btn kind="danger" busy={busy} disabled={!sealChecked} disabledReason={t("story.reason.confirmFirst")} onClick={() => onSubmit(check.items)}>{t(`${K}.submitSeed`)}</Btn>
          </>
        }
      >
        <div style={{ display: "grid", gap: 12 }}>
          <ErrorCallout error={failed.error} params={{ dataset_id: goldenSet.id }} onDismiss={failed.clear} data-testid="v3-golden-import-error" />
          <Notice s="wait">{t(`${K}.sealConfirmBody`)}</Notice>
          <p style={{ margin: 0 }}><b>{t(`${K}.okCount`, { n: check.items.length })}</b> · {projectedText}</p>
          <label style={{ display: "flex", gap: 8, alignItems: "flex-start" }}>
            <input type="checkbox" checked={sealChecked} onChange={(e) => setSealChecked(e.target.checked)} data-testid="v3-golden-seal-check" />
            <span>{t(`${K}.sealConfirmCheck`)}</span>
          </label>
        </div>
      </Dialog>
    );
  }

  return (
    <Dialog
      wide
      title={title}
      onClose={onClose}
      foot={
        <>
          <Btn kind="ghost" onClick={onClose}>{t("v3.common.cancel")}</Btn>
          {check && check.blocked && check.rows.length > 0 && (
            <Btn onClick={report}><Download size={13} /> {t(`${K}.errorReport`)}</Btn>
          )}
          <Btn kind="primary" busy={busy} disabled={!check || check.blocked}
            disabledReason={!check ? t("story.reason.addItems") : t("story.reason.fixErrors")}
            onClick={() => {
              if (!check) return;
              if (mode === "seed") {
                setSealChecked(false);
                setConfirming(true);
              } else onSubmit(check.items);
            }}>
            {mode === "seed" ? t(`${K}.submitSeed`) : t(`${K}.submitAdd`, { n: check?.items.length ?? 0 })}
          </Btn>
        </>
      }
    >
      <div style={{ display: "grid", gap: 12 }}>
        <ErrorCallout error={failed.error} params={{ dataset_id: goldenSet.id }} onDismiss={failed.clear} data-testid="v3-golden-import-error" />
        <p style={{ margin: 0 }}>{hint}</p>
        {extra}
        <div style={{ display: "flex", gap: 6, alignItems: "center", flexWrap: "wrap" }}>
          <span className="v3-std-muted">{t(`${K}.template`)}</span>
          {(["xlsx", "csv", "json"] as TableFormat[]).map((f) => (
            <Btn key={f} size="sm" kind="ghost" disabled={downloading || (Boolean(lineage) && served.loading)} onClick={() => template(f)}>
              <Download size={12} /> {t(`${K}.template${f === "xlsx" ? "Xlsx" : f === "csv" ? "Csv" : "Json"}`)}
            </Btn>
          ))}
          {hasItems && (
            <>
              <span className="v3-std-muted" style={{ marginLeft: 12 }}>{t(`${K}.export`)}</span>
              {(["xlsx", "csv"] as TableFormat[]).map((f) => (
                <Btn key={f} size="sm" kind="ghost" disabled={downloading} onClick={() => exportItems(f)}>
                  <Download size={12} /> {t(`${K}.template${f === "xlsx" ? "Xlsx" : "Csv"}`)}
                </Btn>
              ))}
            </>
          )}
        </div>
        <label className="v3-field">
          <span>{t("v2.dlc.golden.caseTier")}</span>
          <select className="v3-select" value={caseTier} onChange={(e) => setCaseTier(e.target.value as CaseTier)}>
            {CASE_TIERS.map((c) => <option key={c} value={c}>{t(`v2.dlc.caseTier.${c}`)}</option>)}
          </select>
          <small className="v3-hint">{t(`${K}.tierFallback`)}</small>
        </label>
        <label className="v3-field">
          <span>{t("v2.dlc.golden.itemsField")}</span>
          {file ? (
            <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
              <span className="mono">{file.name}</span>
              <Btn size="sm" kind="ghost" onClick={() => setFile(null)}>{t(`${K}.clearFile`)}</Btn>
            </div>
          ) : (
            <textarea className="v3-input mono" rows={8} value={text} onChange={(e) => setText(e.target.value)} />
          )}
          <input type="file" data-testid="v3-golden-file"
            accept=".xlsx,.xls,.csv,.tsv,.json,.txt,application/json,text/plain,text/csv"
            onChange={(e) => {
              const picked = e.target.files?.[0];
              e.target.value = "";
              if (!picked) return;
              readFile(picked)
                .then((out) => {
                  if ("parsed" in out) setFile({ name: picked.name, parsed: out.parsed });
                  else {
                    setFile(null);
                    setText(out.text);
                  }
                })
                .catch((err: unknown) => failed.fail(err));
            }} />
          <small className={source.error ? "v3-err" : "v3-hint"}>{source.error ?? `${t(`${K}.formats`)} ${t("v2.dlc.golden.itemsHint")}`}</small>
          {lineage && served.error && <small className="v3-hint">{t(`${K}.criteriaUnavailable`)}</small>}
        </label>

        {check && source.parsed && (
          <div style={{ display: "grid", gap: 8 }} data-testid="v3-golden-preview">
            <div style={{ display: "flex", gap: 12, alignItems: "center", flexWrap: "wrap" }}>
              <b>{t(`${K}.preview`)}</b>
              <span>
                {t(`${K}.readRows`, { n: check.rows.length })}
                {source.parsed.encoding && ` · ${t(`${K}.encoding`, { encoding: source.parsed.encoding })}`}
                {source.parsed.sheet && ` · ${t(`${K}.sheet`, { sheet: source.parsed.sheet })}`}
              </span>
              <span style={{ color: "var(--v3-ok)" }}>✓ {t(`${K}.okCount`, { n: check.ok })}</span>
              {check.errorRows > 0 && <span className="v3-err">✗ {t(`${K}.errorCount`, { n: check.errorRows })}</span>}
              {check.warnRows > 0 && <span style={{ color: "var(--v3-wait)" }}>! {t(`${K}.warnCount`, { n: check.warnRows })}</span>}
              {check.errorRows > 0 && (
                <label style={{ display: "inline-flex", gap: 6, alignItems: "center", marginLeft: "auto" }}>
                  <input type="checkbox" checked={onlyErrors} onChange={(e) => setOnlyErrors(e.target.checked)} />
                  {t(`${K}.onlyErrors`)}
                </label>
              )}
            </div>
            {check.fileIssues.map((issue) => (
              <Notice key={issue.code} s={issue.level === "error" ? "act" : "wait"}>
                {describeFile(issue)}
                {issue.code === "missingColumns" && ` ${t(`${K}.useTemplate`)}`}
              </Notice>
            ))}
            {projectedText && <small className="v3-hint">{projectedText}</small>}
            {rows.length > 0 && (
              <div style={{ maxHeight: 320, overflow: "auto" }}>
                <table className="v3-table">
                  <thead>
                    <tr>
                      <th className="num">{t(`${K}.col.line`)}</th>
                      <th>{t(`${K}.col.id`)}</th>
                      <th>{t(`${K}.col.question`)}</th>
                      <th>{t(`${K}.col.tier`)}</th>
                      <th>{t(`${K}.col.criteria`)}</th>
                      {mode === "seed" && <th>{t(`${K}.col.split`)}</th>}
                      <th>{t(`${K}.col.status`)}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {rows.map((row) => {
                      const errors = row.issues.filter((i) => i.level === "error");
                      const warns = row.issues.filter((i) => i.level === "warn");
                      const input = row.final.turns?.[0]?.input;
                      return (
                        <tr key={row.line}>
                          <td className="num">{row.line}</td>
                          <td className="mono">{row.final.scenario_id || "—"}</td>
                          <td title={typeof input === "string" ? input : undefined}>{clip(typeof input === "string" ? input : "")}</td>
                          <td>{row.tier ? t(`v2.dlc.caseTier.${row.tier}`) : "—"}</td>
                          <td className="mono">{(row.final.metadata?.dlc?.criteria_ids ?? []).join(" ") || "—"}</td>
                          {mode === "seed" && <td>{row.final.split ? t(`v2.dlc.split.${row.final.split as GoldenSplit}`) : "—"}</td>}
                          <td>
                            {errors.length > 0 ? (
                              <span className="v3-err">✗ {errors.map(describe).join("; ")}</span>
                            ) : warns.length > 0 ? (
                              <span style={{ color: "var(--v3-wait)" }}>! {warns.map(describe).join("; ")}</span>
                            ) : (
                              <span style={{ color: "var(--v3-ok)" }}>✓</span>
                            )}
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        )}
      </div>
    </Dialog>
  );
}
