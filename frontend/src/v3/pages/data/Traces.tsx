import "../observability.css";

import { DatabaseZap, RefreshCw, Search } from "lucide-react";
import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { useNavigate, useSearchParams } from "react-router-dom";

import { api, type V2Range } from "../../../lib/api";
import { fmtNumber, RANGES, rangeLabel } from "../../../v2/format";
import { useWorkspace } from "../../../workspace/workspace-context";
import { ago, ms } from "../../format";
import { useLoad } from "../../hooks";
import { Btn, Chip, Empty, Filters, Lamp, Notice, Panel, Skeleton } from "../../ui";
import { TraceView } from "../observability/TraceView";
import { AddToDataset } from "./AddToDataset";
import { asRange } from "./common";

const PAGE = 50;

/** Observed trajectories: pick the sessions behind them and turn them into dataset items. */
export function TracesTab() {
  const { t } = useTranslation();
  const [params, setParams] = useSearchParams();
  const { current } = useWorkspace();
  const range = asRange(params.get("range"));
  const [force, setForce] = useState(0);
  const traces = useLoad(() => api.obsTraces(range, force > 0), `v3-data-traces:${current?.id ?? ""}:${range}:${force}`);
  const [agent, setAgent] = useState("");
  const [status, setStatus] = useState<"all" | "ok" | "error">("all");
  const [q, setQ] = useState("");
  const [limit, setLimit] = useState(PAGE);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [adding, setAdding] = useState(false);

  const all = useMemo(() => traces.data?.traces ?? [], [traces.data]);
  const agents = useMemo(() => [...new Set(all.map((r) => r.agent).filter(Boolean))].sort(), [all]);
  const rows = useMemo(() => {
    const needle = q.trim().toLowerCase();
    return all.filter((r) => {
      if (agent && r.agent !== agent) return false;
      if (status !== "all" && r.status !== status) return false;
      return !needle || `${r.trace_id} ${r.session_id ?? ""} ${r.agent} ${r.root_operation}`.toLowerCase().includes(needle);
    });
  }, [all, agent, status, q]);
  const shown = rows.slice(0, limit);
  const errors = all.filter((r) => r.status === "error").length;
  // a trace without a session has no trajectory to turn into an item
  const sessions = useMemo(
    () => [...new Set(all.filter((r) => selected.has(r.trace_id) && r.session_id).map((r) => r.session_id as string))],
    [all, selected],
  );
  const pickable = shown.filter((r) => r.session_id).map((r) => r.trace_id);
  const allPicked = pickable.length > 0 && pickable.every((id) => selected.has(id));
  const toggle = (id: string) =>
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  return (
    <div style={{ display: "grid", gap: 16 }}>
      <div style={{ display: "flex", gap: 12, alignItems: "center", flexWrap: "wrap" }}>
        <Filters
          value={range}
          onChange={(r: V2Range) => setParams({ tab: "traces", range: r })}
          options={RANGES.map((r) => ({ value: r, label: rangeLabel(t, r) }))}
        />
        <span style={{ width: 1, height: 20, background: "var(--v3-line)" }} aria-hidden="true" />
        <Filters
          value={status}
          onChange={setStatus}
          options={[
            { value: "all", label: t("v3.data.anyStatus"), count: all.length },
            { value: "error", label: t("v2.traces.errorShort"), s: "act", count: errors },
            { value: "ok", label: t("v2.traces.ok"), s: "ok", count: all.length - errors },
          ]}
        />
        <select className="v3-select" style={{ width: 200 }} value={agent} onChange={(e) => setAgent(e.target.value)} aria-label={t("v3.data.agent")}>
          <option value="">{t("v3.data.allAgents")}</option>
          {agents.map((a) => <option key={a} value={a}>{a}</option>)}
        </select>
        <Btn kind="ghost" onClick={() => setForce((n) => n + 1)} title={t("v3.data.refresh")}><RefreshCw size={14} /></Btn>
        <div style={{ marginLeft: "auto", display: "flex", gap: 10, alignItems: "center" }}>
          <div style={{ position: "relative", width: 260 }}>
            <Search size={14} style={{ position: "absolute", left: 12, top: 11, color: "var(--v3-text-3)" }} aria-hidden="true" />
            <input className="v3-input" style={{ paddingLeft: 34 }} value={q} onChange={(e) => setQ(e.target.value)}
              placeholder={t("v2.traces.search")} aria-label={t("v2.traces.search")} />
          </div>
          <Btn kind="primary" disabled={sessions.length === 0} onClick={() => setAdding(true)}>
            <DatabaseZap size={14} /> {t("v2.traces.addToDataset", { count: sessions.length })}
          </Btn>
        </div>
      </div>

      <Panel flush>
        {traces.loading && !traces.data ? (
          <div style={{ padding: 20 }}><Skeleton rows={6} /></div>
        ) : traces.error ? (
          <div style={{ padding: 20 }}><Notice s="act">{traces.error}</Notice></div>
        ) : rows.length === 0 ? (
          <Empty title={t("v2.traces.empty")} />
        ) : (
          <>
            <table className="v3-table">
              <thead>
                <tr>
                  <th style={{ width: 36 }}>
                    <input type="checkbox" aria-label={t("v2.traces.selectPage")} checked={allPicked}
                      onChange={() =>
                        setSelected((prev) => {
                          const next = new Set(prev);
                          for (const id of pickable) {
                            if (allPicked) next.delete(id);
                            else next.add(id);
                          }
                          return next;
                        })
                      } />
                  </th>
                  <th />
                  <th>{t("v2.traces.colTrace")}</th>
                  <th>{t("v3.data.agent")}</th>
                  <th>{t("v2.traces.colSession")}</th>
                  <th className="num">Tokens</th>
                  <th className="num">{t("v2.traces.colDuration")}</th>
                  <th className="num">{t("v3.data.steps")}</th>
                  <th>{t("v2.traces.colModel")}</th>
                  <th className="num">{t("v2.traces.colStart")}</th>
                </tr>
              </thead>
              <tbody>
                {shown.map((r) => (
                  <tr key={r.trace_id} className="click"
                    onClick={() => setParams({ tab: "traces", view: "trace", id: r.trace_id, range })}>
                    <td onClick={(e) => e.stopPropagation()}>
                      <input type="checkbox" aria-label={r.trace_id} disabled={!r.session_id}
                        title={r.session_id ? undefined : t("v2.traces.noSession")}
                        checked={selected.has(r.trace_id)} onChange={() => toggle(r.trace_id)} />
                    </td>
                    <td style={{ width: 24 }}><Lamp s={r.status === "ok" ? "ok" : "act"} /></td>
                    <td>
                      <div className="v3-name"><div><b className="mono" style={{ fontSize: 12.5 }}>{r.trace_id.slice(0, 12)}…</b><small>{r.root_operation}</small></div></div>
                    </td>
                    <td style={{ color: "var(--v3-text-2)" }}>{r.agent || "—"}</td>
                    <td className="mono" style={{ color: "var(--v3-text-3)", fontSize: 12 }} title={r.session_id ?? undefined}>
                      {r.session_id ? `${r.session_id.slice(0, 14)}…` : "—"}
                    </td>
                    <td className="num">{fmtNumber(r.tokens.total)}</td>
                    <td className="num">{ms(r.duration_ms)}</td>
                    <td className="num">{r.span_count}</td>
                    <td>
                      {r.model ? <Chip>{`${r.model}${r.llm_count ? ` ×${r.llm_count}` : ""}`}</Chip> : <span style={{ color: "var(--v3-text-3)" }}>—</span>}
                      {r.status === "error" && <> <Chip s="act">{t("v2.traces.error", { count: r.error_count })}</Chip></>}
                    </td>
                    <td className="num" style={{ color: "var(--v3-text-3)" }}>{ago(r.time)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            {rows.length > shown.length && (
              <div style={{ padding: 14, display: "flex", justifyContent: "center" }}>
                <Btn size="sm" onClick={() => setLimit((n) => n + PAGE)}>{t("v3.data.more", { count: rows.length - shown.length })}</Btn>
              </div>
            )}
          </>
        )}
      </Panel>
      {adding && (
        <AddToDataset sessionIds={sessions} range={range} onClose={() => setAdding(false)} onDone={() => setSelected(new Set())} />
      )}
    </div>
  );
}

/**
 * One trajectory: V3's trace view (waterfall + span inspector), with the data
 * center's own action — turn its session into a dataset item.
 */
export function DataTrace({ traceId }: { traceId: string }) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();
  const range = asRange(params.get("range"));
  const trace = useLoad(() => api.obsTrace(traceId, range), `v3-data-trace-meta:${traceId}:${range}`);
  const sessionId = trace.data?.meta.session_id ?? null;
  const [adding, setAdding] = useState(false);
  return (
    <div className="v3-obs" style={{ display: "grid", gap: 12 }}>
      <div style={{ display: "flex", justifyContent: "flex-end" }}>
        <Btn kind="primary" disabled={!sessionId} title={sessionId ? undefined : t("v2.traces.noSession")} onClick={() => setAdding(true)}>
          <DatabaseZap size={14} /> {t("v2.traces.addOne")}
        </Btn>
      </div>
      <TraceView
        traceId={traceId}
        range={range}
        onBack={() => setParams({ tab: "traces", range })}
        // the session view lives in observability
        onOpenSession={(sid) => navigate(`/v3/observability?tab=sessions&view=session&id=${encodeURIComponent(sid)}&range=${range}`)}
      />
      {adding && sessionId && <AddToDataset sessionIds={[sessionId]} range={range} onClose={() => setAdding(false)} />}
    </div>
  );
}
