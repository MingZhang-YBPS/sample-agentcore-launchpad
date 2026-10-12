import "./evaluators.css";

import { Pencil, Plus, Search, Trash2 } from "lucide-react";
import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link, useNavigate, useSearchParams } from "react-router-dom";

import { api, errorMessage, type EvaluatorRow } from "../../lib/api";
import { evaluatorLabel } from "../../lib/evaluators";
import { useWorkspace } from "../../workspace/workspace-context";
import { useLoad, useToast } from "../hooks";
import { Btn, Chip, Confirm, Empty, Filters, Lamp, Notice, PageHead, Panel, type Signal, Skeleton, Stat } from "../ui";
import { evaluatorSignal, LEVELS } from "./evaluators/common";
import { EvaluatorView } from "./evaluators/Detail";
import { EvaluatorEditor } from "./evaluators/Editor";

type Source = EvaluatorRow["source"];

function EvaluatorList() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const toast = useToast();
  const { current } = useWorkspace();
  const list = useLoad(() => api.v2Evaluators(), `v3-evaluators:${current?.id ?? ""}`);
  const [source, setSource] = useState<"all" | Source>("all");
  const [level, setLevel] = useState("all");
  const [q, setQ] = useState("");
  const [deleting, setDeleting] = useState<EvaluatorRow | null>(null);
  const [busy, setBusy] = useState(false);
  const all = useMemo(() => list.data?.evaluators ?? [], [list.data]);
  const name = (row: EvaluatorRow) => (row.source === "custom" ? (row.name ?? row.id) : evaluatorLabel(t, row.id));
  const rows = useMemo(() => {
    const needle = q.trim().toLowerCase();
    const rank: Record<Signal, number> = { act: 0, wait: 1, ok: 2, info: 3, off: 4 };
    return all
      .filter((row) => (source === "all" || row.source === source) && (level === "all" || row.level === level))
      .filter((row) => !needle || `${row.id} ${row.name ?? ""} ${evaluatorLabel(t, row.id)}`.toLowerCase().includes(needle))
      // custom ones first (they are the workspace's own), a failing one before all
      .sort((a, b) => rank[evaluatorSignal(a)] - rank[evaluatorSignal(b)] || Number(b.source === "custom") - Number(a.source === "custom"));
  }, [all, source, level, q, t]);
  const custom = all.filter((r) => r.source === "custom");
  const failing = custom.filter((r) => evaluatorSignal(r) === "act");
  const countSource = (s: Source) => all.filter((r) => r.source === s).length;

  const remove = async () => {
    if (!deleting) return;
    setBusy(true);
    try {
      await api.v2DeleteEvaluator(deleting.id);
      toast("ok", t("v2.evaluators.deleted"));
      setDeleting(null);
      list.reload();
    } catch (err) {
      toast("act", errorMessage(err));
    } finally {
      setBusy(false);
    }
  };
  const open = (row: EvaluatorRow) => navigate(`/v3/evaluators?view=detail&id=${encodeURIComponent(row.id)}`);

  return (
    <div className="v3-reveal" style={{ display: "grid", gap: 16 }}>
      <PageHead
        eyebrow={t("v3.evaluators.eyebrow")}
        title={t("v2.evaluators.title")}
        sub={t("v2.evaluators.desc")}
        end={<Link to="/v3/evaluators?view=new" className="v3-btn primary"><Plus size={14} /> {t("v2.evaluators.new")}</Link>}
      />
      <div className="v3-grid c4">
        <Panel><Stat label={t("v3.evaluators.total")} value={list.data ? all.length : "—"} foot={t("v3.evaluators.totalFoot")} /></Panel>
        <Panel signal={custom.length ? "info" : undefined}><Stat label={t("v2.evaluators.source.custom")} value={list.data ? custom.length : "—"} foot={t("v3.evaluators.customFoot")} /></Panel>
        <Panel><Stat label={t("v3.evaluators.needGt")} value={list.data ? all.filter((r) => r.requires_ground_truth).length : "—"} foot={t("v3.evaluators.needGtFoot")} /></Panel>
        <Panel signal={failing.length ? "act" : undefined}>
          <Stat label={t("v3.evaluators.failing")} value={list.data ? failing.length : "—"} signal={failing.length ? "act" : undefined} foot={t("v3.evaluators.failingFoot")} />
        </Panel>
      </div>
      <div style={{ display: "flex", gap: 12, alignItems: "center", flexWrap: "wrap" }}>
        <Filters
          value={source}
          onChange={setSource}
          options={[
            { value: "all", label: t("v3.evaluators.allSources"), count: all.length },
            ...(["custom", "builtin", "third_party"] as const).map((s) => ({ value: s, label: t(`v2.evaluators.source.${s}`), count: countSource(s) })),
          ]}
        />
        <span style={{ width: 1, height: 20, background: "var(--v3-line)" }} aria-hidden="true" />
        <Filters
          value={level}
          onChange={setLevel}
          options={[{ value: "all", label: t("v3.evaluators.allLevels") }, ...LEVELS.map((l) => ({ value: l, label: t(`v2.level.${l}`) }))]}
        />
        <div style={{ marginLeft: "auto", position: "relative", flex: "1 1 200px", maxWidth: 300 }}>
          <Search size={14} style={{ position: "absolute", left: 12, top: 11, color: "var(--v3-text-3)" }} aria-hidden="true" />
          <input className="v3-input" style={{ paddingLeft: 34 }} value={q} onChange={(e) => setQ(e.target.value)}
            placeholder={t("v2.evaluators.search")} aria-label={t("v2.evaluators.search")} />
        </div>
      </div>
      <Panel flush>
        {list.loading && !list.data ? (
          <div style={{ padding: 20 }}><Skeleton rows={6} /></div>
        ) : list.error ? (
          <div style={{ padding: 20 }}><Notice s="act">{list.error}</Notice></div>
        ) : rows.length === 0 ? (
          <Empty title={t("v3.evaluators.none")} />
        ) : (
          <table className="v3-table">
            <thead>
              <tr>
                <th />
                <th>{t("v2.evaluators.colName")}</th>
                <th>{t("v2.evaluators.colSource")}</th>
                <th>{t("v2.evaluators.colLevel")}</th>
                <th>{t("v2.evaluators.colDefinition")}</th>
                <th>{t("v2.evaluators.colGroundTruth")}</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => {
                const s = evaluatorSignal(row);
                return (
                  <tr key={row.id} className="click" onClick={() => open(row)}>
                    <td style={{ width: 24 }}><Lamp s={s} /></td>
                    <td><div className="v3-name"><div><b>{name(row)}</b><small>{row.id}</small></div></div></td>
                    <td><Chip s={row.source === "custom" ? "info" : undefined}>{t(`v2.evaluators.source.${row.source}`)}</Chip></td>
                    <td style={{ color: "var(--v3-text-2)" }}>{t(`v2.level.${row.level}`, { defaultValue: row.level })}</td>
                    <td style={{ color: "var(--v3-text-2)" }}>{row.definition ? t(`v2.evaluators.definition.${row.definition}`) : t("v2.evaluators.definition.managed")}</td>
                    <td>{row.requires_ground_truth ? <Chip s="wait">{t("v2.evaluators.needsGt")}</Chip> : <span style={{ color: "var(--v3-text-3)" }}>{t("v2.common.no")}</span>}</td>
                    <td style={{ width: 1, whiteSpace: "nowrap" }} onClick={(e) => e.stopPropagation()}>
                      {row.source === "custom" && (
                        <div style={{ display: "flex", gap: 4, justifyContent: "flex-end" }}>
                          <Link to={`/v3/evaluators?view=edit&id=${encodeURIComponent(row.id)}`} className="v3-btn ghost sm" title={t("v3.evaluators.edit")}><Pencil size={13} /></Link>
                          <Btn size="sm" kind="ghost" onClick={() => setDeleting(row)} title={t("v3.evaluators.delete")}><Trash2 size={13} /></Btn>
                        </div>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </Panel>
      {deleting && (
        <Confirm title={t("v2.evaluators.deleteTitle")} confirmLabel={t("v3.evaluators.delete")} cancelLabel={t("v3.common.cancel")} danger busy={busy}
          onCancel={() => setDeleting(null)} onConfirm={() => void remove()}>
          {t("v2.evaluators.deleteBody", { name: deleting.name ?? deleting.id })}
        </Confirm>
      )}
    </div>
  );
}

/**
 * Evaluators — the managed catalog and the workspace's own custom evaluators
 * (LLM judge, derived, code). Same `?view=detail|edit|new&id=` as V2.
 */
export function V3Evaluators() {
  const [params] = useSearchParams();
  const view = params.get("view");
  const id = params.get("id");
  if (view === "new") return <EvaluatorEditor id={null} />;
  if (view === "edit" && id) return <EvaluatorEditor key={id} id={id} />;
  if (view === "detail" && id) return <EvaluatorView key={id} id={id} />;
  return <EvaluatorList />;
}
