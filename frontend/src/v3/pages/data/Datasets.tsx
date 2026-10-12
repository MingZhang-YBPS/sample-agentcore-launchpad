import { ArrowLeft, Copy, Pencil, Plus, Search, Trash2 } from "lucide-react";
import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link, useSearchParams } from "react-router-dom";

import { api, errorMessage, type V2Dataset } from "../../../lib/api";
import { useWorkspace } from "../../../workspace/workspace-context";
import { ago } from "../../format";
import { useLoad, useToast } from "../../hooks";
import { Btn, Chip, Confirm, Dialog, Empty, Filters, Lamp, Notice, PageHead, Panel, Skeleton, Stat } from "../../ui";
import { cloudSignal, datasetOrigin, datasetRows } from "./common";

const ROWS = 50;
const editHref = (id: string) => `/v2/eval/data?tab=datasets&view=dataset-edit&id=${encodeURIComponent(id)}`;

function useCloudLabel() {
  const { t } = useTranslation();
  return (ds: V2Dataset) =>
    !ds.cloud?.dataset_id ? t("v2.datasets.cloudLocal") : ds.cloud.draft_status === "MODIFIED" ? t("v2.datasets.cloudModified") : t("v2.datasets.cloudSynced");
}

export function DatasetsTab() {
  const { t } = useTranslation();
  const toast = useToast();
  const [, setParams] = useSearchParams();
  const { current } = useWorkspace();
  const cloudLabel = useCloudLabel();
  const list = useLoad(() => api.v2Datasets(), `v3-data-datasets:${current?.id ?? ""}`);
  const [kind, setKind] = useState("all");
  const [origin, setOrigin] = useState<"all" | "trace" | "manual">("all");
  const [q, setQ] = useState("");
  const [deleting, setDeleting] = useState<V2Dataset | null>(null);
  const [busy, setBusy] = useState(false);
  const all = useMemo(() => list.data?.datasets ?? [], [list.data]);
  const rows = useMemo(() => {
    const needle = q.trim().toLowerCase();
    return all.filter((ds) => {
      if (kind !== "all" && ds.kind !== kind) return false;
      if (origin !== "all" && datasetOrigin(ds) !== origin) return false;
      return !needle || `${ds.name} ${ds.description}`.toLowerCase().includes(needle);
    });
  }, [all, kind, origin, q]);
  const remove = async () => {
    if (!deleting) return;
    setBusy(true);
    try {
      await api.v2DeleteDataset(deleting.id);
      toast("ok", t("v2.datasets.deleted"));
      setDeleting(null);
      list.reload();
    } catch (err) {
      toast("act", errorMessage(err));
    } finally {
      setBusy(false);
    }
  };
  const open = (ds: V2Dataset) => setParams({ tab: "datasets", view: "dataset", id: ds.id });
  const count = (k: string) => all.filter((ds) => ds.kind === k).length;

  return (
    <div style={{ display: "grid", gap: 16 }}>
      <div style={{ display: "flex", gap: 12, alignItems: "center", flexWrap: "wrap" }}>
        <Filters
          value={kind}
          onChange={setKind}
          options={[
            { value: "all", label: t("v3.data.allKinds"), count: all.length },
            ...["predefined", "legacy", "simulated"].map((k) => ({ value: k, label: t(`v2.datasets.kind.${k}`), count: count(k) })),
          ]}
        />
        <span style={{ width: 1, height: 20, background: "var(--v3-line)" }} aria-hidden="true" />
        <Filters
          value={origin}
          onChange={setOrigin}
          options={[
            { value: "all", label: t("v3.data.anyOrigin") },
            { value: "trace", label: t("v2.datasets.origin.trace") },
            { value: "manual", label: t("v2.datasets.origin.manual") },
          ]}
        />
        <div style={{ marginLeft: "auto", display: "flex", gap: 10, alignItems: "center" }}>
          <div style={{ position: "relative", width: 240 }}>
            <Search size={14} style={{ position: "absolute", left: 12, top: 11, color: "var(--v3-text-3)" }} aria-hidden="true" />
            <input className="v3-input" style={{ paddingLeft: 34 }} value={q} onChange={(e) => setQ(e.target.value)}
              placeholder={t("v2.datasets.search")} aria-label={t("v2.datasets.search")} />
          </div>
          <Link to="/v2/eval/data?tab=datasets&view=dataset-new" className="v3-btn primary"><Plus size={14} /> {t("v2.datasets.new")}</Link>
        </div>
      </div>
      <Panel flush>
        {list.loading && !list.data ? (
          <div style={{ padding: 20 }}><Skeleton rows={5} /></div>
        ) : list.error ? (
          <div style={{ padding: 20 }}><Notice s="act">{list.error}</Notice></div>
        ) : rows.length === 0 ? (
          <Empty title={t("v2.datasets.empty")} />
        ) : (
          <table className="v3-table">
            <thead>
              <tr>
                <th />
                <th>{t("v2.datasets.colName")}</th>
                <th>{t("v2.datasets.colKind")}</th>
                <th>{t("v2.datasets.colOrigin")}</th>
                <th className="num">{t("v2.datasets.colCount")}</th>
                <th>{t("v2.datasets.colGt")}</th>
                <th>{t("v2.datasets.colCloud")}</th>
                <th className="num">{t("v3.data.created")}</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {rows.map((ds) => {
                const s = cloudSignal(ds);
                return (
                  <tr key={ds.id} className="click" onClick={() => open(ds)}>
                    <td style={{ width: 24 }}><Lamp s={s === "off" ? "info" : s} /></td>
                    <td><div className="v3-name"><div><b>{ds.name}</b><small>{ds.description ? ds.description.slice(0, 90) : ds.id}</small></div></div></td>
                    <td><Chip>{t(`v2.datasets.kind.${ds.kind}`, { defaultValue: ds.kind })}</Chip></td>
                    <td style={{ color: "var(--v3-text-2)" }}>{t(`v2.datasets.origin.${datasetOrigin(ds)}`)}</td>
                    <td className="num">{ds.item_count}</td>
                    <td>{ds.has_ground_truth ? <Chip s="ok">{t("v2.common.yes")}</Chip> : <span style={{ color: "var(--v3-text-3)" }}>{t("v2.common.no")}</span>}</td>
                    <td>{s === "off" ? <span style={{ color: "var(--v3-text-3)" }}>{cloudLabel(ds)}</span> : <Chip s={s}>{cloudLabel(ds)}</Chip>}</td>
                    <td className="num" style={{ color: "var(--v3-text-3)" }}>{ago(ds.created_at)}</td>
                    <td style={{ width: 1, whiteSpace: "nowrap" }} onClick={(e) => e.stopPropagation()}>
                      <div style={{ display: "flex", gap: 4, justifyContent: "flex-end" }}>
                        {ds.kind !== "simulated" && (
                          <Link to={editHref(ds.id)} className="v3-btn ghost sm" title={t("v3.data.edit")}><Pencil size={13} /></Link>
                        )}
                        <Btn size="sm" kind="ghost" onClick={() => setDeleting(ds)} title={t("v3.data.delete")}><Trash2 size={13} /></Btn>
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </Panel>
      {deleting && (
        <Confirm title={t("v2.datasets.deleteTitle")} confirmLabel={t("v3.data.delete")} cancelLabel={t("v3.common.cancel")} danger busy={busy}
          onCancel={() => setDeleting(null)} onConfirm={() => void remove()}>
          {t("v2.datasets.deleteBody", { name: deleting.name })}
        </Confirm>
      )}
    </div>
  );
}

/** One dataset, read-only, with "copy the picked rows into a new dataset" as V2 has it. */
export function DatasetDetail({ id }: { id: string }) {
  const { t } = useTranslation();
  const toast = useToast();
  const [, setParams] = useSearchParams();
  const cloudLabel = useCloudLabel();
  const list = useLoad(() => api.v2Datasets(), `v3-data-dataset:${id}`);
  const ds = list.data?.datasets.find((d) => d.id === id) ?? null;
  const rows = useMemo(() => (ds ? datasetRows(ds) : []), [ds]);
  const [limit, setLimit] = useState(ROWS);
  const [picked, setPicked] = useState<Set<number>>(new Set());
  const [copyName, setCopyName] = useState<string | null>(null);
  const [copying, setCopying] = useState(false);

  if (list.loading && !list.data) return <Skeleton rows={6} />;
  if (list.error) return <Notice s="act">{list.error}</Notice>;
  if (!ds) return <Notice s="act">{t("v2.datasets.notFound")}</Notice>;
  const simulated = ds.kind === "simulated";
  const toggle = (i: number) =>
    setPicked((prev) => {
      const next = new Set(prev);
      if (next.has(i)) next.delete(i);
      else next.add(i);
      return next;
    });
  // the picked rows' FULL stored items (assertions, every turn, the expected
  // trajectory) — what the row editor cannot re-type
  const copy = async () => {
    if (!copyName?.trim()) return;
    setCopying(true);
    try {
      const items = [...picked].sort((a, b) => a - b).map((i) => {
        const item = structuredClone(rows[i].original ?? {}) as Record<string, unknown>;
        const meta = (item.metadata && typeof item.metadata === "object" ? item.metadata : {}) as Record<string, unknown>;
        item.metadata = { ...meta, copied_from: { dataset_id: ds.id, dataset_name: ds.name, row: i + 1 } };
        return item;
      });
      const created = await api.v2CreateDataset({
        name: copyName.trim(),
        description: t("v2.datasets.copyDescription", { name: ds.name, count: items.length }),
        locale: ds.locale,
        items,
      });
      toast("ok", t("v2.datasets.copied", { name: created.name, count: items.length }));
      setCopyName(null);
      setPicked(new Set());
      setParams({ tab: "datasets", view: "dataset", id: created.id });
    } catch (err) {
      toast("act", errorMessage(err));
    } finally {
      setCopying(false);
    }
  };
  const s = cloudSignal(ds);

  return (
    <div className="v3-reveal" style={{ display: "grid", gap: 16 }}>
      <div>
        <button type="button" className="v3-btn ghost sm" onClick={() => setParams({ tab: "datasets" })}>
          <ArrowLeft size={14} /> {t("v3.data.title")}
        </button>
      </div>
      <PageHead
        eyebrow={`${t(`v2.datasets.kind.${ds.kind}`, { defaultValue: ds.kind })} · ${ds.id}`}
        title={ds.name}
        sub={ds.description || undefined}
        end={
          <>
            {!simulated && <Link to={`/v2/eval/tasks?view=new&dataset=${encodeURIComponent(ds.id)}`} className="v3-btn">{t("v2.datasets.useInTask")}</Link>}
            <Btn disabled={simulated || picked.size === 0} title={picked.size === 0 ? t("v2.datasets.copyPickHint") : undefined}
              onClick={() => setCopyName(`${ds.name}-subset`.slice(0, 64))}>
              <Copy size={14} /> {t("v2.datasets.copySelected", { count: picked.size })}
            </Btn>
            {!simulated && <Link to={editHref(ds.id)} className="v3-btn primary"><Pencil size={14} /> {t("v3.data.edit")}</Link>}
          </>
        }
      />
      <div className="v3-grid c4">
        <Panel><Stat label={t("v2.datasets.colCount")} value={ds.item_count} foot={t(`v2.datasets.origin.${datasetOrigin(ds)}`)} /></Panel>
        <Panel signal={ds.has_ground_truth ? "ok" : undefined}>
          <Stat label={t("v2.datasets.colGt")} value={ds.has_ground_truth ? t("v2.common.yes") : t("v2.common.no")} foot={t("v3.data.gtFoot")} />
        </Panel>
        <Panel signal={s === "off" ? undefined : s}><Stat label={t("v2.datasets.colCloud")} value={cloudLabel(ds)} /></Panel>
        <Panel><Stat label={t("v3.data.created")} value={ago(ds.created_at)} foot={ds.locale} /></Panel>
      </div>
      <Panel title={t("v2.datasets.records")} flush end={<span className="mono">{ds.item_count}</span>}>
        {rows.length === 0 ? (
          <Empty title={t("v3.data.noRows")} />
        ) : (
          <>
            <table className="v3-table">
              <thead>
                <tr>
                  <th style={{ width: 36 }}>
                    <input type="checkbox" aria-label={t("v2.datasets.pickAll")} disabled={simulated}
                      checked={rows.length > 0 && picked.size === rows.length}
                      onChange={(e) => setPicked(e.target.checked ? new Set(rows.map((_, i) => i)) : new Set())} />
                  </th>
                  <th className="num" style={{ width: 48 }}>#</th>
                  <th>Input</th>
                  <th>{t("v2.datasets.expected")}</th>
                  <th>{t("v2.datasets.turns")}</th>
                </tr>
              </thead>
              <tbody>
                {rows.slice(0, limit).map((r, i) => (
                  <tr key={i}>
                    <td><input type="checkbox" aria-label={t("v2.datasets.pickRow", { n: i + 1 })} disabled={simulated} checked={picked.has(i)} onChange={() => toggle(i)} /></td>
                    <td className="num" style={{ color: "var(--v3-text-3)" }}>{i + 1}</td>
                    <td><span className="v3-dc-clip" title={r.input}>{r.input}</span></td>
                    <td>{r.expected ? <span className="v3-dc-clip" title={r.expected}>{r.expected}</span> : <span style={{ color: "var(--v3-text-3)" }}>—</span>}</td>
                    <td style={{ color: "var(--v3-text-2)", whiteSpace: "nowrap" }}>
                      {r.extraTurns ? t("v2.datasets.turnCount", { count: r.extraTurns + 1 }) : t("v2.datasets.singleTurn")}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            {rows.length > limit && (
              <div style={{ padding: 14, display: "flex", justifyContent: "center" }}>
                <Btn size="sm" onClick={() => setLimit((n) => n + ROWS)}>{t("v3.data.more", { count: rows.length - limit })}</Btn>
              </div>
            )}
          </>
        )}
      </Panel>
      {copyName !== null && (
        <Dialog
          title={t("v2.datasets.copyTitle", { count: picked.size })}
          onClose={() => setCopyName(null)}
          foot={
            <>
              <Btn kind="ghost" onClick={() => setCopyName(null)} disabled={copying}>{t("v3.common.cancel")}</Btn>
              <Btn kind="primary" disabled={copying || !copyName.trim()} onClick={() => void copy()}>{t("v2.datasets.copyConfirm")}</Btn>
            </>
          }
        >
          <div style={{ display: "grid", gap: 12 }}>
            <Notice>{t("v2.datasets.copyHint")}</Notice>
            <label className="v3-field">
              <span>{t("v2.datasets.colName")}</span>
              <input className="v3-input" value={copyName} maxLength={64} onChange={(e) => setCopyName(e.target.value)} />
            </label>
          </div>
        </Dialog>
      )}
    </div>
  );
}
