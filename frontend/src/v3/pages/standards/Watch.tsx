/**
 * Drift watch, the fix ladder and the audit trail. A model can change under an
 * agent without a line of code changing, so the standard is re-run on a schedule
 * and a dip is split into three questions: is it unusual (baseline band, and the
 * page says when the baseline is too short), is it the agent or the judge (drift
 * triage), did my fix work (the ladder, with which layer changed). "No alert" is
 * rendered explicitly so quiet days cannot be a dead monitor.
 */
import { Play } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { useNavigate } from "react-router-dom";

import { useAuth } from "../../../auth/auth-context";
import { dlcApi, errorMessage } from "../../../lib/api";
import { DLC_DIMENSIONS, type RunComparison, type WatchView } from "../../../lib/dlc";
import { useLoad, useToast } from "../../hooks";
import { Btn, Chip, Empty, Filters, Lamp, Notice, Panel, Skeleton, Stat } from "../../ui";
import { BandChart, LadderChart } from "./charts";
import { rate, stamp } from "./common";

function WatchConfig({ view, agentId, onReload }: { view: WatchView; agentId: string; onReload: () => void }) {
  const { t } = useTranslation();
  const { can } = useAuth();
  const toast = useToast();
  const config = view.config;
  const [form, setForm] = useState({
    every: config?.every ?? "daily",
    at_hour: config?.at_hour ?? 3,
    repeats: config?.repeats ?? 1,
    max_cost_usd: config?.max_cost_usd ?? null,
    enabled: config?.enabled ?? true,
  });
  const [busy, setBusy] = useState(false);
  const mayEdit = can("eval.run");
  const act = (fn: () => Promise<unknown>, okKey: string) => {
    setBusy(true);
    fn()
      .then(() => { toast("ok", t(okKey)); onReload(); })
      .catch((err: unknown) => toast("act", errorMessage(err)))
      .finally(() => setBusy(false));
  };

  return (
    <Panel title={t("v2.dlc.watch.config")} signal={config?.enabled ? "ok" : "wait"}
      end={config ? <Chip s={config.enabled ? "ok" : undefined}>{t(config.enabled ? "v2.dlc.watch.on" : "v2.dlc.watch.off")}</Chip> : <Chip s="wait">{t("v2.dlc.watch.notConfigured")}</Chip>}>
      <p className="v3-std-muted" style={{ margin: "0 0 12px" }}>{t("v2.dlc.watch.configSub")}</p>
      <div className="v3-grid c4" style={{ alignItems: "end" }}>
        <label className="v3-field">
          <span>{t("v2.dlc.watch.every")}</span>
          <select className="v3-select" value={form.every} disabled={!mayEdit} onChange={(e) => setForm({ ...form, every: e.target.value as "daily" | "weekly" })}>
            {["daily", "weekly"].map((v) => <option key={v} value={v}>{t(`v2.dlc.watch.${v}`)}</option>)}
          </select>
        </label>
        <label className="v3-field">
          <span>{t("v2.dlc.watch.atHour")}</span>
          <input className="v3-input" type="number" min={0} max={23} disabled={!mayEdit} value={form.at_hour} onChange={(e) => setForm({ ...form, at_hour: Number(e.target.value) })} />
        </label>
        <label className="v3-field">
          <span>{t("v2.dlc.watch.repeats")}</span>
          <input className="v3-input" type="number" min={1} max={10} disabled={!mayEdit} value={form.repeats} onChange={(e) => setForm({ ...form, repeats: Number(e.target.value) })} />
        </label>
        <label className="v3-field">
          <span>{t("v2.dlc.watch.ceiling")}</span>
          <input className="v3-input" type="number" min={0} step={0.5} disabled={!mayEdit} value={form.max_cost_usd ?? ""}
            onChange={(e) => setForm({ ...form, max_cost_usd: e.target.value === "" ? null : Number(e.target.value) })} />
        </label>
      </div>
      <label style={{ display: "inline-flex", gap: 8, alignItems: "center", marginTop: 12 }}>
        <input type="checkbox" checked={form.enabled} disabled={!mayEdit} onChange={(e) => setForm({ ...form, enabled: e.target.checked })} />
        {t("v2.dlc.watch.enabled")}
      </label>
      {config && (
        <p className="v3-std-muted" style={{ margin: "10px 0 0" }}>
          {t("v2.dlc.watch.nextDue", { at: stamp(config.next_due_at) })}
          {config.last_status ? ` · ${t(`v2.dlc.watch.lastStatus.${config.last_status}`, { defaultValue: config.last_status })}` : ""}
          {config.last_detail ? ` — ${config.last_detail}` : ""}
        </p>
      )}
      {mayEdit && (
        <div style={{ display: "flex", gap: 8, marginTop: 14 }}>
          <Btn kind="primary" disabled={busy}
            onClick={() => act(() => dlcApi.putWatch(agentId, {
              every: form.every as "daily" | "weekly", at_hour: form.at_hour, repeats: form.repeats, max_cost_usd: form.max_cost_usd, enabled: form.enabled,
            }), "v2.dlc.watch.saved")}>
            {t("v3.standards.save")}
          </Btn>
          <Btn disabled={busy || !config} onClick={() => act(() => dlcApi.runWatch(agentId), "v2.dlc.watch.started")}><Play size={13} /> {t("v2.dlc.watch.runNow")}</Btn>
        </div>
      )}
    </Panel>
  );
}

export function Watch({ agentId }: { agentId: string }) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const [nonce, setNonce] = useState(0);
  const [dimension, setDimension] = useState<string>("quality");
  const view = useLoad<WatchView>(() => dlcApi.watch(agentId), `v3-std-watch:${agentId}:${nonce}`);
  if (view.loading && !view.data) return <Skeleton rows={6} />;
  if (!view.data) return <Notice s="act">{view.error ?? t("v2.dlc.watch.notFound")}</Notice>;
  const data = view.data;
  const alerts = data.alerts;
  const firing = [...alerts.count, ...alerts.score, ...alerts.distribution];
  const notReady = Object.entries(alerts.baseline_ready).filter(([, ready]) => !ready).map(([d]) => d);
  const drift = data.drift;
  const dims = DLC_DIMENSIONS.filter((d) => (data.series[d] ?? []).length > 0 || d === dimension);

  return (
    <div style={{ display: "grid", gap: 16 }}>
      <Panel title={t("v2.dlc.watch.alerts")} signal={alerts.quiet ? "ok" : "act"} end={<span className="mono">{alerts.firing}</span>}>
        {alerts.quiet ? (
          <Notice s="ok">{t("v2.dlc.watch.quiet", { n: alerts.runs_considered, at: stamp(alerts.checked_at) })}</Notice>
        ) : (
          <ul className="v3-std-att">
            {firing.map((a, i) => (
              <li key={`${a.family}:${a.criterion_key ?? a.dimension ?? i}`}>
                <Chip s={a.severity === "page" ? "act" : "wait"}>{t(`v2.dlc.watch.family.${a.family}`)}</Chip>
                <Chip>{t(`v2.dlc.watch.severity.${a.severity}`)}</Chip>
                <span>{a.detail}</span>
              </li>
            ))}
          </ul>
        )}
        {notReady.length > 0 && (
          <div style={{ marginTop: 10 }}>
            <Notice s="wait">{t("v2.dlc.watch.baselineNotReady", { dims: notReady.map((d) => t(`v2.dlc.dimension.${d}`, { defaultValue: d })).join(", ") })}</Notice>
          </div>
        )}
      </Panel>

      <Panel title={t("v2.dlc.watch.series")}
        end={<Filters value={dimension} onChange={setDimension} options={dims.map((d) => ({ value: d, label: t(`v2.dlc.dimension.${d}`) }))} />}>
        <p className="v3-std-muted" style={{ margin: "0 0 10px" }}>{t("v2.dlc.watch.seriesSub")}</p>
        <BandChart points={data.series[dimension] ?? []} />
      </Panel>

      <Panel title={t("v2.dlc.watch.drift")} flush signal={drift.needs_recalibration.length > 0 || drift.versions_changed ? "wait" : undefined}>
        <div style={{ padding: "0 20px 14px", display: "grid", gap: 12 }}>
          <Notice>{t("v2.dlc.watch.driftHint")}</Notice>
          <div className="v3-grid c2">
            <Stat label={t("v2.dlc.watch.versionsInWindow")} value={<span className="mono" style={{ fontSize: 18 }}>{drift.agent_versions_in_window.join(", ") || "—"}</span>}
              signal={drift.versions_changed ? "wait" : undefined} />
            <Stat label={t("v2.dlc.watch.needRecalibration")} value={drift.needs_recalibration.length} signal={drift.needs_recalibration.length > 0 ? "wait" : undefined} />
          </div>
        </div>
        {drift.judges.length === 0 ? (
          <Empty title={t("v2.dlc.watch.noJudges")} />
        ) : (
          <table className="v3-table">
            <thead><tr><th>{t("v2.dlc.criteria.key")}</th><th>{t("v2.dlc.criteria.evaluatorId")}</th><th>{t("v2.dlc.watch.calibrated")}</th><th className="num">κ</th></tr></thead>
            <tbody>
              {drift.judges.map((row) => (
                <tr key={row.criterion_key}>
                  <td className="mono">{row.criterion_key}</td>
                  <td className="mono">{row.evaluator_id ?? "—"}</td>
                  <td>
                    <Chip s={row.calibrated ? "ok" : "wait"}>
                      {row.calibrated ? t("v2.dlc.watch.yes") : t(`v2.dlc.calibration.reason.${row.reason ?? "never_calibrated"}`, { defaultValue: row.reason ?? "" })}
                    </Chip>
                  </td>
                  <td className="num">{row.judge_human_kappa === null ? "—" : row.judge_human_kappa.toFixed(2)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Panel>

      <WatchConfig view={data} agentId={agentId} onReload={() => setNonce((n) => n + 1)} />

      <Panel title={t("v2.dlc.watch.runs")} flush>
        {data.runs.length === 0 ? (
          <Empty title={t("v2.dlc.watch.noRuns")} />
        ) : (
          <table className="v3-table">
            <thead>
              <tr>
                <th>{t("v2.dlc.watch.run")}</th>
                <th>{t("v2.dlc.watch.at")}</th>
                <th>{t("v2.dlc.golden.split")}</th>
                <th>{t("v2.dlc.release.version")}</th>
                <th>{t("v2.dlc.release.criteriaVersion")}</th>
              </tr>
            </thead>
            <tbody>
              {data.runs.map((row) => (
                <tr key={row.id} className="click" onClick={() => navigate(`/v2/eval/tasks?view=detail&id=${encodeURIComponent(row.id)}`)}>
                  <td className="mono">{row.id.slice(0, 8)}</td>
                  <td>{stamp(row.at)}</td>
                  <td>{row.split ?? "—"}</td>
                  <td className="mono">{row.agent_version ?? "—"}</td>
                  <td className="mono">v{row.criteria_set_version ?? "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Panel>
    </div>
  );
}

export function Compare({ agentId }: { agentId: string }) {
  const { t } = useTranslation();
  const ladder = useLoad<RunComparison>(() => dlcApi.ladder(agentId), `v3-std-ladder:${agentId}`);
  const data = ladder.data;
  if (ladder.loading && !data) return <Skeleton rows={6} />;
  if (!data) return <Notice s="act">{ladder.error ?? t("v2.dlc.compare.noRuns")}</Notice>;
  if (!data.comparable && !data.two_numbers) {
    return (
      <Panel title={t("v2.dlc.compare.title")} signal="wait">
        <Notice s="wait">{data.incomparable_reason ?? t("v2.dlc.compare.incomparable")}</Notice>
        {data.runs && <div style={{ marginTop: 14 }}><LadderChart rungs={data.runs} /></div>}
      </Panel>
    );
  }
  const two = data.two_numbers;
  return (
    <div style={{ display: "grid", gap: 16 }}>
      <Panel title={t("v2.dlc.compare.title")}>
        <p className="v3-std-muted" style={{ margin: "0 0 12px" }}>{t("v2.dlc.compare.sub")}</p>
        {data.warning && <div style={{ marginBottom: 12 }}><Notice s="wait">{data.warning}</Notice></div>}
        {!data.comparable && two && (
          <div style={{ display: "grid", gap: 12, marginBottom: 14 }}>
            <Notice s="wait">{data.incomparable_reason}</Notice>
            <div className="v3-grid c3">
              <Stat label={t("v2.dlc.compare.agentDelta")} value={two.agent_delta === null ? "—" : `${(two.agent_delta * 100).toFixed(1)}pp`}
                foot={t("v2.dlc.compare.agentDeltaSub")} signal={(two.agent_delta ?? 0) < 0 ? "act" : undefined} />
              <Stat label={t("v2.dlc.compare.standardDelta")} value={two.standard_delta === null ? "—" : `${(two.standard_delta * 100).toFixed(1)}pp`}
                foot={t("v2.dlc.compare.standardDeltaSub")} />
              <Stat label={t("v2.dlc.compare.added")} value={<span className="mono" style={{ fontSize: 16 }}>{two.added_criteria.join(", ") || "—"}</span>} />
            </div>
          </div>
        )}
        <LadderChart rungs={data.runs} />
      </Panel>

      <div className="v3-grid c3">
        <Panel signal={data.fixed.length ? "ok" : undefined}><Stat label={t("v2.dlc.compare.fixed")} value={data.fixed.length} /></Panel>
        <Panel signal={data.new_failures.length ? "act" : undefined}><Stat label={t("v2.dlc.compare.newFailures")} value={data.new_failures.length} /></Panel>
        <Panel signal={data.still_failing.length ? "wait" : undefined}><Stat label={t("v2.dlc.compare.stillFailing")} value={data.still_failing.length} /></Panel>
      </div>

      <Panel title={t("v2.dlc.compare.perCriterion")} flush>
        {data.criteria.length === 0 ? (
          <Empty title={t("v2.dlc.compare.noCriteria")} />
        ) : (
          <table className="v3-table">
            <thead><tr><th>{t("v2.dlc.criteria.key")}</th><th className="num">{t("v2.dlc.compare.before")}</th><th className="num">{t("v2.dlc.compare.after")}</th><th className="num">Δ</th><th className="num">p</th></tr></thead>
            <tbody>
              {data.criteria.map((row) => (
                <tr key={row.key}>
                  <td className="mono">{row.key}</td>
                  <td className="num">{rate(row.before.rate)} <span className="v3-std-muted">n={row.before.n}</span></td>
                  <td className="num">{rate(row.after.rate)} <span className="v3-std-muted">n={row.after.n}</span></td>
                  <td className="num">
                    {row.delta === undefined ? "—" : (
                      <span className={row.delta >= 0 ? "v3-std-up" : "v3-std-down"}>{row.delta >= 0 ? "+" : ""}{(row.delta * 100).toFixed(1)}pp</span>
                    )}
                  </td>
                  <td className="num">
                    {row.p_value === undefined || row.p_value === null ? "—" : <>{row.p_value.toFixed(3)} {!row.significant && <Chip>{t("v2.dlc.compare.noise")}</Chip>}</>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Panel>

      {data.new_failures.length > 0 && (
        <Panel title={t("v2.dlc.compare.newFailures")} signal="act" flush>
          <table className="v3-table">
            <tbody>
              {data.new_failures.slice(0, 20).map((m) => (
                <tr key={`${m.criterion_key}:${m.scenario_id}`}>
                  <td style={{ width: 30 }}><Lamp s="act" /></td>
                  <td className="mono">{m.criterion_key}</td>
                  <td className="mono">{m.scenario_id}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </Panel>
      )}
    </div>
  );
}

const FAMILIES = ["criteria.", "golden.", "calibration.", "release.", "waiver."] as const;

export function Audit({ agentId }: { agentId: string | null }) {
  const { t } = useTranslation();
  const [action, setAction] = useState<"" | (typeof FAMILIES)[number]>("");
  const trail = useLoad(() => dlcApi.audit({ action: action || undefined, limit: 200 }), `v3-std-audit:${action}`);
  const rows = (trail.data?.events ?? []).filter((e) => !agentId || e.target.includes(agentId) || !e.target);
  return (
    <div style={{ display: "grid", gap: 16 }}>
      <Filters value={action} onChange={setAction}
        options={[{ value: "" as const, label: t("v2.dlc.audit.allActions") }, ...FAMILIES.map((a) => ({ value: a, label: t(`v2.dlc.audit.family.${a.replace(".", "")}`) }))]} />
      <Panel title={t("v2.dlc.audit.title")} flush end={<span className="mono">{rows.length}</span>}>
        <p className="v3-std-muted" style={{ margin: "0 20px 10px" }}>{t("v2.dlc.audit.sub")}</p>
        {trail.loading && !trail.data ? (
          <div style={{ padding: 20 }}><Skeleton rows={4} /></div>
        ) : trail.error ? (
          <div style={{ padding: 20 }}><Notice s="act">{trail.error}</Notice></div>
        ) : rows.length === 0 ? (
          <Empty title={t("v2.dlc.audit.empty")} />
        ) : (
          <table className="v3-table">
            <thead><tr><th>{t("v2.dlc.audit.at")}</th><th>{t("v2.dlc.audit.actor")}</th><th>{t("v2.dlc.audit.action")}</th><th>{t("v2.dlc.audit.target")}</th></tr></thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.id}>
                  <td className="mono" style={{ whiteSpace: "nowrap" }}>{stamp(row.at, 19)}</td>
                  <td>{row.actor}</td>
                  <td className="mono">{row.action}</td>
                  <td className="mono" style={{ color: "var(--v3-text-2)" }}>{row.target || "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Panel>
    </div>
  );
}
