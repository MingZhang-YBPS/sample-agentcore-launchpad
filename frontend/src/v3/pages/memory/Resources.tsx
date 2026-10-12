import { Plus, RefreshCw, Search } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link, useNavigate } from "react-router-dom";

import { useAuth } from "../../../auth/auth-context";
import { api, errorMessage, type MemoryResourceRow } from "../../../lib/api";
import { isTransient, POLL_MS } from "../../../v2/pages/memory/common";
import { ago } from "../../format";
import { useLoad, useToast } from "../../hooks";
import { Btn, Chip, Confirm, Empty, Lamp, Notice, Panel, Skeleton } from "../../ui";
import { memSignal } from "./signal";

// resource detail and its editor stay the hosted V2 sub-pages
const detailHref = (id: string) => `/v2/memory?view=resource&id=${encodeURIComponent(id)}`;
const editHref = (id: string) => `/v2/memory?view=resource-edit&id=${encodeURIComponent(id)}`;

/** Why a row cannot be deleted, or null when it can (the same rule as V2). */
function deleteBlock(t: (k: string) => string, row: MemoryResourceRow): string | null {
  if (row.is_default) return t("v2.memory.res.defaultProtected");
  if (row.agents.length > 0) return t("memoryPage.resources.inUseHint");
  if ((row.status ?? "").toUpperCase() === "DELETING") return t("v2.memory.res.deleting");
  return null;
}

/**
 * Memory resources in this workspace's account/region. The bootstrap memory is
 * the delete-protected default; other *managed* memories (created here or
 * adopted) can be pinned per agent. A memory still used by agents cannot be
 * deleted; anything not managed offers only an administrator's adopt.
 */
export function Resources() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const toast = useToast();
  const { can, isAdmin } = useAuth();
  const mayManage = can("memory.manage");
  const [tick, setTick] = useState(0);
  const list = useLoad(() => api.memoryResources(), `v3-mem-res:${tick}`);
  const [q, setQ] = useState("");
  const [pending, setPending] = useState<{ row: MemoryResourceRow; kind: "delete" | "adopt" } | null>(null);
  const [busy, setBusy] = useState(false);
  const all = useMemo(() => list.data?.items ?? [], [list.data]);
  const rows = useMemo(() => {
    const needle = q.trim().toLowerCase();
    return all.filter((r) => !needle || `${r.name ?? ""} ${r.id ?? ""}`.toLowerCase().includes(needle));
  }, [all, q]);

  // CREATING / DELETING settle on their own: follow them
  const transient = all.some((r) => isTransient(r.status));
  useEffect(() => {
    if (!transient) return;
    const timer = window.setInterval(() => setTick((n) => n + 1), POLL_MS);
    return () => window.clearInterval(timer);
  }, [transient]);

  const run = async () => {
    if (!pending?.row.id) return;
    const { row, kind } = pending;
    setBusy(true);
    try {
      if (kind === "delete") {
        await api.memoryResourceDelete(row.id!);
        toast("ok", t("memoryPage.resources.deleted", { id: row.id }));
      } else {
        await api.memoryResourceAdopt(row.id!);
        toast("ok", t("v2.memory.res.adopted", { id: row.id }));
      }
      setPending(null);
      setTick((n) => n + 1);
    } catch (err) {
      toast("act", t(kind === "delete" ? "memoryPage.resources.deleteFailed" : "v2.memory.res.adoptFailed", { msg: errorMessage(err) }));
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <Notice>{t("memoryPage.resources.note")}</Notice>
      <div style={{ display: "flex", gap: 10, alignItems: "center" }}>
        <div style={{ position: "relative", flex: "1 1 200px", maxWidth: 320 }}>
          <Search size={14} style={{ position: "absolute", left: 12, top: 11, color: "var(--v3-text-3)" }} aria-hidden="true" />
          <input className="v3-input" style={{ paddingLeft: 34 }} value={q} onChange={(e) => setQ(e.target.value)}
            placeholder={t("v3.memory.searchResource")} aria-label={t("v3.memory.searchResource")} />
        </div>
        <span style={{ marginLeft: "auto", display: "inline-flex", gap: 8 }}>
          <Btn kind="ghost" onClick={list.reload}><RefreshCw size={14} /></Btn>
          {mayManage ? (
            <Link className="v3-btn primary" to="/v2/memory?view=resource-new"><Plus size={14} /> {t("v3.memory.newResource")}</Link>
          ) : (
            <Btn kind="primary" disabled title={t("v2.memory.res.noPermission")}><Plus size={14} /> {t("v3.memory.newResource")}</Btn>
          )}
        </span>
      </div>
      <Panel flush>
        {list.loading && !list.data ? (
          <div style={{ padding: 20 }}><Skeleton rows={4} /></div>
        ) : list.error ? (
          <div style={{ padding: 20 }}><Notice s="act">{list.error}</Notice></div>
        ) : rows.length === 0 ? (
          <Empty title={all.length ? t("v2.memory.res.noMatch") : t("memoryPage.resources.empty")} />
        ) : (
          <table className="v3-table">
            <thead>
              <tr>
                <th /><th>{t("v3.memory.resource")}</th><th>{t("v3.memory.usedBy")}</th>
                <th className="num">{t("v3.memory.created")}</th><th className="num">{t("v3.memory.updated")}</th><th />
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => {
                const block = mayManage ? deleteBlock(t, row) : t("v2.memory.res.noPermission");
                return (
                  <tr key={row.id ?? row.arn ?? ""} className={row.managed ? "click" : undefined}
                    onClick={() => row.managed && row.id && navigate(detailHref(row.id))}>
                    <td style={{ width: 30 }}><Lamp s={memSignal(row.status)} live={isTransient(row.status)} /></td>
                    <td>
                      <div className="v3-name">
                        <div>
                          <b style={row.managed ? undefined : { color: "var(--v3-text-3)" }}>
                            {row.name ?? "—"}{" "}
                            {row.is_default && <Chip s="info">{t("v2.memory.res.default")}</Chip>}{" "}
                            {!row.managed && <Chip title={t("v2.memory.res.externalHint")}>{t("v2.memory.res.external")}</Chip>}
                          </b>
                          <small className="mono" title={row.arn ?? ""}>{row.id ?? "—"} · {row.status ?? "—"}</small>
                        </div>
                      </div>
                    </td>
                    <td onClick={(e) => e.stopPropagation()}>
                      {row.is_default ? (
                        <span className="v3-mem-muted">{t("memoryPage.resources.sharedDefault")}</span>
                      ) : row.agents.length === 0 ? (
                        <span className="v3-mem-muted">{t("v2.memory.res.unused")}</span>
                      ) : (
                        <span style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
                          {row.agents.map((a) => <Link key={a.id} to={`/v3/agents?id=${encodeURIComponent(a.id)}`} className="v3-chip" data-s="info">{a.name}</Link>)}
                        </span>
                      )}
                    </td>
                    <td className="num" style={{ color: "var(--v3-text-3)" }}>{ago(row.created_at)}</td>
                    <td className="num" style={{ color: "var(--v3-text-3)" }}>{ago(row.updated_at)}</td>
                    <td style={{ width: 1, whiteSpace: "nowrap" }} onClick={(e) => e.stopPropagation()}>
                      <div style={{ display: "flex", gap: 4, justifyContent: "flex-end" }}>
                        {!row.managed ? (
                          // detected, not managed: only an administrator's adopt applies
                          isAdmin ? <Btn size="sm" disabled={!row.id || busy} onClick={() => setPending({ row, kind: "adopt" })}>{t("v2.memory.res.adopt")}</Btn> : <span className="v3-mem-muted">—</span>
                        ) : (
                          <>
                            {mayManage && row.id && !isTransient(row.status) ? (
                              <Link className="v3-btn sm ghost" to={editHref(row.id)}>{t("v3.memory.edit")}</Link>
                            ) : (
                              <Btn size="sm" kind="ghost" disabled title={mayManage ? undefined : t("v2.memory.res.noPermission")}>{t("v3.memory.edit")}</Btn>
                            )}
                            {/* the bootstrap memory is delete-protected: no delete at all */}
                            {!row.is_default && (
                              <Btn size="sm" kind="ghost" disabled={!row.id || block !== null || busy} title={block ?? undefined}
                                onClick={() => setPending({ row, kind: "delete" })}>{t("v3.memory.delete")}</Btn>
                            )}
                          </>
                        )}
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </Panel>
      {pending && (
        <Confirm
          title={pending.kind === "delete" ? t("memoryPage.resources.deleteTitle") : t("v2.memory.res.adoptTitle")}
          confirmLabel={pending.kind === "delete" ? t("v3.memory.delete") : t("v2.memory.res.adopt")}
          cancelLabel={t("v3.common.cancel")}
          danger={pending.kind === "delete"}
          busy={busy}
          onCancel={() => !busy && setPending(null)}
          onConfirm={() => void run()}
        >
          {pending.kind === "delete"
            ? t("memoryPage.resources.deleteBody", { id: pending.row.id ?? "" })
            : t("v2.memory.res.adoptBody", { id: pending.row.id ?? "" })}
        </Confirm>
      )}
    </>
  );
}
