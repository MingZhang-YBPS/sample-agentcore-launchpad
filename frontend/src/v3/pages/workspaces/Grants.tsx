import { Search } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { useSearchParams } from "react-router-dom";

import { api, ApiError, errorMessage, type WorkspaceGrantFilter, type WorkspaceGrants } from "../../../lib/api";
import { useToast } from "../../hooks";
import { Btn, Chip, Empty, Filters, Notice, Panel, Skeleton } from "../../ui";

const PAGE_SIZE = 10;

/**
 * Member access to one workspace — V2's GrantsCard on V3: search / filter / page
 * are server-side and live in the URL (`gq`, `granted`, `gpage`); the checkbox
 * selection is page-local and drives batch grant / revoke.
 */
export function Grants({ workspaceId, onTotal }: { workspaceId: string; onTotal: (total: number | null) => void }) {
  const { t } = useTranslation();
  const toast = useToast();
  const [params, setParams] = useSearchParams();
  const query = params.get("gq") ?? "";
  const filterParam = params.get("granted");
  const filter: WorkspaceGrantFilter = filterParam === "granted" || filterParam === "ungranted" ? filterParam : "all";
  const pageNo = Math.max(1, Number(params.get("gpage") ?? "1") || 1);
  const [grants, setGrants] = useState<WorkspaceGrants | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState(false);
  const seq = useRef(0);

  const load = useCallback(async () => {
    const id = ++seq.current;
    setLoading(true);
    try {
      const page = await api.listWorkspaceGrants(workspaceId, {
        q: query || undefined, granted: filter, limit: PAGE_SIZE, offset: (pageNo - 1) * PAGE_SIZE,
      });
      if (id !== seq.current) return; // ignore out-of-order responses
      setGrants(page);
      setError(null);
      onTotal(page.granted_total);
    } catch (err) {
      if (id !== seq.current) return;
      setGrants(null);
      onTotal(null);
      // a gone workspace is expected (detached elsewhere); the empty state says so
      setError(err instanceof ApiError && err.code === "workspace.not_found" ? null : errorMessage(err));
    } finally {
      if (id === seq.current) setLoading(false);
    }
  }, [filter, onTotal, pageNo, query, workspaceId]);
  useEffect(() => {
    void load();
  }, [load]);
  // selection acts on rows the operator can see: a page / search / filter change starts fresh
  useEffect(() => {
    setSelected(new Set());
  }, [filter, pageNo, query]);

  const setParam = (key: string, value: string | null) =>
    setParams(
      (prev) => {
        const next = new URLSearchParams(prev);
        if (value) next.set(key, value);
        else next.delete(key);
        if (key !== "gpage") next.delete("gpage");
        return next;
      },
      { replace: true },
    );

  const rows = grants?.users ?? [];
  const pageIds = rows.map((u) => u.id);
  const allOnPage = pageIds.length > 0 && pageIds.every((id) => selected.has(id));
  const toggle = (id: string) =>
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  const apply = async (action: "grant" | "revoke") => {
    const ids = [...selected];
    if (ids.length === 0) return;
    setBusy(true);
    try {
      const result = await api.updateWorkspaceGrants(workspaceId, { [action]: ids });
      // the count is the selection: re-granting a holder changes no row but is what was asked
      toast("ok", t(action === "grant" ? "workspacesPage.detail.grantsGranted" : "workspacesPage.detail.grantsRevoked", {
        count: ids.length, total: result.granted_total,
      }));
      setSelected(new Set());
      await load();
    } catch (err) {
      toast("act", errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  const total = grants?.total ?? 0;
  const pages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  return (
    <Panel title={t("v2.workspaces.grantsTitle")} flush
      end={<span>{t("workspacesPage.detail.grantsSub", { granted: grants?.granted_total ?? 0 })}</span>}>
      <div style={{ padding: "0 20px 14px", display: "grid", gap: 12 }}>
        <Notice>{t("workspacesPage.detail.adminHint")}</Notice>
        <div style={{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap" }}>
          <Filters
            value={filter}
            onChange={(v) => setParam("granted", v === "all" ? null : v)}
            options={[
              { value: "all", label: t("v3.workspaces.all") },
              { value: "granted", label: t("workspacesPage.detail.grantFilters.granted"), s: "ok" },
              { value: "ungranted", label: t("workspacesPage.detail.grantFilters.ungranted"), s: "off" },
            ]}
          />
          <div style={{ position: "relative", flex: "1 1 180px", maxWidth: 280 }}>
            <Search size={14} style={{ position: "absolute", left: 12, top: 11, color: "var(--v3-text-3)" }} aria-hidden="true" />
            <input className="v3-input" style={{ paddingLeft: 34 }} value={query} onChange={(e) => setParam("gq", e.target.value || null)}
              placeholder={t("workspacesPage.detail.grantsSearch")} aria-label={t("workspacesPage.detail.grantsSearch")} />
          </div>
          <span style={{ marginLeft: "auto", display: "inline-flex", gap: 8, alignItems: "center" }}>
            <span className="mono" style={{ color: "var(--v3-text-3)" }}>{t("v2.workspaces.selected", { count: selected.size })}</span>
            <Btn size="sm" kind="primary" disabled={selected.size === 0 || busy} onClick={() => void apply("grant")}>{t("v2.workspaces.grant")}</Btn>
            <Btn size="sm" kind="danger" disabled={selected.size === 0 || busy} onClick={() => void apply("revoke")}>{t("v2.workspaces.revoke")}</Btn>
          </span>
        </div>
      </div>
      {loading && !grants ? (
        <div style={{ padding: 20 }}><Skeleton rows={3} /></div>
      ) : error ? (
        <div style={{ padding: 20 }}><Notice s="act">{error}</Notice></div>
      ) : rows.length === 0 ? (
        <Empty title={query || filter !== "all" ? t("workspacesPage.detail.noMatchingAccounts") : t("workspacesPage.detail.noAccounts")} />
      ) : (
        <table className="v3-table">
          <thead>
            <tr>
              <th style={{ width: 40 }}>
                <input type="checkbox" checked={allOnPage} disabled={pageIds.length === 0}
                  onChange={() => setSelected(allOnPage ? new Set() : new Set(pageIds))} aria-label={t("workspacesPage.detail.selectPage")} />
              </th>
              <th>{t("v2.workspaces.grantCol.account")}</th>
              <th>{t("v2.workspaces.grantCol.email")}</th>
              <th>{t("v2.workspaces.grantCol.status")}</th>
              <th>{t("v2.workspaces.grantCol.granted")}</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((u) => (
              <tr key={u.id} className="click" onClick={() => toggle(u.id)}>
                <td onClick={(e) => e.stopPropagation()}>
                  <input type="checkbox" checked={selected.has(u.id)} onChange={() => toggle(u.id)} aria-label={u.username} />
                </td>
                <td><b>{u.username}</b></td>
                <td style={{ color: "var(--v3-text-3)" }}>{u.email || "—"}</td>
                <td><Chip s={u.status === "active" ? "ok" : undefined}>{t(`usersPage.filters.${u.status}`)}</Chip></td>
                <td>{u.granted ? <Chip s="ok">{t("v2.workspaces.grantYes")}</Chip> : <span style={{ color: "var(--v3-text-3)" }}>{t("v2.workspaces.grantNo")}</span>}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {total > PAGE_SIZE && (
        <div style={{ display: "flex", gap: 10, justifyContent: "flex-end", alignItems: "center", padding: "12px 16px" }}>
          <Btn size="sm" kind="ghost" disabled={pageNo <= 1} onClick={() => setParam("gpage", pageNo - 1 > 1 ? String(pageNo - 1) : null)}>←</Btn>
          <span className="mono">{pageNo} / {pages}</span>
          <Btn size="sm" kind="ghost" disabled={pageNo >= pages} onClick={() => setParam("gpage", String(pageNo + 1))}>→</Btn>
        </div>
      )}
    </Panel>
  );
}
