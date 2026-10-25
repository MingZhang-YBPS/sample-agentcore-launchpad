// The registry's "waiting approval" tab: the queue, longest-waiting first, with
// multi-select and batch approve / reject run from the browser (no batch endpoint:
// the existing action call, three at a time, one retry when throttled).
import { Search } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import { ErrorCallout } from "../../../components/ErrorCallout";
import { api, errorMessage, type RegistryRecordOut } from "../../../lib/api";
import { useFailure } from "../../failure";
import { ago } from "../../format";
import { useToast } from "../../hooks";
import { Reasoned } from "../../reason";
import { Btn, Chip, Confirm, Empty, Filters, Lamp, Panel } from "../../ui";
import { PAGE_SIZE, pageCount, runBatch, type RowState } from "./queue";

type RecordType = RegistryRecordOut["type"];
type Decision = "approve" | "reject";

/** How many record names the confirmation lists before "and N more". */
const CONFIRM_NAMES = 10;
/** Approved / rejected rows stay this long, fading, before the list reloads. */
const FADE_MS = 1000;

/** Keys typed into a field, or a space on a control, are not shortcuts. */
function ownsKey(target: EventTarget | null, key: string): boolean {
  if (!(target instanceof HTMLElement)) return false;
  if (target.isContentEditable || target instanceof HTMLTextAreaElement || target instanceof HTMLSelectElement) return true;
  if (target instanceof HTMLInputElement) return target.type !== "checkbox" || key === " ";
  return key === " " && (target instanceof HTMLButtonElement || target instanceof HTMLAnchorElement);
}

export function PendingTab({
  queue,
  isAdmin,
  typeLabel,
  statusLabel,
  single,
  onOpen,
  onDone,
}: {
  /** every record waiting approval, longest-waiting first */
  queue: RegistryRecordOut[];
  isAdmin: boolean;
  typeLabel: (type: string) => string;
  statusLabel: (status: string) => string;
  /** the one-record lifecycle the row buttons use */
  single: { busy: Set<string>; run: (record: RegistryRecordOut, action: Decision) => Promise<void> };
  onOpen: (id: string) => void;
  onDone: () => void;
}) {
  const { t } = useTranslation();
  const toast = useToast();
  // a batch with refused rows says so in place; the rows keep their own reasons
  const outcome = useFailure();
  const [type, setType] = useState<"all" | RecordType>("all");
  const [q, setQ] = useState("");
  const [page, setPage] = useState(0);
  const [cursor, setCursor] = useState(0);
  const [selected, setSelected] = useState<Set<string>>(() => new Set());
  const [rowStates, setRowStates] = useState<Record<string, RowState>>({});
  const [decision, setDecision] = useState<Decision>("approve");
  const [running, setRunning] = useState(false);
  const [failed, setFailed] = useState<string[]>([]);
  const [confirm, setConfirm] = useState<{ action: Decision; ids: string[] } | null>(null);

  // a member cannot move a system record (the server refuses too): shown, read-only
  const actionable = (r: RegistryRecordOut) => !r.system || isAdmin;
  const rows = useMemo(() => {
    const needle = q.trim().toLowerCase();
    return queue
      .filter((r) => type === "all" || r.type === type)
      .filter((r) => !needle || `${r.name} ${r.record_id} ${r.description}`.toLowerCase().includes(needle));
  }, [queue, type, q]);
  const pages = pageCount(rows.length);
  const at = Math.min(page, pages - 1);
  const pageRows = rows.slice(at * PAGE_SIZE, (at + 1) * PAGE_SIZE);
  const selectable = rows.filter(actionable).map((r) => r.record_id);
  // a batch acts on what is selected and still in view: "select all" means the current filter
  const chosen = selectable.filter((id) => selected.has(id));
  const allChosen = selectable.length > 0 && chosen.length === selectable.length;
  const byId = useMemo(() => new Map(queue.map((r) => [r.record_id, r])), [queue]);

  // a reload drops what left the queue; a row that is still there keeps its state
  useEffect(() => {
    setSelected((prev) => new Set([...prev].filter((id) => byId.has(id))));
    setRowStates((prev) => Object.fromEntries(Object.entries(prev).filter(([id]) => byId.has(id))));
  }, [byId]);

  const toggle = (id: string) =>
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  const execute = async (action: Decision, ids: string[]) => {
    setConfirm(null);
    setDecision(action);
    setRunning(true);
    setFailed([]);
    outcome.clear();
    const result = await runBatch(
      ids,
      (id) => api.registryAction(id, action),
      (id, state) => setRowStates((prev) => ({ ...prev, [id]: state })),
      { describe: errorMessage },
    );
    setRunning(false);
    // failed rows stay selected so the same button retries them
    const done = new Set(result.ok);
    setSelected((prev) => new Set([...prev].filter((id) => !done.has(id))));
    setFailed(result.failed);
    const verb = t(`v3.registry.batch.${action}Done`);
    if (result.failed.length) outcome.fail(t("v3.registry.batch.summary", { verb, ok: result.ok.length, failed: result.failed.length }));
    else toast("ok", t("v3.registry.batch.summaryOk", { verb, ok: result.ok.length }));
    // one reload for the whole batch, after the finished rows have faded
    if (result.ok.length) window.setTimeout(onDone, FADE_MS);
  };

  // keyboard: ↑/↓ move, space selects, A approves and R rejects the current row (both confirm)
  const keys = useRef<(e: KeyboardEvent) => void>(() => undefined);
  keys.current = (e) => {
    if (e.defaultPrevented || e.ctrlKey || e.metaKey || e.altKey || confirm || running) return;
    if (ownsKey(e.target, e.key) || document.querySelector(".v3-modal-mask")) return;
    const row = pageRows[Math.min(cursor, pageRows.length - 1)];
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      if (!pageRows.length) return;
      e.preventDefault();
      setCursor((c) => Math.max(0, Math.min(pageRows.length - 1, c + (e.key === "ArrowDown" ? 1 : -1))));
      return;
    }
    if (!row || !actionable(row)) return;
    const key = e.key.toLowerCase();
    if (e.key === " ") {
      e.preventDefault();
      toggle(row.record_id);
    } else if (key === "a" || key === "r") {
      e.preventDefault();
      setConfirm({ action: key === "a" ? "approve" : "reject", ids: [row.record_id] });
    }
  };
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => keys.current(e);
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const resetView = () => {
    setPage(0);
    setCursor(0);
  };
  const countType = (k: RecordType) => queue.filter((r) => r.type === k).length;
  const retry = failed.filter((id) => selected.has(id) && byId.has(id));
  const confirmNames = (confirm?.ids ?? []).map((id) => byId.get(id)?.name ?? id);

  return (
    <>
      <ErrorCallout error={outcome.error} onDismiss={outcome.clear} data-testid="v3-registry-batch-error" />
      <div style={{ display: "flex", gap: 12, alignItems: "center", flexWrap: "wrap" }}>
        <label style={{ display: "inline-flex", gap: 8, alignItems: "center", color: "var(--v3-text-2)" }}>
          <Reasoned disabledReason={running ? null : t("story.reason.nothingToPick")}>
            <input
              type="checkbox"
              checked={allChosen}
              disabled={!selectable.length || running}
              ref={(el) => { if (el) el.indeterminate = chosen.length > 0 && !allChosen; }}
              onChange={() => setSelected(allChosen ? new Set() : new Set(selectable))}
            />
          </Reasoned>
          {t("v3.registry.batch.selectAll", { count: selectable.length })}
        </label>
        <span className="mono" style={{ color: "var(--v3-text-3)" }}>{t("v3.registry.batch.selected", { count: chosen.length })}</span>
        <Btn size="sm" kind="primary" busy={running} disabled={!chosen.length} disabledReason={t("story.reason.selectRows")} onClick={() => setConfirm({ action: "approve", ids: chosen })}>
          {t("v3.registry.batch.approve")}
        </Btn>
        <Btn size="sm" busy={running} disabled={!chosen.length} disabledReason={t("story.reason.selectRows")} onClick={() => setConfirm({ action: "reject", ids: chosen })}>
          {t("v3.registry.batch.reject")}
        </Btn>
        {retry.length > 0 && !running && (
          <Btn size="sm" kind="ghost" onClick={() => void execute(decision, retry)}>
            {t("v3.registry.batch.retryFailed", { count: retry.length })}
          </Btn>
        )}
        <span style={{ marginLeft: "auto" }} />
        <Filters
          value={type}
          onChange={(next) => { setType(next); resetView(); }}
          options={[
            { value: "all", label: t("v3.registry.allTypes"), count: queue.length },
            ...(["A2A", "MCP", "AGENT_SKILLS"] as const).map((k) => ({ value: k, label: typeLabel(k), count: countType(k) })),
          ]}
        />
        <div style={{ position: "relative", flex: "1 1 200px", maxWidth: 300 }}>
          <Search size={14} style={{ position: "absolute", left: 12, top: 11, color: "var(--v3-text-3)" }} aria-hidden="true" />
          <input className="v3-input" style={{ paddingLeft: 34 }} value={q} onChange={(e) => { setQ(e.target.value); resetView(); }}
            placeholder={t("v3.registry.search")} aria-label={t("v3.registry.search")} />
        </div>
      </div>

      <Panel flush>
        {rows.length === 0 ? (
          <Empty title={queue.length ? t("v3.registry.none") : t("v3.registry.pendingEmpty")} />
        ) : (
          <table className="v3-table v3-registry-queue">
            <tbody>
              {pageRows.map((r, i) => {
                const can = actionable(r);
                const st = rowStates[r.record_id];
                const busy = single.busy.has(r.record_id) || st?.state === "running";
                return (
                  <tr key={r.record_id} data-row={st?.state} data-cursor={i === cursor ? "true" : undefined}
                    onClick={() => setCursor(i)}>
                    <td style={{ width: 30 }}>
                      {can && (
                        <input type="checkbox" aria-label={r.name} checked={selected.has(r.record_id)} disabled={running}
                          onChange={() => toggle(r.record_id)} />
                      )}
                    </td>
                    <td style={{ width: 30 }}><Lamp s="wait" live /></td>
                    <td>
                      <div className="v3-name"><div><b>{r.name}</b><small>{(r.description || r.record_id).slice(0, 90)}</small></div></div>
                    </td>
                    <td><Chip>{typeLabel(r.type)}</Chip>{r.system && <> <Chip s="info">{t("v2.registry.system")}</Chip></>}</td>
                    <td className="mono" style={{ color: "var(--v3-text-2)", whiteSpace: "nowrap" }}>{r.version ?? "—"}</td>
                    <td className="num" style={{ color: "var(--v3-text-3)", whiteSpace: "nowrap" }}>{t("v3.registry.waiting", { ago: ago(r.updated_at) })}</td>
                    <td style={{ width: 1, whiteSpace: "nowrap" }}>
                      <div style={{ display: "flex", gap: 6, justifyContent: "flex-end", alignItems: "center" }}>
                        {st?.state === "running" && <Chip s="info">{t("v3.registry.batch.rowRunning")}</Chip>}
                        {st?.state === "ok" && <Chip s="ok">✓ {statusLabel(decision === "approve" ? "APPROVED" : "REJECTED")}</Chip>}
                        {st?.state === "failed" && <Chip s="act" title={st.message}>✗ {t("v3.registry.batch.rowFailed", { msg: st.message })}</Chip>}
                        {can ? (
                          <>
                            <Btn size="sm" kind="primary" disabled={busy || running} onClick={() => void single.run(r, "approve")}>
                              {t("v2.registry.action.approve")}
                            </Btn>
                            <Btn size="sm" disabled={busy || running} onClick={() => void single.run(r, "reject")}>
                              {t("v2.registry.action.reject")}
                            </Btn>
                          </>
                        ) : (
                          <span style={{ color: "var(--v3-text-3)" }}>{t("registry.drawer.system.memberReadOnly")}</span>
                        )}
                        <Btn size="sm" kind="ghost" onClick={() => onOpen(r.record_id)}>{t("v3.registry.open")}</Btn>
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </Panel>

      <div style={{ display: "flex", gap: 8, alignItems: "center", justifyContent: "space-between", flexWrap: "wrap", color: "var(--v3-text-3)", fontSize: 12 }}>
        <span>{t("v3.registry.keysHint")}</span>
        {pages > 1 && (
          <span style={{ display: "inline-flex", gap: 4, alignItems: "center" }} role="navigation" aria-label={t("v3.registry.pages")}>
            <span style={{ marginRight: 8 }}>{t("v3.registry.perPage", { count: PAGE_SIZE })}</span>
            <Btn size="sm" kind="ghost" disabled={at === 0} disabledReason={t("story.reason.firstPage")} title={t("v3.registry.prevPage")} onClick={() => { setPage(at - 1); setCursor(0); }}>‹</Btn>
            {Array.from({ length: pages }, (_, p) => (
              <Btn key={p} size="sm" kind={p === at ? undefined : "ghost"} onClick={() => { setPage(p); setCursor(0); }}>{p + 1}</Btn>
            ))}
            <Btn size="sm" kind="ghost" disabled={at === pages - 1} disabledReason={t("story.reason.lastPage")} title={t("v3.registry.nextPage")} onClick={() => { setPage(at + 1); setCursor(0); }}>›</Btn>
          </span>
        )}
      </div>

      {confirm && (
        <Confirm
          title={t("v3.registry.batch.confirmTitle", { context: confirm.action, count: confirm.ids.length })}
          confirmLabel={t(`v2.registry.action.${confirm.action}`)}
          cancelLabel={t("v3.common.cancel")}
          danger={confirm.action === "reject"}
          onCancel={() => setConfirm(null)}
          onConfirm={() => void execute(confirm.action, confirm.ids)}
        >
          <p style={{ margin: "0 0 8px" }}>{t("v3.registry.batch.confirmBody", { context: confirm.action, count: confirm.ids.length })}</p>
          <ul style={{ margin: 0, paddingLeft: 18 }}>
            {confirmNames.slice(0, CONFIRM_NAMES).map((name, i) => <li key={`${name}:${i}`}>{name}</li>)}
          </ul>
          {confirmNames.length > CONFIRM_NAMES && (
            <p style={{ margin: "8px 0 0", color: "var(--v3-text-3)" }}>{t("v3.registry.batch.more", { count: confirmNames.length - CONFIRM_NAMES })}</p>
          )}
        </Confirm>
      )}
    </>
  );
}
