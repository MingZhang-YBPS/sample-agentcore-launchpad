import "./releases.css";

import { ArrowLeft, ArrowRight, Check, Play, RotateCcw, Scale, X } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link, useNavigate, useSearchParams } from "react-router-dom";

import { useAuth } from "../../auth/auth-context";
import {
  api,
  errorMessage,
  type PromotionInfo,
  type PromotionLogLine,
  type PromotionPlan,
  type PromotionStage,
  type SpecDiffRow,
} from "../../lib/api";
import { useWorkspace } from "../../workspace/workspace-context";
import { ago } from "../format";
import { useLoad, useToast } from "../hooks";
import {
  Btn,
  Chip,
  Confirm,
  Dialog,
  Empty,
  Filters,
  Lamp,
  Notice,
  PageHead,
  Panel,
  type Signal,
  Skeleton,
  Stat,
  Track,
  type TrackNode,
} from "../ui";

type Status = PromotionInfo["status"];

/** A release request: pending waits on a reviewer, executing on the platform. */
function promotionSignal(status: Status): Signal {
  switch (status) {
    case "pending":
    case "executing":
      return "wait";
    case "approved":
      return "info";
    case "succeeded":
      return "ok";
    case "failed":
      return "act";
    default:
      return "off";
  }
}

const STAGE_SIGNAL: Record<PromotionStage["status"], Signal> = {
  pending: "off",
  running: "wait",
  succeeded: "ok",
  skipped: "off",
  failed: "act",
};

function agentLabel(p: PromotionInfo): string {
  return p.bundle?.display_name || p.bundle?.agent_name || p.bundle_id;
}

const detailPath = (id: string) => `/v3/releases?id=${encodeURIComponent(id)}`;

function Route({ p }: { p: PromotionInfo }) {
  return (
    <span className="mono v3-rel-route">
      {p.source_workspace_id ?? "—"} <ArrowRight size={11} aria-hidden="true" /> {p.target_workspace_id}
    </span>
  );
}

/* ── desk ────────────────────────────────────────────────────────────────── */

function ReleaseDesk() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const { can } = useAuth();
  const { current } = useWorkspace();
  const list = useLoad(() => api.listPromotions({}), `v3-releases:${current?.id ?? ""}`);
  const [filter, setFilter] = useState<"all" | Signal>("all");
  const rows = useMemo(
    () => [...(list.data?.promotions ?? [])].sort((a, b) => (b.created_at ?? "").localeCompare(a.created_at ?? "")),
    [list.data],
  );
  const approver = can("promotion.approve");
  // what waits on someone: a review, or (for whoever may execute) an approved release
  const pending = rows.filter((r) => r.status === "pending");
  const ready = rows.filter((r) => r.status === "approved");
  const running = rows.filter((r) => r.status === "executing");
  const failed = rows.filter((r) => r.status === "failed");
  const history = rows.filter((r) => filter === "all" || promotionSignal(r.status) === filter);
  const count = (s: Signal) => rows.filter((r) => promotionSignal(r.status) === s).length;

  // an execution moves on its own: follow it while one runs
  const reload = list.reload;
  useEffect(() => {
    if (!running.length) return;
    const timer = window.setInterval(reload, 5000);
    return () => window.clearInterval(timer);
  }, [running.length, reload]);

  const queue = [...pending, ...(approver ? ready : [])];

  return (
    <div className="v3-reveal" style={{ display: "grid", gap: 16 }}>
      <PageHead
        eyebrow={t("v3.releases.eyebrow")}
        title={t("v3.releases.title")}
        sub={t("v3.releases.sub")}
        end={<Link to="/v3/gate" className="v3-btn ghost"><Scale size={14} /> {t("v3.releases.gate")}</Link>}
      />

      <div className="v3-grid c4">
        <Panel signal={pending.length ? "wait" : undefined}>
          <Stat label={t("v3.releases.kpiPending")} value={list.data ? pending.length : "—"} signal={pending.length ? "wait" : undefined}
            foot={t("v3.releases.kpiPendingFoot")} />
        </Panel>
        <Panel signal={ready.length ? "info" : undefined}>
          <Stat label={t("v3.releases.kpiReady")} value={list.data ? ready.length : "—"} foot={t("v3.releases.kpiReadyFoot")} />
        </Panel>
        <Panel signal={running.length ? "wait" : undefined}>
          <Stat label={t("v3.releases.kpiRunning")} value={list.data ? running.length : "—"} foot={t("v3.releases.kpiRunningFoot")} />
        </Panel>
        <Panel signal={failed.length ? "act" : undefined}>
          <Stat label={t("v3.releases.kpiFailed")} value={list.data ? failed.length : "—"} signal={failed.length ? "act" : undefined}
            foot={t("v3.releases.kpiTotal", { count: rows.length })} />
        </Panel>
      </div>

      {list.error && <Notice s="act">{list.error}</Notice>}

      {queue.length > 0 && (
        <Panel title={t("v3.releases.queue")} signal="wait" flush end={<span className="mono">{queue.length}</span>}>
          <table className="v3-table">
            <tbody>
              {queue.map((r) => (
                <tr key={r.id} className="click" onClick={() => navigate(detailPath(r.id))}>
                  <td style={{ width: 30 }}><Lamp s={promotionSignal(r.status)} live /></td>
                  <td>
                    <div className="v3-name">
                      <div>
                        <b>{agentLabel(r)}</b>
                        <small>{r.change_note ? r.change_note.slice(0, 90) : r.bundle?.digest.slice(0, 12)}</small>
                      </div>
                    </div>
                  </td>
                  <td><Route p={r} /></td>
                  <td style={{ color: "var(--v3-text-2)" }}>{r.requested_by ?? "—"}</td>
                  <td>
                    {(r.gates.blocking_failures ?? []).length > 0 ? (
                      <Chip s="wait">{t("v3.releases.gatesAttention", { count: (r.gates.blocking_failures ?? []).length })}</Chip>
                    ) : (
                      <Chip s="ok">{t("v3.releases.gatesOk")}</Chip>
                    )}
                  </td>
                  <td className="num" style={{ color: "var(--v3-text-3)" }}>{ago(r.created_at)}</td>
                  <td style={{ width: 1, whiteSpace: "nowrap" }}>
                    <Btn size="sm" kind={r.status === "approved" ? "primary" : undefined} onClick={() => navigate(detailPath(r.id))}>
                      {r.status === "approved" ? t("v3.releases.toExecute") : t("v3.releases.review")}
                    </Btn>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </Panel>
      )}

      {running.length > 0 && (
        <Panel title={t("v3.releases.inFlight")} signal="wait" flush>
          <table className="v3-table">
            <tbody>
              {running.map((r) => {
                const stages = r.stages ?? [];
                const at = stages.find((s) => s.status === "running") ?? stages.find((s) => s.status === "pending");
                return (
                  <tr key={r.id} className="click" onClick={() => navigate(detailPath(r.id))}>
                    <td style={{ width: 30 }}><Lamp s="wait" live /></td>
                    <td><b>{agentLabel(r)}</b></td>
                    <td><Route p={r} /></td>
                    <td style={{ color: "var(--v3-text-2)" }}>
                      {r.action === "rollback" ? t("v2.promotions.execTitleRollback") : t("v2.promotions.execTitle")}
                      {at ? ` · ${t(`v2.promotions.stage.${at.name}`, { defaultValue: at.name })}` : ""}
                    </td>
                    <td className="num" style={{ color: "var(--v3-text-3)" }}>{ago(r.started_at ?? r.updated_at)}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </Panel>
      )}

      <Filters
        value={filter}
        onChange={setFilter}
        options={[
          { value: "all", label: t("v3.releases.all"), count: rows.length },
          { value: "wait", label: t("v3.releases.fWait"), s: "wait", count: count("wait") },
          { value: "info", label: t("v2.promotions.status.approved"), s: "info", count: count("info") },
          { value: "ok", label: t("v2.promotions.status.succeeded"), s: "ok", count: count("ok") },
          { value: "act", label: t("v2.promotions.status.failed"), s: "act", count: count("act") },
          { value: "off", label: t("v3.releases.fClosed"), s: "off", count: count("off") },
        ]}
      />

      <Panel title={t("v3.releases.history")} flush>
        {list.loading && !list.data ? (
          <div style={{ padding: 20 }}><Skeleton rows={5} /></div>
        ) : history.length === 0 ? (
          <Empty title={rows.length ? t("v3.releases.none") : t("v2.promotions.empty")}>{!rows.length && t("v3.releases.emptySub")}</Empty>
        ) : (
          <table className="v3-table">
            <thead>
              <tr>
                <th />
                <th>{t("v2.promotions.colAgent")}</th>
                <th>{t("v2.promotions.colRoute")}</th>
                <th>{t("v2.promotions.colStatus")}</th>
                <th>{t("v2.promotions.requestedBy")}</th>
                <th>{t("v2.promotions.reviewedBy")}</th>
                <th className="num">{t("v2.promotions.colCreated")}</th>
              </tr>
            </thead>
            <tbody>
              {history.map((r) => {
                const s = promotionSignal(r.status);
                return (
                  <tr key={r.id} className="click" onClick={() => navigate(detailPath(r.id))}>
                    <td style={{ width: 30 }}><Lamp s={s} live={r.status === "executing"} /></td>
                    <td>
                      <div className="v3-name"><div><b>{agentLabel(r)}</b><small className="mono">{r.bundle?.digest.slice(0, 12) ?? r.bundle_id}</small></div></div>
                    </td>
                    <td><Route p={r} /></td>
                    <td><Chip s={s === "off" ? undefined : s}>{t(`v2.promotions.status.${r.status}`)}</Chip></td>
                    <td style={{ color: "var(--v3-text-2)" }}>{r.requested_by ?? "—"}</td>
                    <td style={{ color: "var(--v3-text-2)" }}>{r.reviewed_by ?? "—"}</td>
                    <td className="num" style={{ color: "var(--v3-text-3)" }}>{ago(r.created_at)}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </Panel>
    </div>
  );
}

/* ── one release ─────────────────────────────────────────────────────────── */

/** Requested → reviewed → executed → done, read from the request's status. */
function lifecycle(p: PromotionInfo, t: (k: string, o?: Record<string, unknown>) => string): TrackNode[] {
  const s = p.status;
  const reviewed = s !== "pending";
  const review: Signal = s === "pending" ? "wait" : s === "rejected" ? "act" : s === "cancelled" ? "off" : "ok";
  const started = ["executing", "succeeded", "failed", "rolled_back"].includes(s);
  const exec: Signal = !started ? "off" : s === "executing" ? "wait" : s === "failed" ? "act" : "ok";
  const done: Signal = s === "succeeded" ? "ok" : s === "rolled_back" ? "off" : "off";
  return [
    { key: "req", label: t("v3.releases.stepRequested"), detail: `${p.requested_by ?? "—"} · ${ago(p.created_at)}`, s: "ok" },
    {
      key: "rev",
      label: t("v3.releases.stepReviewed"),
      detail: reviewed ? `${t(`v2.promotions.status.${s === "executing" || s === "succeeded" || s === "failed" || s === "rolled_back" ? "approved" : s}`)}${p.reviewed_by ? ` · ${p.reviewed_by}` : ""}` : t("v3.releases.waitingReview"),
      s: review,
      here: s === "pending" || s === "rejected" || s === "cancelled",
    },
    {
      key: "exec",
      label: p.action === "rollback" ? t("v2.promotions.execTitleRollback") : t("v3.releases.stepExecuted"),
      detail: started ? t(`v2.promotions.status.${s}`) : s === "approved" ? t("v3.releases.notStarted") : "—",
      s: s === "approved" ? "info" : exec,
      here: s === "approved" || s === "executing" || s === "failed",
    },
    {
      key: "done",
      label: t("v3.releases.stepLive"),
      detail: s === "succeeded" ? p.target_workspace_id : s === "rolled_back" ? t("v2.promotions.status.rolled_back") : "—",
      s: done,
      here: s === "succeeded" || s === "rolled_back",
    },
  ];
}

function DiffBlock({ rows }: { rows: SpecDiffRow[] }) {
  const { t } = useTranslation();
  if (rows.length === 0) return <p style={{ margin: 0, color: "var(--v3-text-3)" }}>{t("v2.promotions.noDiff")}</p>;
  return (
    <div className="v3-rel-diff">
      <div className="head"><span>{t("v3.releases.field")}</span><span>{t("v3.releases.before")}</span><span>{t("v3.releases.after")}</span></div>
      {rows.map((row) => (
        <div key={row.field} className="row" data-kind={row.kind}>
          <div className="field">
            <span className="mono">{row.field}</span>
            <span style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
              <Chip s={row.kind === "removed" ? "act" : row.kind === "added" ? "ok" : "wait"}>{t(`v2.promotions.diffKind.${row.kind}`)}</Chip>
              {row.redacted && <Chip>{t("v2.promotions.redacted")}</Chip>}
            </span>
          </div>
          <pre className="v3-pre before">{row.before || "—"}</pre>
          <pre className="v3-pre after">{row.after || "—"}</pre>
        </div>
      ))}
    </div>
  );
}

/** What executing would create or change in the target — shown before Execute is offered. */
function PlanPanel({ p, onStarted }: { p: PromotionInfo; onStarted: () => void }) {
  const { t } = useTranslation();
  const { can } = useAuth();
  const toast = useToast();
  const plan = useLoad<PromotionPlan>(() => api.promotionPlan(p.id), `v3-plan:${p.id}`);
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const data = plan.data;
  const run = async () => {
    setBusy(true);
    try {
      await api.executePromotion(p.id);
      toast("ok", t("v2.promotions.executeStarted"));
      setConfirming(false);
      onStarted();
    } catch (err) {
      toast("act", errorMessage(err));
      setConfirming(false);
      plan.reload();
    } finally {
      setBusy(false);
    }
  };
  return (
    <Panel
      title={t("v2.promotions.planTitle")}
      signal={data ? (data.can_execute ? "info" : "wait") : undefined}
      end={
        can("promotion.approve") && (
          <Btn size="sm" kind="primary" disabled={!data?.can_execute} onClick={() => setConfirming(true)}>
            <Play size={13} /> {t("v2.promotions.execute")}
          </Btn>
        )
      }
    >
      <p style={{ margin: "0 0 12px", color: "var(--v3-text-3)", fontSize: 13 }}>{t("v2.promotions.planSub", { target: p.target_workspace_id })}</p>
      {plan.loading && !data && <Skeleton rows={3} />}
      {plan.error && <Notice s="act">{plan.error}</Notice>}
      {data && (
        <div style={{ display: "grid", gap: 10 }}>
          {data.blocked_by.length > 0 && (
            <Notice s="wait">
              {t("v2.promotions.blockedBy", { keys: data.blocked_by.join(", ") })}
              {data.gates.next_allowed_at && ` ${t("v2.promotions.nextAllowed", { time: new Date(data.gates.next_allowed_at).toLocaleString() })}`}
            </Notice>
          )}
          <div className="v3-rel-lines">
            {data.items.map((item) => {
              const inert = item.action === "none" || item.action === "skip";
              return (
                <div key={item.key} className="line">
                  <Lamp s={inert ? "off" : "info"} />
                  <Chip s={inert ? undefined : "info"}>{t(`v2.promotions.planAction.${item.action}`, { defaultValue: item.action })}</Chip>
                  <b>{t(`v2.promotions.planItem.${item.key}`, { defaultValue: item.key })}</b>
                  <span className="detail">{item.detail}</span>
                </div>
              );
            })}
          </div>
          {data.observe_seconds > 0 && <p style={{ margin: 0, color: "var(--v3-text-3)", fontSize: 13 }}>{t("v2.promotions.observeNote", { seconds: data.observe_seconds })}</p>}
        </div>
      )}
      {confirming && (
        <Confirm
          title={t("v2.promotions.executeTitle")}
          confirmLabel={t("v2.promotions.executeConfirm")}
          cancelLabel={t("v3.common.cancel")}
          busy={busy}
          onCancel={() => setConfirming(false)}
          onConfirm={() => void run()}
        >
          {t("v2.promotions.executeBody", { target: p.target_workspace_id })}
        </Confirm>
      )}
    </Panel>
  );
}

/** One node per stage, polled while the release runs; rollback when one was recorded. */
function ExecutionPanel({ p, onChanged }: { p: PromotionInfo; onChanged: () => void }) {
  const { t } = useTranslation();
  const { can } = useAuth();
  const toast = useToast();
  const [live, setLive] = useState<{ promotion: PromotionInfo; log: PromotionLogLine[] } | null>(null);
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const current = live?.promotion ?? p;
  const running = current.status === "executing";
  const changed = useRef(onChanged);
  changed.current = onChanged;
  const previous = useRef(p.status);

  const refresh = useCallback(async () => {
    try {
      const next = await api.promotionExecution(p.id);
      setLive(next);
      if (previous.current === "executing" && next.promotion.status !== "executing") {
        previous.current = next.promotion.status;
        changed.current();
      }
    } catch {
      /* keep the last good view; the next tick retries */
    }
  }, [p.id]);
  useEffect(() => {
    void refresh();
  }, [refresh, p.status]);
  useEffect(() => {
    if (!running) return undefined;
    const timer = window.setInterval(() => void refresh(), 3000);
    return () => window.clearInterval(timer);
  }, [running, refresh]);

  const stages = current.stages ?? [];
  const log = live?.log ?? [];
  const canRollback = can("promotion.approve") && (current.status === "succeeded" || current.status === "failed");
  const rollback = async () => {
    setBusy(true);
    try {
      await api.rollbackPromotion(p.id);
      toast("ok", t("v2.promotions.rollbackStarted"));
      setConfirming(false);
      previous.current = "executing";
      onChanged();
    } catch (err) {
      toast("act", errorMessage(err));
      setConfirming(false);
    } finally {
      setBusy(false);
    }
  };
  const nodes: TrackNode[] = stages.map((stage) => {
    const lines = log.filter((line) => line.stage === stage.name && line.level !== "debug");
    return {
      key: stage.name,
      label: t(`v2.promotions.stage.${stage.name}`, { defaultValue: stage.name }),
      detail: stage.detail || lines[lines.length - 1]?.msg || t(`v2.promotions.stageStatus.${stage.status}`),
      s: STAGE_SIGNAL[stage.status],
      here: stage.status === "running" || stage.status === "failed",
    };
  });
  return (
    <Panel
      title={current.action === "rollback" ? t("v2.promotions.execTitleRollback") : t("v2.promotions.execTitle")}
      signal={running ? "wait" : current.status === "failed" ? "act" : current.status === "succeeded" ? "ok" : undefined}
      end={canRollback && <Btn size="sm" onClick={() => setConfirming(true)}><RotateCcw size={13} /> {t("v2.promotions.rollback")}</Btn>}
    >
      {current.status === "failed" && current.error && (
        <div style={{ marginBottom: 14 }}>
          <Notice s="act">
            {current.failed_stage ? `${t("v2.promotions.failedAt", { stage: t(`v2.promotions.stage.${current.failed_stage}`) })} — ` : ""}
            {current.error}
          </Notice>
        </div>
      )}
      {nodes.length > 0 && <div className="v3-rel-stages"><Track nodes={nodes} /></div>}
      {canRollback && !current.previous_bundle_id && <p style={{ margin: "12px 0 0", color: "var(--v3-text-3)", fontSize: 13 }}>{t("v2.promotions.noPrevious")}</p>}
      {confirming && (
        <Confirm
          title={t("v2.promotions.rollbackTitle")}
          confirmLabel={t("v2.promotions.rollbackConfirm")}
          cancelLabel={t("v3.common.cancel")}
          danger
          busy={busy}
          onCancel={() => setConfirming(false)}
          onConfirm={() => void rollback()}
        >
          {t("v2.promotions.rollbackBody", { target: p.target_workspace_id })}
        </Confirm>
      )}
    </Panel>
  );
}

function ReviewDialog({ p, decision, onClose, onDone }: { p: PromotionInfo; decision: "approve" | "reject"; onClose: () => void; onDone: () => void }) {
  const { t } = useTranslation();
  const toast = useToast();
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const failures = p.gates.blocking_failures ?? [];
  const submit = async () => {
    setBusy(true);
    try {
      await api.reviewPromotion(p.id, { decision, note: note.trim() || undefined });
      toast("ok", t(`v2.promotions.${decision}d`));
      onDone();
      onClose();
    } catch (err) {
      toast("act", errorMessage(err));
    } finally {
      setBusy(false);
    }
  };
  return (
    <Dialog
      title={t(`v2.promotions.${decision}Title`)}
      onClose={() => !busy && onClose()}
      foot={
        <>
          <Btn kind="ghost" onClick={onClose} disabled={busy}>{t("v3.common.cancel")}</Btn>
          <Btn kind={decision === "reject" ? "danger" : "primary"} disabled={busy} onClick={() => void submit()}>
            {t(`v2.promotions.${decision}Confirm`)}
          </Btn>
        </>
      }
    >
      <div style={{ display: "grid", gap: 12 }}>
        {decision === "approve" && failures.length > 0 && <Notice s="wait">{t("v2.promotions.gatesFailing", { keys: failures.join(", ") })}</Notice>}
        <label className="v3-field">
          <span>{t("v2.promotions.reviewNote")}</span>
          <textarea className="v3-input" rows={4} maxLength={4000} value={note} onChange={(e) => setNote(e.target.value)} />
          <small className="v3-hint">{t("v2.promotions.reviewNoteHint")}</small>
        </label>
      </div>
    </Dialog>
  );
}

function ReleaseDetail({ id }: { id: string }) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const { can } = useAuth();
  const [tick, setTick] = useState(0);
  const detail = useLoad(() => api.getPromotion(id), `v3-release:${id}:${tick}`);
  const [review, setReview] = useState<"approve" | "reject" | null>(null);
  const p = detail.data;
  const bump = () => setTick((n) => n + 1);

  if (detail.loading && !p) return <Skeleton rows={6} />;
  if (!p) return <Notice s="act">{detail.error ?? t("v2.promotions.gone")}</Notice>;

  const s = promotionSignal(p.status);
  const pending = p.status === "pending";
  const bundle = p.bundle;
  const checks = p.gates.checks ?? [];
  const evalRun = (bundle?.evaluation as { run_id?: string } | undefined)?.run_id;

  return (
    <div className="v3-reveal" style={{ display: "grid", gap: 16 }}>
      <div>
        <button type="button" className="v3-btn ghost sm" onClick={() => navigate("/v3/releases")}>
          <ArrowLeft size={14} /> {t("v3.releases.title")}
        </button>
      </div>
      <PageHead
        eyebrow={`${p.source_workspace_id ?? "—"} → ${p.target_workspace_id}`}
        title={
          <span style={{ display: "inline-flex", alignItems: "center", gap: 14 }}>
            <Lamp s={s} live={p.status === "executing" || pending} />
            {t("v2.promotions.detailTitle", { agent: agentLabel(p) })}
          </span>
        }
        sub={p.change_note || undefined}
        end={
          pending && can("promotion.approve") ? (
            <>
              <Btn onClick={() => setReview("reject")}><X size={14} /> {t("v2.promotions.reject")}</Btn>
              <Btn kind="primary" onClick={() => setReview("approve")}><Check size={14} /> {t("v2.promotions.approve")}</Btn>
            </>
          ) : (
            <Chip s={s === "off" ? undefined : s}>{t(`v2.promotions.status.${p.status}`)}</Chip>
          )
        }
      />

      <Panel title={t("v3.releases.lifecycle")} signal={s === "off" ? undefined : s}>
        <Track nodes={lifecycle(p, t)} />
      </Panel>

      <div className="v3-grid c2" style={{ alignItems: "start" }}>
        <Panel title={t("v2.promotions.request")}>
          <dl className="v3-kv">
            <dt>{t("v2.promotions.requestedBy")}</dt><dd>{p.requested_by ?? "—"} · {ago(p.created_at)}</dd>
            <dt>{t("v2.promotions.reviewedBy")}</dt><dd>{p.reviewed_by ? `${p.reviewed_by} · ${ago(p.reviewed_at)}` : "—"}</dd>
            <dt>{t("v2.promotions.changeNote")}</dt><dd style={{ whiteSpace: "pre-wrap" }}>{p.change_note || "—"}</dd>
            <dt>{t("v2.promotions.rollbackNote")}</dt><dd style={{ whiteSpace: "pre-wrap" }}>{p.rollback_note || "—"}</dd>
            {p.review_note && <><dt>{t("v2.promotions.reviewNote")}</dt><dd style={{ whiteSpace: "pre-wrap" }}>{p.review_note}</dd></>}
          </dl>
        </Panel>
        <Panel title={t("v2.promotions.bundle")}>
          <p style={{ margin: "0 0 12px", color: "var(--v3-text-3)", fontSize: 13 }}>{t("v2.promotions.bundleSub")}</p>
          <dl className="v3-kv">
            <dt>{t("v2.promotions.digest")}</dt><dd className="mono">{bundle?.digest ?? "—"}</dd>
            <dt>{t("v2.promotions.publish")}</dt><dd className="mono">{bundle?.snapshot_seq ?? "—"}</dd>
            <dt>{t("v2.promotions.method")}</dt><dd>{bundle?.method ? <Chip>{bundle.method}</Chip> : "—"}</dd>
            <dt>{t("v2.promotions.evaluation")}</dt><dd className="mono">{evalRun ?? t("v2.promotions.noEval")}</dd>
          </dl>
          {bundle?.agent_id && (
            <div style={{ marginTop: 14 }}>
              <Link to={`/v3/gate?agent=${encodeURIComponent(bundle.agent_id)}`} className="v3-btn ghost sm"><Scale size={13} /> {t("v3.releases.openGate")}</Link>
            </div>
          )}
        </Panel>
      </div>

      <Panel title={t("v2.promotions.gates")} signal={checks.length ? (checks.every((g) => g.ok) ? "ok" : "wait") : undefined}>
        <p style={{ margin: "0 0 12px", color: "var(--v3-text-3)", fontSize: 13 }}>{t("v2.promotions.gatesSub")}</p>
        {checks.length === 0 ? (
          <p style={{ margin: 0, color: "var(--v3-text-3)" }}>—</p>
        ) : (
          <div className="v3-rel-lines">
            {checks.map((g) => (
              <div key={g.key} className="line">
                <Lamp s={g.ok ? "ok" : "wait"} />
                <Chip s={g.ok ? "ok" : "wait"}>{g.ok ? t("v2.promotions.gateOk") : t("v2.promotions.gateFail")}</Chip>
                <b>{t(`v2.promotions.gate.${g.key}`, { defaultValue: g.key })}</b>
                <span className="detail">{g.detail}</span>
              </div>
            ))}
          </div>
        )}
      </Panel>

      {(p.status === "approved" || (p.status === "failed" && p.action !== "rollback")) && <PlanPanel p={p} onStarted={bump} />}
      {(p.stages ?? []).length > 0 && <ExecutionPanel p={p} onChanged={bump} />}

      <Panel title={t("v2.promotions.diff")}>
        <p style={{ margin: "0 0 12px", color: "var(--v3-text-3)", fontSize: 13 }}>{t("v2.promotions.diffSub")}</p>
        {p.target_visible === false ? (
          <Notice>{t("v2.promotions.targetHidden", { target: p.target_workspace_id })}</Notice>
        ) : (
          <DiffBlock rows={p.diff ?? []} />
        )}
      </Panel>

      {review && <ReviewDialog p={p} decision={review} onClose={() => setReview(null)} onDone={bump} />}
    </div>
  );
}

export function V3Releases() {
  const [params] = useSearchParams();
  const id = params.get("id");
  return id ? <ReleaseDetail key={id} id={id} /> : <ReleaseDesk />;
}
