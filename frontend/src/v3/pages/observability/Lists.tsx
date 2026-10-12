import { Search } from "lucide-react";
import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";

import type { ObsSessionRow, ObsTraceRow } from "../../../lib/api";
import { fmtNumber } from "../../../v2/format";
import { approxCost, isSystemTrace, shortId } from "../../../v2/pages/observability/common";
import { ago, ms } from "../../format";
import { Chip, Empty, Filters, Lamp, Notice, Panel, Skeleton } from "../../ui";
import { CopyId, TraceStatus } from "./shared";

const PAGE = 50;

function Toolbar({
  agents,
  agent,
  onAgent,
  q,
  onQ,
  placeholder,
  count,
  children,
}: {
  agents: string[];
  agent: string;
  onAgent: (v: string) => void;
  q: string;
  onQ: (v: string) => void;
  placeholder: string;
  count: string;
  children?: React.ReactNode;
}) {
  const { t } = useTranslation();
  return (
    <div style={{ display: "flex", gap: 12, alignItems: "center", flexWrap: "wrap" }}>
      <select className="v3-select" style={{ width: 200 }} value={agent} onChange={(e) => onAgent(e.target.value)} aria-label={t("v3.obs.agent")}>
        <option value="">{t("v3.obs.allAgents")}</option>
        {agents.map((a) => <option key={a} value={a}>{a}</option>)}
      </select>
      {children}
      <div style={{ marginLeft: "auto", display: "flex", gap: 12, alignItems: "center", flex: "1 1 260px", justifyContent: "flex-end" }}>
        <span className="v3-obs-cache">{count}</span>
        <div style={{ position: "relative", flex: "0 1 300px" }}>
          <Search size={14} style={{ position: "absolute", left: 12, top: 11, color: "var(--v3-text-3)" }} aria-hidden="true" />
          <input className="v3-input" style={{ paddingLeft: 34 }} value={q} onChange={(e) => onQ(e.target.value)} placeholder={placeholder} aria-label={placeholder} />
        </div>
      </div>
    </div>
  );
}

function More({ shown, total, onMore }: { shown: number; total: number; onMore: () => void }) {
  const { t } = useTranslation();
  if (shown >= total) return null;
  return (
    <div className="v3-obs-more">
      <button type="button" className="v3-btn sm" onClick={onMore}>{t("v3.obs.showMore", { shown, total })}</button>
    </div>
  );
}

export function SessionsView({
  rows: sessions,
  loading,
  error,
  range,
  onOpen,
}: {
  rows: ObsSessionRow[] | null;
  loading: boolean;
  error: string | null;
  range: string;
  onOpen: (id: string) => void;
}) {
  const { t } = useTranslation();
  const [agent, setAgent] = useState("");
  const [errors, setErrors] = useState<"all" | "errors" | "clean">("all");
  const [q, setQ] = useState("");
  const [limit, setLimit] = useState(PAGE);
  const all = useMemo(() => sessions ?? [], [sessions]);
  const agents = useMemo(() => [...new Set(all.map((r) => r.agent).filter(Boolean))].sort(), [all]);
  const rows = useMemo(() => {
    const needle = q.trim().toLowerCase();
    return all.filter((r) => {
      if (agent && r.agent !== agent) return false;
      if (errors === "errors" && r.errors === 0) return false;
      if (errors === "clean" && r.errors > 0) return false;
      return !needle || r.session_id.toLowerCase().includes(needle);
    });
  }, [all, agent, errors, q]);
  const withErrors = all.filter((r) => r.errors > 0).length;

  return (
    <>
      <Toolbar agents={agents} agent={agent} onAgent={setAgent} q={q} onQ={setQ} placeholder={t("v3.obs.searchSession")}
        count={t("v3.obs.scannedSessions", { count: rows.length, range: range.toUpperCase() })}>
        <Filters
          value={errors}
          onChange={setErrors}
          options={[
            { value: "all", label: t("v3.obs.all"), count: all.length },
            { value: "errors", label: t("v3.obs.withErrors"), s: "act", count: withErrors },
            { value: "clean", label: t("v3.obs.clean"), s: "ok", count: all.length - withErrors },
          ]}
        />
      </Toolbar>
      <Panel flush>
        {loading && !sessions ? (
          <div style={{ padding: 20 }}><Skeleton rows={6} /></div>
        ) : error && !sessions ? (
          <div style={{ padding: 20 }}><Notice s="act">{t("obs.loadFailed", { msg: error })}</Notice></div>
        ) : rows.length === 0 ? (
          <Empty title={all.length ? t("v3.obs.noMatch") : t("v3.obs.sessionsEmpty")} />
        ) : (
          <>
            <table className="v3-table">
              <thead>
                <tr>
                  <th />
                  <th>{t("v3.obs.session")}</th>
                  <th>{t("v3.obs.agent")}</th>
                  <th className="num">{t("v3.obs.traces")}</th>
                  <th className="num">{t("v3.obs.llmCalls")}</th>
                  <th className="num">{t("v3.obs.tokensCost")}</th>
                  <th className="num">{t("v3.obs.errors")}</th>
                  <th className="num">{t("v3.obs.lastActive")}</th>
                </tr>
              </thead>
              <tbody>
                {rows.slice(0, limit).map((r) => (
                  <tr key={r.session_id} className="click" onClick={() => onOpen(r.session_id)}>
                    <td style={{ width: 30 }}><Lamp s={r.errors > 0 ? "act" : "ok"} /></td>
                    <td><CopyId id={r.session_id} chars={22} /></td>
                    <td style={{ color: "var(--v3-text-2)" }}>{r.agent || "—"}</td>
                    <td className="num">{r.traces}</td>
                    <td className="num">{r.llm_calls}</td>
                    <td className="num">
                      {r.tokens.total > 0 ? <>{fmtNumber(r.tokens.total)}<span className="v3-obs-sub">{approxCost(r.est_cost_usd)}</span></> : "—"}
                    </td>
                    <td className="num">{r.errors > 0 ? <Chip s="act">{r.errors}</Chip> : <span style={{ color: "var(--v3-text-3)" }}>0</span>}</td>
                    <td className="num" style={{ color: "var(--v3-text-3)" }} title={r.last ?? undefined}>{ago(r.last)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            <More shown={Math.min(limit, rows.length)} total={rows.length} onMore={() => setLimit((n) => n + PAGE)} />
          </>
        )}
      </Panel>
    </>
  );
}

export function TracesView({
  rows: traces,
  loading,
  error,
  range,
  initialStatus,
  onOpen,
  onOpenSession,
}: {
  rows: ObsTraceRow[] | null;
  loading: boolean;
  error: string | null;
  range: string;
  initialStatus: "all" | "ok" | "error";
  onOpen: (id: string) => void;
  onOpenSession: (id: string) => void;
}) {
  const { t } = useTranslation();
  const [agent, setAgent] = useState("");
  const [status, setStatus] = useState<"all" | "ok" | "error">(initialStatus);
  const [q, setQ] = useState("");
  const [showSystem, setShowSystem] = useState(false);
  const [limit, setLimit] = useState(PAGE);
  const all = useMemo(() => traces ?? [], [traces]);
  const agents = useMemo(() => [...new Set(all.map((r) => r.agent).filter(Boolean))].sort(), [all]);
  // Harness health-check traces are noise for humans: hidden unless asked for
  const visible = useMemo(() => all.filter((r) => showSystem || !isSystemTrace(r)), [all, showSystem]);
  const systemCount = all.length - all.filter((r) => !isSystemTrace(r)).length;
  const rows = useMemo(() => {
    const needle = q.trim().toLowerCase();
    return visible.filter((r) => {
      if (agent && r.agent !== agent) return false;
      if (status !== "all" && r.status !== status) return false;
      return !needle || `${r.trace_id} ${r.session_id ?? ""}`.toLowerCase().includes(needle);
    });
  }, [visible, agent, status, q]);
  const failed = visible.filter((r) => r.status === "error").length;

  return (
    <>
      <Toolbar agents={agents} agent={agent} onAgent={setAgent} q={q} onQ={setQ} placeholder={t("v3.obs.searchTrace")}
        count={t("v3.obs.scannedTraces", { count: rows.length, range: range.toUpperCase() })}>
        <Filters
          value={status}
          onChange={setStatus}
          options={[
            { value: "all", label: t("v3.obs.all"), count: visible.length },
            { value: "error", label: t("v3.obs.status.error"), s: "act", count: failed },
            { value: "ok", label: t("v3.obs.status.ok"), s: "ok", count: visible.length - failed },
          ]}
        />
        {systemCount > 0 && (
          <label className="v3-obs-check" title={t("v3.obs.systemHint")}>
            <input type="checkbox" checked={showSystem} onChange={(e) => setShowSystem(e.target.checked)} />
            {t("v3.obs.showSystem", { count: systemCount })}
          </label>
        )}
      </Toolbar>
      <Panel flush>
        {loading && !traces ? (
          <div style={{ padding: 20 }}><Skeleton rows={6} /></div>
        ) : error && !traces ? (
          <div style={{ padding: 20 }}><Notice s="act">{t("obs.loadFailed", { msg: error })}</Notice></div>
        ) : rows.length === 0 ? (
          <Empty title={all.length ? t("v3.obs.noMatch") : t("v3.obs.tracesEmpty")} />
        ) : (
          <>
            <table className="v3-table">
              <thead>
                <tr>
                  <th />
                  <th>{t("v3.obs.trace")}</th>
                  <th>{t("v3.obs.agent")}</th>
                  <th>{t("v3.obs.session")}</th>
                  <th className="num">{t("v3.obs.duration")}</th>
                  <th className="num">{t("v3.obs.spansLlm")}</th>
                  <th className="num">{t("v3.obs.tokensCost")}</th>
                  <th>{t("v3.obs.statusLabel")}</th>
                </tr>
              </thead>
              <tbody>
                {rows.slice(0, limit).map((r) => (
                  <tr key={r.trace_id} className="click" onClick={() => onOpen(r.trace_id)}>
                    <td style={{ width: 30 }}><Lamp s={r.status === "error" ? "act" : "ok"} /></td>
                    <td>
                      <span className="mono" style={{ fontSize: 12.5 }} title={r.trace_id}>{shortId(r.trace_id, 16)}</span>
                      <span className="v3-obs-sub">{ago(r.time)} · {r.root_operation}</span>
                    </td>
                    <td style={{ color: "var(--v3-text-2)" }}>{r.agent || "—"}</td>
                    <td onClick={(e) => e.stopPropagation()}>
                      {r.session_id ? (
                        <button type="button" className="v3-obs-link mono" style={{ fontSize: 12.5 }} title={r.session_id}
                          onClick={() => onOpenSession(r.session_id as string)}>
                          {shortId(r.session_id, 14)}
                        </button>
                      ) : "—"}
                    </td>
                    <td className="num" style={r.duration_ms > 10_000 ? { color: "var(--v3-wait)" } : undefined}>{ms(r.duration_ms)}</td>
                    <td className="num" title={r.model ?? undefined}>{r.span_count} / {r.llm_count}</td>
                    <td className="num">
                      {r.tokens.total > 0 ? <>{fmtNumber(r.tokens.total)}<span className="v3-obs-sub">{approxCost(r.est_cost_usd)}</span></> : "—"}
                    </td>
                    <td><TraceStatus status={r.status} durationMs={r.duration_ms} /></td>
                  </tr>
                ))}
              </tbody>
            </table>
            <More shown={Math.min(limit, rows.length)} total={rows.length} onMore={() => setLimit((n) => n + PAGE)} />
          </>
        )}
      </Panel>
    </>
  );
}
