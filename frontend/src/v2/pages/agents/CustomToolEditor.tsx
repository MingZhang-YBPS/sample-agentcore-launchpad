import { Plus, X } from "lucide-react";
import { type KeyboardEvent, type RefObject, useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import { useAuth } from "../../../auth/auth-context";
import { errorMessage } from "../../../lib/api";
import { type CustomToolCheck, customToolsApi } from "../../../lib/api/custom-tools";
import {
  type CustomToolExecution,
  type CustomToolRow,
  customToolTemplate,
  emptyStrandsForm,
  MAX_CUSTOM_TOOLS,
  nextCustomToolName,
  renameInCode,
  requirementList,
  type Sb3IssueKey,
  sb3RowIssues,
  type StrandsForm,
} from "../../../lib/strands-builder";
import { Button, Field, Segmented } from "../../ui";
import type { SectionProps } from "./wizardKit";
import "./custom-tool-editor.css";

/**
 * Custom Python tools (strands-builder SB-3): each tool is a bench — name and where
 * it runs on the head, "when to call it" under it, then the code on a dark slab
 * with a line gutter. The backend's check reads the slab as the member types
 * (debounced): a syntax error, the parameters the model will see, and every
 * finding pinned to its line in the gutter. A version with findings deploys only
 * once an administrator approved that exact code hash — the verdict strip says
 * which state this version is in, and an administrator approves from it.
 */

const CHECK_DELAY_MS = 600;
const INDENT = "    ";

type Patch = Partial<StrandsForm> | ((prev: StrandsForm) => Partial<StrandsForm>);

function useStrandsPatch(set: SectionProps["set"]) {
  return (patch: Patch) =>
    set((prev) => {
      const cur = { ...emptyStrandsForm(), ...(prev.strands ?? {}) };
      return { strands: { ...cur, ...(typeof patch === "function" ? patch(cur) : patch) } };
    });
}

/** Mounted in the Tools group, under the sandboxed built-in tools. */
export function CustomToolsPanel({ form, set, touched }: Pick<SectionProps, "form" | "set"> & { touched: boolean }) {
  const { t } = useTranslation();
  const s = { ...emptyStrandsForm(), ...(form.strands ?? {}) };
  const patch = useStrandsPatch(set);
  const rowIssues = sb3RowIssues(s);
  const [opened, setOpened] = useState<number | null>(null);
  const setRow = (i: number, change: Partial<CustomToolRow>) =>
    patch((prev) => ({ customTools: prev.customTools.map((r, j) => (j === i ? { ...r, ...change } : r)) }));
  const add = () => {
    const name = nextCustomToolName(s.customTools);
    patch((prev) => ({
      customTools: [
        ...prev.customTools,
        { name, description: "", code: customToolTemplate(name), requirements: "", execution: "runtime" },
      ],
    }));
    setOpened(s.customTools.length);
  };
  const full = s.customTools.length >= MAX_CUSTOM_TOOLS;
  const countIssue = rowIssues.get(-1)?.customToolCount;
  return (
    <Field
      label={t("strandsBuilder.custom.label")}
      hint={t("strandsBuilder.custom.hint")}
      error={countIssue !== undefined ? t("strandsBuilder.err.customToolCount", { detail: countIssue }) : undefined}
    >
      <div className="ct-list" data-testid="sb-custom-tools">
        {s.customTools.map((row, i) => (
          <CustomToolBench
            key={i}
            index={i}
            row={row}
            issues={rowIssues.get(i) ?? {}}
            touched={touched}
            startOpen={opened === i}
            onChange={(change) => setRow(i, change)}
            onRemove={() => patch((prev) => ({ customTools: prev.customTools.filter((_, j) => j !== i) }))}
          />
        ))}
        <button type="button" className="ct-add" onClick={add} disabled={full} data-testid="sb-custom-tool-add">
          <Plus size={14} aria-hidden="true" />
          <span>{t("strandsBuilder.custom.add")}</span>
          <span className="ct-add-when">{full ? t("strandsBuilder.custom.full", { count: MAX_CUSTOM_TOOLS }) : t("strandsBuilder.custom.addWhen")}</span>
        </button>
      </div>
    </Field>
  );
}

/* ── one tool ──────────────────────────────────────────────────────────── */

type Verdict = "idle" | "checking" | "error" | "syntax" | "clean" | "review" | "approved";

function verdictOf(check: CustomToolCheck | null, checking: boolean, failed: string | null): Verdict {
  if (checking) return "checking";
  if (failed) return "error";
  if (!check) return "idle";
  if (!check.ok) return "syntax";
  if (!check.needs_approval) return "clean";
  return check.approval ? "approved" : "review";
}

function CustomToolBench({
  index,
  row,
  issues,
  touched,
  startOpen,
  onChange,
  onRemove,
}: {
  index: number;
  row: CustomToolRow;
  issues: Partial<Record<Sb3IssueKey, string>>;
  touched: boolean;
  startOpen: boolean;
  onChange: (change: Partial<CustomToolRow>) => void;
  onRemove: () => void;
}) {
  const { t } = useTranslation();
  const { isAdmin } = useAuth();
  const editor = useRef<HTMLTextAreaElement>(null);
  const [check, setCheck] = useState<CustomToolCheck | null>(null);
  const [checking, setChecking] = useState(false);
  const [failed, setFailed] = useState<string | null>(null);
  const [nonce, setNonce] = useState(0);
  const [note, setNote] = useState("");
  const [approving, setApproving] = useState(false);
  const [approveError, setApproveError] = useState<string | null>(null);
  const requirements = useMemo(() => requirementList(row.requirements), [row.requirements]);
  const name = row.name.trim();
  const checkable = !issues.customToolName && !issues.customToolCode && !issues.customToolRequirement && !issues.customToolSandboxRequirements;

  useEffect(() => {
    if (!checkable) {
      setCheck(null);
      setChecking(false);
      return;
    }
    let live = true;
    setChecking(true);
    const timer = window.setTimeout(() => {
      customToolsApi
        .check({ name, code: row.code, requirements, execution: row.execution })
        .then((res) => {
          if (!live) return;
          setCheck(res);
          setFailed(null);
        })
        .catch((err: unknown) => live && setFailed(errorMessage(err)))
        .finally(() => live && setChecking(false));
    }, CHECK_DELAY_MS);
    return () => {
      live = false;
      window.clearTimeout(timer);
    };
  }, [checkable, name, row.code, requirements, row.execution, nonce]);

  const verdict = verdictOf(check, checking, failed);
  const marks = useMemo(() => {
    const out = new Map<number, "error" | "finding">();
    if (check?.error?.line) out.set(check.error.line, "error");
    for (const f of check?.findings ?? []) if (f.line > 0 && !out.has(f.line)) out.set(f.line, "finding");
    return out;
  }, [check]);
  const params = Object.entries(check?.input_schema?.properties ?? {});
  const required = new Set(check?.input_schema?.required ?? []);
  const show = (key: Sb3IssueKey) => {
    const detail = issues[key];
    if (detail === undefined) return undefined;
    if (!detail && !touched && key === "customToolDescription") return undefined;
    return t(`strandsBuilder.err.${key}`, { detail });
  };
  const jumpTo = (line: number) => {
    const area = editor.current;
    if (!area || line < 1) return;
    const lines = area.value.split("\n");
    const start = lines.slice(0, line - 1).reduce((n, l) => n + l.length + 1, 0);
    area.focus();
    area.setSelectionRange(start, start + (lines[line - 1]?.length ?? 0));
  };
  const approve = () => {
    setApproving(true);
    setApproveError(null);
    customToolsApi
      .approve({ name, code: row.code, requirements, execution: row.execution, description: row.description.trim() || name, note })
      .then(() => {
        setNote("");
        setNonce((n) => n + 1);
      })
      .catch((err: unknown) => setApproveError(errorMessage(err)))
      .finally(() => setApproving(false));
  };
  const revoke = () => {
    if (!check?.approval) return;
    customToolsApi
      .revoke(check.code_hash)
      .then(() => setNonce((n) => n + 1))
      .catch((err: unknown) => setApproveError(errorMessage(err)));
  };

  return (
    <details className={`ct-bench v-${verdict}`} open={startOpen || undefined} data-testid={`sb-custom-tool-${index}`}>
      <summary className="ct-head">
        <span className="ct-idx" aria-hidden="true">{String(index + 1).padStart(2, "0")}</span>
        <span className="ct-name mono">{name || t("strandsBuilder.custom.unnamed")}</span>
        <span className={`ct-where ${row.execution}`}>{t(`strandsBuilder.custom.where.${row.execution}`)}</span>
        <span className="ct-state">
          <span className="sb-lamp" aria-hidden="true" />
          {t(`strandsBuilder.custom.verdictShort.${verdict}`)}
        </span>
        <button type="button" className="sb-x" aria-label={t("strandsBuilder.remove")} onClick={(e) => { e.preventDefault(); onRemove(); }}>
          <X size={14} aria-hidden="true" />
        </button>
      </summary>
      <div className="ct-body">
        <div className="ct-meta">
          <Field label={t("strandsBuilder.custom.name")} required error={show("customToolName") ?? show("customToolDuplicate")}>
            <input
              className="v2-input mono"
              value={row.name}
              spellCheck={false}
              onChange={(e) => onChange({ name: e.target.value, code: renameInCode(row.code, row.name.trim(), e.target.value.trim()) })}
              aria-label={t("strandsBuilder.custom.name")}
              data-testid={`sb-custom-tool-name-${index}`}
            />
          </Field>
          <Field label={t("strandsBuilder.custom.runs")} hint={t(`strandsBuilder.custom.runsWhen.${row.execution}`)}>
            <Segmented<CustomToolExecution>
              value={row.execution}
              options={[
                { value: "runtime", label: t("strandsBuilder.custom.where.runtime") },
                { value: "code_interpreter", label: t("strandsBuilder.custom.where.code_interpreter") },
              ]}
              onChange={(execution) => onChange({ execution })}
              ariaLabel={t("strandsBuilder.custom.runs")}
            />
          </Field>
        </div>
        <Field label={t("strandsBuilder.custom.description")} required error={show("customToolDescription")}>
          <textarea
            className="v2-textarea"
            rows={2}
            value={row.description}
            placeholder={t("strandsBuilder.custom.descriptionPlaceholder")}
            onChange={(e) => onChange({ description: e.target.value })}
            aria-label={t("strandsBuilder.custom.description")}
          />
        </Field>
        <Field label={t("strandsBuilder.custom.code")} required error={show("customToolCode")}>
          <CodeSlab
            areaRef={editor}
            value={row.code}
            marks={marks}
            onChange={(code) => onChange({ code })}
            label={t("strandsBuilder.custom.code")}
            testId={`sb-custom-tool-code-${index}`}
          />
          <div className="ct-verdict" aria-live="polite">
            <span className="sb-lamp" aria-hidden="true" />
            <span className="ct-verdict-text">
              {verdict === "syntax" && check?.error
                ? t("strandsBuilder.custom.verdict.syntax", { line: check.error.line, message: check.error.message })
                : verdict === "approved" && check?.approval
                  ? t("strandsBuilder.custom.verdict.approved", {
                      who: check.approval.approved_by,
                      when: check.approval.approved_at.slice(0, 10),
                    })
                  : verdict === "error"
                    ? failed
                    : t(`strandsBuilder.custom.verdict.${verdict}`, { count: check?.findings.length ?? 0, params: params.length })}
            </span>
            {check && <span className="ct-hash mono" title={check.code_hash}>#{check.code_hash.slice(0, 10)}</span>}
          </div>
        </Field>
        {params.length > 0 && verdict !== "syntax" && (
          <div className="ct-params" aria-label={t("strandsBuilder.custom.params")}>
            <span className="ct-params-label">{t("strandsBuilder.custom.params")}</span>
            {params.map(([p, schema]) => (
              <span key={p} className="sb-chip mono">
                {p}
                {required.has(p) ? "*" : ""}
                <span className="ct-type">{schema.type ?? "any"}</span>
              </span>
            ))}
          </div>
        )}
        {check?.ok && check.findings.length > 0 && (
          <div className="ct-findings">
            <ol>
              {check.findings.map((f, i) => (
                <li key={`${f.line}-${i}`}>
                  <button type="button" className="ct-line mono" disabled={f.line < 1} onClick={() => jumpTo(f.line)}>
                    {f.line > 0 ? `L${f.line}` : t("strandsBuilder.custom.reqLine")}
                  </button>
                  <span className={`ct-kind k-${f.kind}`}>{t(`strandsBuilder.custom.kind.${f.kind}`, { defaultValue: f.kind })}</span>
                  <span className="ct-msg">{f.message}</span>
                </li>
              ))}
            </ol>
            {isAdmin ? (
              <div className="ct-approve">
                {check.approval ? (
                  <>
                    <span className="sb-fine">{check.approval.note ? `“${check.approval.note}”` : t("strandsBuilder.custom.approvedNoNote")}</span>
                    <Button size="sm" onClick={revoke}>
                      {t("strandsBuilder.custom.revoke")}
                    </Button>
                  </>
                ) : (
                  <>
                    <input
                      className="v2-input"
                      value={note}
                      maxLength={500}
                      placeholder={t("strandsBuilder.custom.notePlaceholder")}
                      onChange={(e) => setNote(e.target.value)}
                      aria-label={t("strandsBuilder.custom.note")}
                    />
                    <Button size="sm" kind="primary" onClick={approve} disabled={approving || checking} testId={`sb-custom-tool-approve-${index}`}>
                      {t("strandsBuilder.custom.approve")}
                    </Button>
                  </>
                )}
                {approveError && <p className="sb-err">{approveError}</p>}
              </div>
            ) : (
              !check.approval && <p className="sb-fine">{t("strandsBuilder.custom.askAdmin")}</p>
            )}
          </div>
        )}
        <Field
          label={t("strandsBuilder.custom.requirements")}
          hint={t(row.execution === "code_interpreter" ? "strandsBuilder.custom.requirementsSandbox" : "strandsBuilder.custom.requirementsHint")}
          error={show("customToolRequirement") ?? show("customToolSandboxRequirements")}
        >
          <textarea
            className="v2-textarea mono"
            rows={Math.min(4, Math.max(1, requirements.length + 1))}
            value={row.requirements}
            spellCheck={false}
            placeholder="requests==2.32.3"
            onChange={(e) => onChange({ requirements: e.target.value })}
            aria-label={t("strandsBuilder.custom.requirements")}
          />
        </Field>
      </div>
    </details>
  );
}

/* ── the code slab: a textarea with a line gutter ──────────────────────── */

function CodeSlab({
  areaRef,
  value,
  marks,
  onChange,
  label,
  testId,
}: {
  areaRef: RefObject<HTMLTextAreaElement>;
  value: string;
  marks: Map<number, "error" | "finding">;
  onChange: (value: string) => void;
  label: string;
  testId: string;
}) {
  const gutter = useRef<HTMLDivElement>(null);
  // Escape hands Tab back to the page, so the slab is never a keyboard trap
  const released = useRef(false);
  const lines = value.split("\n").length;
  // Tab indents, Enter keeps the indentation (one level more after a colon)
  const onKey = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    const area = e.currentTarget;
    const { selectionStart: start, selectionEnd: end } = area;
    if (e.key === "Escape") {
      released.current = true;
      return;
    }
    let insert: string | null = null;
    if (e.key === "Tab" && !released.current && !e.shiftKey && !e.altKey && !e.metaKey && !e.ctrlKey) insert = INDENT;
    if (e.key !== "Tab") released.current = false;
    if (e.key === "Enter" && !e.shiftKey && !e.altKey && !e.metaKey && !e.ctrlKey) {
      const lineStart = value.lastIndexOf("\n", start - 1) + 1;
      const current = value.slice(lineStart, start);
      const indent = /^\s*/.exec(current)?.[0] ?? "";
      insert = "\n" + indent + (current.trimEnd().endsWith(":") ? INDENT : "");
    }
    if (insert === null) return;
    e.preventDefault();
    const next = value.slice(0, start) + insert + value.slice(end);
    onChange(next);
    requestAnimationFrame(() => area.setSelectionRange(start + insert.length, start + insert.length));
  };
  return (
    <div className="ct-slab">
      <div className="ct-gutter" ref={gutter} aria-hidden="true">
        {Array.from({ length: lines }, (_, i) => (
          <span key={i} className={marks.get(i + 1) ? `m-${marks.get(i + 1)}` : undefined}>
            {i + 1}
          </span>
        ))}
      </div>
      <textarea
        ref={areaRef}
        className="ct-code"
        value={value}
        rows={Math.min(22, Math.max(8, lines + 1))}
        spellCheck={false}
        autoCapitalize="off"
        autoComplete="off"
        wrap="off"
        onChange={(e) => onChange(e.target.value)}
        onKeyDown={onKey}
        onScroll={(e) => {
          if (gutter.current) gutter.current.scrollTop = e.currentTarget.scrollTop;
        }}
        aria-label={label}
        data-testid={testId}
      />
    </div>
  );
}
