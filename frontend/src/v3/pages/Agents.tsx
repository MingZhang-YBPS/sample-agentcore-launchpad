import { ArrowUpDown, Download, MessagesSquare, RefreshCw, Search, Trash2 } from "lucide-react";
import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link, useNavigate, useSearchParams } from "react-router-dom";

import { isImported, useAgentActions } from "../../lib/agent-admin/actions";
import { useAgentPermissions } from "../../lib/agent-admin/permissions";
import { api, type AgentInfo } from "../../lib/api";
import { useWorkspace } from "../../workspace/workspace-context";
import { Reasoned } from "../reason";
import { agentSignal } from "../signals";
import { Chip, Empty, Lamp, Notice, PageHead, Panel, type Signal, Skeleton } from "../ui";
import { useLoad, useToast } from "../hooks";
import { ago } from "../format";
import { ActionDialog } from "./agent/ActionDialog";
import { AgentDetail } from "./agent/AgentDetail";
import { BulkDeleteDialog } from "./agents-list/BulkDeleteDialog";
import { RowMenu } from "./agents-list/RowMenu";
import { bulkDeletable, liveSelection, opensDetail, rowChat, toggleSelection } from "./agents-list/rows";

function AgentList() {
  const { t } = useTranslation();
  const [params, setParams] = useSearchParams();
  const { current } = useWorkspace();
  const agents = useLoad(() => api.listAgents(), `v3-agents:${current?.id ?? ""}`);
  const q = params.get("q") ?? "";
  const status = params.get("status");
  const filter = (["ok", "wait", "act", "off"] as const).find((s) => s === status) ?? "all";
  const sort = params.get("sort") === "name" ? "name" : "updated";
  const update = (key: string, value: string) => setParams((prev) => {
    const next = new URLSearchParams(prev);
    if (value) next.set(key, value); else next.delete(key);
    return next;
  }, { replace: true });
  const rows = useMemo(() => {
    const needle = q.trim().toLowerCase();
    return (agents.data?.agents ?? [])
      .filter((a) => a.status !== "deleted")
      .filter((a) => filter === "all" || agentSignal(a) === filter)
      .filter((a) => !needle || `${a.name} ${a.display_name ?? ""} ${a.method}`.toLowerCase().includes(needle))
      .sort((a, b) => sort === "name" ? (a.display_name || a.name).localeCompare(b.display_name || b.name) : (b.updated_at ?? "").localeCompare(a.updated_at ?? ""));
  }, [agents.data, q, filter, sort]);
  const all = (agents.data?.agents ?? []).filter((a) => a.status !== "deleted");
  const count = (s: Signal) => all.filter((a) => agentSignal(a) === s).length;

  // row actions and bulk delete (v3-agent-management §3), on the detail page's rules
  const perms = useAgentPermissions();
  const navigate = useNavigate();
  const toast = useToast();
  const actions = useAgentActions({
    onDeleted: (agent) => {
      const name = agent.display_name || agent.name;
      toast("ok", t(isImported(agent) ? "v3.agent.action.removed" : "v3.agent.action.deleted", { name }));
      agents.reload();
    },
    onConverted: (agent) => {
      toast("ok", t("v3.agent.action.convertStarted", { name: agent.name }));
      navigate(`/v3/agents?id=${encodeURIComponent(agent.id)}`);
    },
  });
  const [selected, setSelected] = useState<Set<string>>(() => new Set());
  const [bulk, setBulk] = useState<AgentInfo[] | null>(null);
  // the checkbox column shows when the member may delete some row at all
  const picking = all.some((a) => perms.canDelete(a));
  const pickable = rows.filter((a) => bulkDeletable(a, perms)).map((a) => a.id);
  const picked = liveSelection(selected, rows, perms);
  const allPicked = pickable.length > 0 && pickable.every((id) => selected.has(id));
  // a click on a row is "view": the detail page, keeping the list's filters for the way back
  const detailHref = (a: AgentInfo) => `/v3/agents?${new URLSearchParams({ ...Object.fromEntries(params), id: a.id })}`;

  return (
    <div className="v3-reveal" style={{ display: "grid", gap: 16 }}>
      <PageHead
        eyebrow={t("v3.agents.eyebrow")}
        title={t("v3.agents.title")}
        sub={t("v3.agents.sub")}
        end={<><button type="button" className="v3-btn" onClick={agents.reload} disabled={agents.loading}><RefreshCw size={14} /> {t("v3.ux.refresh")}</button>
          {/* the classic import page until V3 has its own (§3) */}
          <Reasoned disabledReason={perms.lockTitle ?? t("story.reason.locked")}>
            <button type="button" className="v3-btn" onClick={() => navigate("/agents/import")} disabled={perms.locked} data-testid="v3-agents-import">
              <Download size={14} /> {t("v3.agents.import")}
            </button>
          </Reasoned>
          <Link to="/v3/create" className="v3-btn primary">{t("v3.nav.create")}</Link></>}
      />
      <div style={{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap" }}>
        {(["all", "ok", "wait", "act", "off"] as const).map((f) => (
          <button key={f} type="button" aria-pressed={filter === f} className={`v3-btn sm${filter === f ? "" : " ghost"}`} onClick={() => update("status", f === "all" ? "" : f)}>
            {f !== "all" && <Lamp s={f} />}
            {t(`v3.agents.filter.${f}`)}
            <span className="mono" style={{ color: "var(--v3-text-3)" }}>{agents.data ? (f === "all" ? all.length : count(f)) : "—"}</span>
          </button>
        ))}
        <div className="v3-agent-search">
          <Search size={14} style={{ position: "absolute", left: 12, top: 11, color: "var(--v3-text-3)" }} aria-hidden="true" />
          <input className="v3-input" style={{ paddingLeft: 34 }} value={q} onChange={(e) => update("q", e.target.value)}
            placeholder={t("v3.agents.search")} aria-label={t("v3.agents.search")} />
        </div>
      </div>
      {picked.length > 0 && (
        <div className="v3-agents-bulk" data-testid="v3-agents-bulk">
          <span aria-live="polite">{t("v3.agents.bulk.selected", { count: picked.length })}</span>
          <button type="button" className="v3-btn sm danger" onClick={() => setBulk(picked)} data-testid="v3-agents-bulk-delete">
            <Trash2 size={14} /> {t("v3.agents.bulk.delete", { count: picked.length })}
          </button>
          <button type="button" className="v3-btn sm ghost" onClick={() => setSelected(new Set())}>{t("v3.agents.bulk.clear")}</button>
        </div>
      )}
      <div className="v3-list-summary">
        <span aria-live="polite">{agents.data ? t("v3.ux.results", { count: rows.length, total: all.length }) : t("v3.home.loading")}</span>
        <label><ArrowUpDown size={14} /> <span>{t("v3.ux.sort")}</span>
          <select className="v3-input" value={sort} onChange={(e) => update("sort", e.target.value === "updated" ? "" : e.target.value)}>
            <option value="updated">{t("v3.ux.updatedFirst")}</option><option value="name">{t("v3.ux.nameOrder")}</option>
          </select>
        </label>
      </div>
      <Panel flush className="v3-agent-list">
        {agents.loading && !agents.data ? (
          <div style={{ padding: 20 }}><Skeleton rows={6} /></div>
        ) : agents.error ? (
          <div style={{ padding: 20 }}><Notice s="act">{agents.error} <button type="button" className="v3-btn sm" onClick={agents.reload}>{t("v3.ux.retry")}</button></Notice></div>
        ) : rows.length === 0 ? (
          <Empty title={t(all.length === 0 ? "v3.home.noAgents" : "v3.agents.none")}>
            {all.length === 0 ? <Link to="/v3/create" className="v3-btn primary">{t("v3.nav.create")}</Link> :
              <button type="button" className="v3-btn" onClick={() => setParams({})}>{t("v3.ux.clearFilters")}</button>}
          </Empty>
        ) : (
          <div className="v3-table-scroll" role="region" aria-label={t("v3.ux.tableScroll")} tabIndex={0}>
          <table className="v3-table">
            <thead>
              <tr>
                {picking && (
                  <th className="pick">
                    <Reasoned disabledReason={perms.locked ? perms.lockTitle ?? t("story.reason.locked") : t("story.reason.nothingToPick")}>
                      <input type="checkbox" checked={allPicked} disabled={pickable.length === 0}
                        onChange={() => setSelected((prev) => toggleSelection(prev, pickable))}
                        aria-label={t("v3.agents.bulk.selectAll")} data-testid="v3-agents-pick-all" />
                    </Reasoned>
                  </th>
                )}
                <th>{t("v3.ux.status")}</th>
                <th>{t("v3.agents.name")}</th>
                <th>{t("v3.agents.method")}</th>
                <th className="num">{t("v3.agents.version")}</th>
                <th>{t("v3.agents.owner")}</th>
                <th className="num">{t("v3.agents.updated")}</th>
                <th className="end" aria-label={t("v3.agents.actions")} />
              </tr>
            </thead>
            <tbody>
              {rows.map((a) => (
                <tr key={a.id} className="v3-agents-row" data-testid={`v3-agents-tr-${a.id}`}
                  onClick={(e) => { if (opensDetail(e.target as Element)) navigate(detailHref(a)); }}>
                  {picking && (
                    <td className="pick">
                      {perms.canDelete(a) && (
                        <Reasoned disabledReason={perms.locked ? perms.lockTitle ?? t("story.reason.locked") : t("v3.agent.editBlocked.deploying")}>
                          <input type="checkbox" checked={selected.has(a.id) && bulkDeletable(a, perms)} disabled={!bulkDeletable(a, perms)}
                            onChange={() => setSelected((prev) => toggleSelection(prev, a.id))}
                            aria-label={t("v3.agents.bulk.select", { name: a.display_name || a.name })} data-testid={`v3-agents-pick-${a.id}`} />
                        </Reasoned>
                      )}
                    </td>
                  )}
                  <td><Chip s={agentSignal(a)}><Lamp s={agentSignal(a)} live={a.status === "active"} /> {t(`v3.agents.filter.${agentSignal(a)}`)}</Chip></td>
                  <td>
                    <div className="v3-name">
                      <div>
                        <Link className="v3-inline-link" to={detailHref(a)}><b>{a.display_name || a.name}</b></Link>
                        <small>{a.status === "failed" ? (a.error ?? "").slice(0, 80) : a.name}</small>
                      </div>
                    </div>
                  </td>
                  <td><Chip>{a.method}</Chip></td>
                  <td className="num">v{a.version ?? "—"}</td>
                  <td className="mono" style={{ color: "var(--v3-text-2)" }}>{a.created_by_name || a.owner || "—"}</td>
                  <td className="num" style={{ color: "var(--v3-text-3)" }}>{ago(a.updated_at)}</td>
                  <td className="end">
                    <div className="v3-agents-row-actions">
                      {rowChat(a) && (
                        <Link to={`/v3/chat?agent=${encodeURIComponent(a.id)}`} className="v3-btn sm" data-testid={`v3-agents-chat-${a.id}`}>
                          <MessagesSquare size={14} /> {t("v3.agent.chat")}
                        </Link>
                      )}
                      <RowMenu agent={a} perms={perms} actions={actions} />
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          </div>
        )}
      </Panel>
      <ActionDialog actions={actions} />
      {bulk && (
        <BulkDeleteDialog
          agents={bulk}
          onCancel={() => setBulk(null)}
          onDone={({ deleted, failed }) => {
            if (deleted.length) toast("ok", t("v3.agents.bulk.deleted", { count: deleted.length }));
            setSelected((prev) => new Set([...prev].filter((id) => failed.some((f) => f.agent.id === id))));
            if (!failed.length) setBulk(null);
            agents.reload();
          }}
        />
      )}
    </div>
  );
}

export function V3Agents() {
  const [params] = useSearchParams();
  const id = params.get("id");
  return id ? <AgentDetail key={id} id={id} /> : <AgentList />;
}
