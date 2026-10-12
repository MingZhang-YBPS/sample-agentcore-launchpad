import { Play, RotateCcw, ShieldCheck, ShieldX } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link, useSearchParams } from "react-router-dom";

import { useAuth } from "../../auth/auth-context";
import { api, dlcApi, errorMessage } from "../../lib/api";
import type { CostEstimate, GateReport, GateRow, ReleaseState, Scorecard } from "../../lib/dlc";
import { useWorkspace } from "../../workspace/workspace-context";
import { gateSignal, releaseSignal } from "../signals";
import { Btn, Chip, Dialog, Empty, Lamp, Notice, PageHead, Panel, type Signal, Skeleton, Stat } from "../ui";
import { useLoad, useToast } from "../hooks";
import { ago, pct } from "../format";

const ROW_SIGNAL: Record<GateRow["verdict"], Signal> = {
  PASS: "ok",
  BLOCKED: "act",
  INVALID: "wait",
  WAIVED: "info",
  OBSERVED: "off",
};

/**
 * The four gates, in the order they are applied, as one pipeline the verdict
 * flows through. A gate that stopped the release lights up and everything after it
 * stays dark — so "where did it stop" is answered before anything is read.
 */
function Pipeline({ report }: { report: GateReport }) {
  const { t } = useTranslation();
  const stopIndex =
    report.redline_violations.length > 0 ? 0 : report.invalid.length > 0 || report.provenance.issues.length > 0 ? 1
      : report.gate_failures.length > 0 ? 2 : -1;
  const steps = [
    { key: "redline", count: report.redline_violations.length, s: "act" as Signal },
    { key: "denominator", count: report.invalid.length + report.provenance.issues.length, s: "wait" as Signal },
    { key: "gate", count: report.gate_failures.length, s: "act" as Signal },
    { key: "observe", count: report.criteria.filter((c) => c.verdict === "OBSERVED").length, s: "off" as Signal },
  ];
  return (
    <div className="v3-pipe">
      {steps.map((step, i) => {
        const stopped = i === stopIndex;
        const passed = stopIndex === -1 || i < stopIndex;
        const s: Signal = stopped ? step.s : passed ? (step.key === "observe" ? "info" : "ok") : "off";
        return (
          <div key={step.key} className="v3-pipe-step" data-s={s}>
            <div className="v3-pipe-node"><Lamp s={s} live={stopped} /></div>
            <div className="v3-stat">
              <div className="label">{`0${i + 1}`} · {t(`v3.gate.step.${step.key}`)}</div>
              <div className="mono" style={{ marginTop: 4 }}>
                {stopped ? t(`v3.gate.stopped.${step.key}`, { count: step.count })
                  : passed ? (step.key === "observe" ? t("v3.gate.recorded", { count: step.count }) : t("v3.gate.passed"))
                    : t("v3.gate.notReached")}
              </div>
            </div>
          </div>
        );
      })}
    </div>
  );
}

/** A rate as a point inside its interval, against the threshold tick. */
function Interval({ row }: { row: GateRow }) {
  const { t } = useTranslation();
  const rate = typeof row.measured === "number" ? row.measured : null;
  if (rate === null) return <span style={{ color: "var(--v3-text-3)" }}>—</span>;
  const x = (v: number) => `${Math.max(0, Math.min(100, v * 100))}%`;
  const lo = row.wilson_low ?? null;
  const hi = row.wilson_high ?? null;
  return (
    <div className="v3-interval" data-undecided={row.threshold_inside_ci ? "true" : undefined}>
      <div className="track">
        {lo !== null && hi !== null && <div className="ci" style={{ left: x(lo), width: `${Math.max(1, (hi - lo) * 100)}%` }} />}
        {typeof row.threshold === "number" && <div className="thr" style={{ left: x(row.threshold) }} />}
        <div className="dot" data-s={ROW_SIGNAL[row.verdict]} style={{ left: x(rate) }} />
      </div>
      <span className="mono">{pct(rate, 1)}</span>
      {row.threshold_inside_ci && <Chip s="wait">{t("v3.gate.cannotDecide")}</Chip>}
    </div>
  );
}

function Dimensions({ card }: { card: Scorecard }) {
  const { t } = useTranslation();
  return (
    <div className="v3-grid" style={{ gridTemplateColumns: "repeat(5, minmax(0, 1fr))" }}>
      {card.dimensions.map((d) => {
        const metric = d.current === null && d.metrics?.length ? d.metrics[0] : null;
        const short = d.current !== null && d.standard !== null && d.current < d.standard;
        return (
          <Panel key={d.dimension} signal={d.not_applicable ? undefined : short || d.redline_violations ? "act" : "ok"}>
            <Stat
              label={t(`v2.dlc.dimension.${d.dimension}`)}
              value={d.not_applicable ? "—" : metric ? Math.round(metric.value ?? 0).toLocaleString() : pct(d.current)}
              unit={metric ? (metric.metric?.includes("ms") ? "ms" : undefined) : undefined}
              signal={d.not_applicable ? undefined : short || d.redline_violations ? "act" : "ok"}
              foot={d.not_applicable ? t("v3.gate.na") : metric ? `${metric.op} ${metric.bound}`
                : t("v3.gate.standard", { v: d.standard === null ? "—" : pct(d.standard) })}
            />
          </Panel>
        );
      })}
    </div>
  );
}

export function V3Gate() {
  const { t } = useTranslation();
  const toast = useToast();
  const { can } = useAuth();
  const { current } = useWorkspace();
  const [params, setParams] = useSearchParams();
  const agents = useLoad(() => api.listAgents(), `v3-gate-agents:${current?.id ?? ""}`);
  const active = useMemo(() => (agents.data?.agents ?? []).filter((a) => a.status === "active"), [agents.data]);
  const agentId = params.get("agent") ?? "";
  useEffect(() => {
    if (!agentId && active.length) setParams({ agent: active[0].id }, { replace: true });
  }, [agentId, active, setParams]);

  const [nonce, setNonce] = useState(0);
  const [busy, setBusy] = useState(false);
  // sign / block / rollback each go through a dialog with a note, as in V2: a
  // block or rollback must say why, and the reason is recorded against the release
  const [deciding, setDeciding] = useState<"sign" | "block" | "rollback" | null>(null);
  const [note, setNote] = useState("");
  // what running the gate would cost; over the workspace limit it cannot run, and
  // the server decides whether the cost needs an explicit confirm
  const estimate = useLoad<CostEstimate | null>(
    () => (agentId ? dlcApi.estimate({ agent_id: agentId, repeats: 1 }) : Promise.resolve(null)), `v3-gate-est:${agentId}`);
  const release = useLoad<ReleaseState | null>(
    () => (agentId ? dlcApi.release(agentId) : Promise.resolve(null)), `v3-gate-rel:${agentId}:${nonce}`);
  const card = useLoad<Scorecard | null>(
    () => (agentId ? dlcApi.scorecard(agentId) : Promise.resolve(null)), `v3-gate-card:${agentId}:${nonce}`);
  const pending = release.data?.pending ?? null;
  const evaluated = Boolean(pending && pending.run_ids.length > 0);
  const gate = useLoad(
    () => (evaluated ? dlcApi.gate(agentId) : Promise.resolve(null)), `v3-gate-report:${agentId}:${evaluated}:${nonce}`);
  const report = gate.data?.report ?? null;
  const evaluating = gate.data?.status === "evaluating";

  const act = (fn: () => Promise<unknown>, ok: string) => {
    setBusy(true);
    fn()
      .then(() => {
        toast("ok", t(ok));
        setNonce((n) => n + 1);
      })
      .catch((err: unknown) => toast("act", errorMessage(err)))
      .finally(() => setBusy(false));
  };

  const agent = active.find((a) => a.id === agentId);
  const state = release.data?.state;
  const verdictSignal = gateSignal(report?.verdict);

  return (
    <div className="v3-reveal" style={{ display: "grid", gap: 16 }}>
      <PageHead
        eyebrow={t("v3.gate.eyebrow")}
        title={report ? t(`v3.gate.headline.${report.verdict}`) : pending ? t("v3.gate.headline.waiting") : t("v3.gate.headline.none")}
        sub={t("v3.gate.sub")}
        end={
          <select className="v3-select" style={{ width: 260 }} value={agentId}
            onChange={(e) => setParams({ agent: e.target.value })} aria-label={t("v3.chat.agent")}>
            {active.map((a) => <option key={a.id} value={a.id}>{a.display_name || a.name}</option>)}
          </select>
        }
      />

      {agents.loading && !agents.data ? <Skeleton rows={5} /> : !agent ? (
        <Panel><Empty title={t("v3.gate.noAgents")} /></Panel>
      ) : (
        <>
          <div className="v3-grid c4">
            <Panel signal="ok"><Stat label={t("v3.gate.live")} value={`v${state?.live_version ?? agent.version ?? "—"}`}
              foot={state?.endpoint_mode === "live" ? t("v3.gate.namedEndpoint") : t("v3.gate.defaultEndpoint")} /></Panel>
            <Panel signal={pending ? "wait" : undefined}><Stat label={t("v3.gate.candidate")}
              value={pending ? `v${pending.candidate_version ?? "—"}` : "—"} signal={pending ? "wait" : undefined}
              foot={pending ? t(`v2.dlc.release.decision.${pending.decision}`) : t("v3.gate.noCandidate")} /></Panel>
            <Panel signal={verdictSignal === "off" ? undefined : verdictSignal}><Stat label={t("v3.gate.verdict")}
              value={report?.verdict ?? "—"} signal={verdictSignal === "off" ? undefined : verdictSignal}
              foot={report ? ago(report.decided_at) : evaluating ? t("v3.gate.evaluating") : "—"} /></Panel>
            <Panel><Stat label={t("v3.gate.standardLabel")}
              value={release.data?.criteria_set ? `v${release.data.criteria_set.version}` : "—"}
              foot={release.data?.criteria_set?.signed_by ? t("v3.gate.signedBy", { who: release.data.criteria_set.signed_by })
                : t("v3.gate.unsigned")} signal={release.data?.criteria_set?.signed_by ? undefined : "wait"} /></Panel>
          </div>

          {state?.endpoint_mode !== "live" && (
            <Notice s="wait">
              {t("v3.gate.notGated")}{" "}
              <Link to={`/v2/eval/standards?agent=${agent.id}&view=release`} style={{ textDecoration: "underline" }}>
                {t("v3.gate.setUp")}
              </Link>
            </Notice>
          )}

          {report && (
            <Panel title={t("v3.gate.pipeline")} signal={verdictSignal === "off" ? undefined : verdictSignal}>
              <Pipeline report={report} />
              {report.provenance.issues.length > 0 && (
                <div style={{ marginTop: 14 }}>
                  <Notice s="wait">{report.provenance.issues.join(" · ")}</Notice>
                </div>
              )}
            </Panel>
          )}

          {pending && !evaluated && (
            <Panel title={t("v3.gate.run")} signal="wait">
              <div style={{ display: "flex", alignItems: "center", gap: 16, flexWrap: "wrap" }}>
                <span style={{ color: "var(--v3-text-2)" }}>{t("v3.gate.runHint")}</span>
                <span style={{ display: "inline-flex", gap: 8, alignItems: "center", marginLeft: "auto" }} title={t("v2.dlc.cost.basisHint")}>
                  <span style={{ color: "var(--v3-text-3)" }}>{t("v2.dlc.cost.estimate")}</span>
                  <b className="mono">
                    {estimate.data?.total_usd == null ? t("v2.dlc.cost.unknown") : `$${estimate.data.total_usd.toFixed(2)}`}
                  </b>
                  {estimate.data && (
                    <span style={{ color: "var(--v3-text-3)", fontSize: 12 }}>
                      {t("v2.dlc.cost.sessions", { sessions: estimate.data.sessions, minutes: estimate.data.duration_minutes ?? 0 })}
                    </span>
                  )}
                  {estimate.data?.unpriced && <Chip s="wait">{t("v2.dlc.cost.unpriced")}</Chip>}
                  {estimate.data?.over_limit && <Chip s="act">{t("v2.dlc.cost.overLimit")}</Chip>}
                </span>
                <Btn kind="primary" disabled={busy || !can("eval.run") || estimate.loading || Boolean(estimate.data?.over_limit)}
                  onClick={() =>
                    act(
                      () => dlcApi.evaluateRelease(agent.id, { repeats: 1, confirm_cost: Boolean(estimate.data?.confirm_required) }),
                      "v3.gate.started",
                    )
                  }>
                  <Play size={14} /> {t("v3.gate.runBtn")}
                </Btn>
              </div>
            </Panel>
          )}
          {evaluating && <Notice s="wait">{t("v3.gate.evaluating")}</Notice>}

          {report && (
            <Panel title={t("v3.gate.criteria")} flush
              end={
                can("release.sign") ? (
                  <span style={{ display: "flex", gap: 8 }}>
                    <Btn kind="primary" size="sm" disabled={busy || report.verdict !== "PASS"}
                      title={report.verdict !== "PASS" ? t("v2.dlc.release.onlyPassSigns") : undefined}
                      onClick={() => { setNote(""); setDeciding("sign"); }}>
                      <ShieldCheck size={13} /> {t("v3.gate.sign")}
                    </Btn>
                    <Btn kind="danger" size="sm" disabled={busy}
                      onClick={() => { setNote(""); setDeciding("block"); }}>
                      <ShieldX size={13} /> {t("v3.gate.block")}
                    </Btn>
                  </span>
                ) : undefined
              }
            >
              <table className="v3-table">
                <thead>
                  <tr>
                    <th />
                    <th>{t("v3.gate.criterion")}</th>
                    <th>{t("v3.gate.tier")}</th>
                    <th style={{ width: "34%" }}>{t("v3.gate.measured")}</th>
                    <th className="num">n</th>
                  </tr>
                </thead>
                <tbody>
                  {report.criteria.map((row) => (
                    <tr key={row.key}>
                      <td style={{ width: 30 }}><Lamp s={ROW_SIGNAL[row.verdict]} /></td>
                      <td>
                        <div style={{ fontWeight: 600 }}><span className="mono" style={{ color: "var(--v3-text-3)" }}>{row.key}</span> {row.text}</div>
                        {row.reason && <div style={{ color: "var(--v3-text-3)", fontSize: 12 }}>{row.reason}</div>}
                      </td>
                      <td>
                        <Chip s={row.tier === "redline" ? "act" : row.tier === "gate" ? "wait" : undefined}>
                          {t(`v2.dlc.tier.${row.effective_tier ?? row.tier}`)}
                        </Chip>
                      </td>
                      <td><Interval row={row} /></td>
                      <td className="num">{row.n ?? "—"}<span style={{ color: "var(--v3-text-3)" }}>/{row.expected ?? "—"}</span></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </Panel>
          )}

          {card.data && (
            <>
              <div className="v3-rail-label" style={{ padding: "6px 0 0" }}>{t("v3.gate.dimensions")}</div>
              <Dimensions card={card.data} />
            </>
          )}

          {release.data && release.data.records.length > 0 && (
            <Panel title={t("v3.gate.history")} flush
              end={can("release.sign") && state?.live_version ? (
                <Btn size="sm" kind="ghost" disabled={busy}
                  onClick={() => { setNote(""); setDeciding("rollback"); }}>
                  <RotateCcw size={13} /> {t("v3.gate.rollback")}
                </Btn>
              ) : undefined}>
              <table className="v3-table">
                <tbody>
                  {release.data.records.slice(0, 8).map((r) => (
                    <tr key={r.id}>
                      <td style={{ width: 30 }}><Lamp s={releaseSignal(r.decision)} /></td>
                      <td className="mono">v{r.candidate_version ?? "—"}</td>
                      <td>{t(`v2.dlc.release.decision.${r.decision}`)}</td>
                      <td className="mono" style={{ color: "var(--v3-text-2)" }}>{r.decided_by ?? r.requested_by}</td>
                      <td className="num" style={{ color: "var(--v3-text-3)" }}>{ago(r.decided_at ?? r.created_at)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </Panel>
          )}

          <div style={{ display: "flex", gap: 10 }}>
            <Link to={`/v3/standards?agent=${agent.id}&view=criteria`} className="v3-btn ghost">
              {t("v3.gate.editCriteria")}
            </Link>
            <Link to={`/v3/standards?agent=${agent.id}&view=calibration`} className="v3-btn ghost">
              {t("v3.gate.calibrate")}
            </Link>
          </div>

          {deciding && (
            <Dialog
              title={t(deciding === "sign" ? "v3.gate.sign" : deciding === "block" ? "v3.gate.block" : "v3.gate.rollback")}
              onClose={() => setDeciding(null)}
              foot={
                <>
                  <Btn kind="ghost" onClick={() => setDeciding(null)}>{t("v3.common.cancel")}</Btn>
                  <Btn
                    kind={deciding === "sign" ? "primary" : "danger"}
                    disabled={busy || (deciding !== "sign" && !note.trim())}
                    onClick={() => {
                      const action = deciding;
                      setDeciding(null);
                      if (action === "sign") act(() => dlcApi.signRelease(agent.id, note), "v3.gate.signed");
                      else if (action === "block") act(() => dlcApi.blockRelease(agent.id, note), "v3.gate.blocked");
                      else act(() => dlcApi.rollback(agent.id, note), "v3.gate.rolledBack");
                    }}
                  >
                    {t(deciding === "sign" ? "v3.gate.sign" : deciding === "block" ? "v3.gate.block" : "v3.gate.rollback")}
                  </Btn>
                </>
              }
            >
              <p style={{ marginTop: 0 }}>{t(`v2.dlc.release.${deciding}Hint`)}</p>
              {deciding === "sign" && report && (
                <dl className="v3-kv" style={{ marginBottom: 14 }}>
                  <dt>{t("v2.dlc.release.verdict")}</dt><dd><Chip s={gateSignal(report.verdict)}>{report.verdict}</Chip></dd>
                  <dt>{t("v2.dlc.release.waived")}</dt><dd>{report.waived.join(", ") || "—"}</dd>
                  <dt>{t("v2.dlc.release.liveAfter")}</dt><dd className="mono">{pending?.candidate_version ? `v${pending.candidate_version}` : "—"}</dd>
                  <dt>{t("v2.dlc.release.rollbackTo")}</dt><dd className="mono">{pending?.previous_live_version ? `v${pending.previous_live_version}` : "—"}</dd>
                </dl>
              )}
              <label className="v3-field">
                <span>{t("v2.dlc.release.note")}{deciding !== "sign" ? " *" : ""}</span>
                <textarea className="v3-input" rows={3} value={note} onChange={(e) => setNote(e.target.value)} />
              </label>
            </Dialog>
          )}
        </>
      )}
    </div>
  );
}
