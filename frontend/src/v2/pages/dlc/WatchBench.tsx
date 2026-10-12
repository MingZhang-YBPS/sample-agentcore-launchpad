/**
 * The drift bench and the fix ladder — §7.7 and §7.8.
 *
 * A model can change under an agent without a line of code changing, so a standard
 * applied only at release time stops being a standard. This page schedules the
 * re-run, then separates the three questions a dip raises:
 *
 * - is this unusual? — the rate against its rolling-median baseline, and the page
 *   says plainly when the baseline is too short to judge;
 * - is it the agent or the judge? — drift triage, which re-runs the frozen
 *   calibration set rather than guessing;
 * - did my fix work? — the ladder, with Δ, a p-value and *which layer changed*,
 *   because a gain across two simultaneous changes belongs to neither.
 *
 * "No alert" is rendered explicitly, so fourteen quiet days cannot be a dead monitor.
 */
import { useState } from "react";
import { useTranslation } from "react-i18next";

import { useAuth } from "../../../auth/auth-context";
import { dlcApi, errorMessage } from "../../../lib/api";
import { DLC_DIMENSIONS, type RunComparison, type WatchView } from "../../../lib/dlc";
import { useLoad, useV2Toast } from "../../hooks";
import { Alert, Button, Card, Field, Kpi, LinkButton, Select, Spin, Table, Tag } from "../../ui";
import { BandChart, LadderChart } from "./charts";

const pct = (v: number | null | undefined, digits = 1) =>
  v === null || v === undefined ? "—" : `${(v * 100).toFixed(digits)}%`;

function WatchConfigCard({ view, agentId, onReload }: { view: WatchView; agentId: string; onReload: () => void }) {
  const { t } = useTranslation();
  const { can } = useAuth();
  const toast = useV2Toast();
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

  const save = () => {
    setBusy(true);
    dlcApi
      .putWatch(agentId, {
        every: form.every as "daily" | "weekly",
        at_hour: form.at_hour,
        repeats: form.repeats,
        max_cost_usd: form.max_cost_usd,
        enabled: form.enabled,
      })
      .then(() => {
        toast("success", t("v2.dlc.watch.saved"));
        onReload();
      })
      .catch((error: unknown) => toast("error", errorMessage(error)))
      .finally(() => setBusy(false));
  };

  return (
    <Card
      title={t("v2.dlc.watch.config")}
      sub={t("v2.dlc.watch.configSub")}
      end={
        config ? (
          <Tag tone={config.enabled ? "green" : "gray"}>
            {t(config.enabled ? "v2.dlc.watch.on" : "v2.dlc.watch.off")}
          </Tag>
        ) : (
          <Tag tone="orange">{t("v2.dlc.watch.notConfigured")}</Tag>
        )
      }
    >
      <div className="v2-dlc-grid3">
        <Field label={t("v2.dlc.watch.every")}>
          <Select
            value={form.every}
            onChange={(v) => setForm({ ...form, every: v as "daily" | "weekly" })}
            options={["daily", "weekly"].map((v) => ({ value: v, label: t(`v2.dlc.watch.${v}`) }))}
          />
        </Field>
        <Field label={t("v2.dlc.watch.atHour")}>
          <input
            className="v2-input"
            type="number"
            min={0}
            max={23}
            value={form.at_hour}
            onChange={(e) => setForm({ ...form, at_hour: Number(e.target.value) })}
          />
        </Field>
        <Field label={t("v2.dlc.watch.repeats")} hint={t("v2.dlc.watch.repeatsHint")}>
          <input
            className="v2-input"
            type="number"
            min={1}
            max={10}
            value={form.repeats}
            onChange={(e) => setForm({ ...form, repeats: Number(e.target.value) })}
          />
        </Field>
        <Field label={t("v2.dlc.watch.ceiling")} hint={t("v2.dlc.watch.ceilingHint")}>
          <input
            className="v2-input"
            type="number"
            min={0}
            step={0.5}
            value={form.max_cost_usd ?? ""}
            onChange={(e) => setForm({ ...form, max_cost_usd: e.target.value === "" ? null : Number(e.target.value) })}
          />
        </Field>
        <Field label={t("v2.dlc.watch.enabled")}>
          <input type="checkbox" checked={form.enabled} onChange={(e) => setForm({ ...form, enabled: e.target.checked })} />
        </Field>
      </div>
      {config && (
        <p className="v2-muted">
          {t("v2.dlc.watch.nextDue", { at: config.next_due_at?.slice(0, 16).replace("T", " ") ?? "—" })}
          {config.last_status ? ` · ${t(`v2.dlc.watch.lastStatus.${config.last_status}`, { defaultValue: config.last_status })}` : ""}
          {config.last_detail ? ` — ${config.last_detail}` : ""}
        </p>
      )}
      {mayEdit && (
        <div className="v2-dlc-actions">
          <Button kind="primary" disabled={busy} onClick={save}>
            {t("v2.common.save")}
          </Button>
          <Button
            disabled={busy || !config}
            onClick={() => {
              setBusy(true);
              dlcApi
                .runWatch(agentId)
                .then(() => {
                  toast("success", t("v2.dlc.watch.started"));
                  onReload();
                })
                .catch((error: unknown) => toast("error", errorMessage(error)))
                .finally(() => setBusy(false));
            }}
          >
            {t("v2.dlc.watch.runNow")}
          </Button>
        </div>
      )}
    </Card>
  );
}

function Alerts({ view }: { view: WatchView }) {
  const { t } = useTranslation();
  const alerts = view.alerts;
  const firing = [...alerts.count, ...alerts.score, ...alerts.distribution];
  return (
    <Card title={t("v2.dlc.watch.alerts")} sub={t("v2.dlc.watch.alertsSub")}>
      {alerts.quiet ? (
        <Alert tone="success">
          {t("v2.dlc.watch.quiet", { n: alerts.runs_considered, at: alerts.checked_at.slice(0, 16).replace("T", " ") })}
        </Alert>
      ) : (
        <ul className="v2-dlc-alerts">
          {firing.map((a, i) => (
            <li key={`${a.family}:${a.criterion_key ?? a.dimension ?? i}`}>
              <Tag tone={a.severity === "page" ? "red" : "orange"}>{t(`v2.dlc.watch.family.${a.family}`)}</Tag>
              <Tag tone="gray">{t(`v2.dlc.watch.severity.${a.severity}`)}</Tag>
              <span>{a.detail}</span>
            </li>
          ))}
        </ul>
      )}
      {Object.entries(alerts.baseline_ready).some(([, ready]) => !ready) && (
        <Alert tone="warn">
          {t("v2.dlc.watch.baselineNotReady", {
            dims: Object.entries(alerts.baseline_ready)
              .filter(([, ready]) => !ready)
              .map(([d]) => t(`v2.dlc.dimension.${d}`, { defaultValue: d }))
              .join(", "),
          })}
        </Alert>
      )}
    </Card>
  );
}

function DriftPanel({ view }: { view: WatchView }) {
  const { t } = useTranslation();
  const drift = view.drift;
  return (
    <Card title={t("v2.dlc.watch.drift")} sub={t("v2.dlc.watch.driftSub")}>
      <Alert>{drift.hint}</Alert>
      <div className="v2-dlc-kpis">
        <Kpi
          label={t("v2.dlc.watch.versionsInWindow")}
          value={drift.agent_versions_in_window.join(", ") || "—"}
          tone={drift.versions_changed ? "bad" : undefined}
        />
        <Kpi
          label={t("v2.dlc.watch.needRecalibration")}
          value={String(drift.needs_recalibration.length)}
          tone={drift.needs_recalibration.length > 0 ? "bad" : undefined}
        />
      </div>
      <Table
        rows={drift.judges}
        rowKey={(row) => row.criterion_key}
        empty={t("v2.dlc.watch.noJudges")}
        columns={[
          { key: "key", title: t("v2.dlc.criteria.key"), render: (row) => <span className="mono">{row.criterion_key}</span> },
          { key: "ev", title: t("v2.dlc.criteria.evaluatorId"), render: (row) => <span className="mono">{row.evaluator_id ?? "—"}</span> },
          {
            key: "cal",
            title: t("v2.dlc.watch.calibrated"),
            render: (row) => (
              <Tag tone={row.calibrated ? "green" : "orange"}>
                {row.calibrated
                  ? t("v2.dlc.watch.yes")
                  : t(`v2.dlc.calibration.reason.${row.reason ?? "never_calibrated"}`, { defaultValue: row.reason ?? "" })}
              </Tag>
            ),
          },
          { key: "k", title: "κ", render: (row) => (row.judge_human_kappa === null ? "—" : row.judge_human_kappa.toFixed(2)) },
        ]}
      />
    </Card>
  );
}

function ComparePanel({ agentId }: { agentId: string }) {
  const { t } = useTranslation();
  const ladder = useLoad<RunComparison>(() => dlcApi.ladder(agentId), `ladder:${agentId}`);
  const data = ladder.data;
  if (ladder.loading && !data) return <Spin />;
  if (!data) return <Alert tone="error">{ladder.error ?? t("v2.dlc.compare.noRuns")}</Alert>;
  if (!data.comparable && !data.two_numbers) {
    return (
      <Card title={t("v2.dlc.compare.title")}>
        <Alert tone="warn">{data.incomparable_reason ?? t("v2.dlc.compare.incomparable")}</Alert>
        {data.runs && <LadderChart rungs={data.runs} />}
      </Card>
    );
  }
  return (
    <>
      <Card title={t("v2.dlc.compare.title")} sub={t("v2.dlc.compare.sub")}>
        {data.warning && <Alert tone="warn">{data.warning}</Alert>}
        {!data.comparable && data.two_numbers && (
          <>
            <Alert tone="warn">{data.incomparable_reason}</Alert>
            <div className="v2-dlc-kpis">
              <Kpi
                label={t("v2.dlc.compare.agentDelta")}
                value={data.two_numbers.agent_delta === null ? "—" : `${(data.two_numbers.agent_delta * 100).toFixed(1)}pp`}
                sub={t("v2.dlc.compare.agentDeltaSub")}
                tone={(data.two_numbers.agent_delta ?? 0) < 0 ? "bad" : undefined}
              />
              <Kpi
                label={t("v2.dlc.compare.standardDelta")}
                value={data.two_numbers.standard_delta === null ? "—" : `${(data.two_numbers.standard_delta * 100).toFixed(1)}pp`}
                sub={t("v2.dlc.compare.standardDeltaSub")}
              />
              <Kpi label={t("v2.dlc.compare.added")} value={data.two_numbers.added_criteria.join(", ") || "—"} />
            </div>
          </>
        )}
        <LadderChart rungs={data.runs} />
      </Card>

      <Card title={t("v2.dlc.compare.perCriterion")} flush>
        <Table
          rows={data.criteria}
          rowKey={(row) => row.key}
          empty={t("v2.dlc.compare.noCriteria")}
          columns={[
            { key: "key", title: t("v2.dlc.criteria.key"), render: (row) => <span className="mono">{row.key}</span> },
            { key: "before", title: t("v2.dlc.compare.before"), render: (row) => `${pct(row.before.rate)} (n=${row.before.n})` },
            { key: "after", title: t("v2.dlc.compare.after"), render: (row) => `${pct(row.after.rate)} (n=${row.after.n})` },
            {
              key: "delta",
              title: "Δ",
              render: (row) =>
                row.delta === undefined ? (
                  "—"
                ) : (
                  <span className={row.delta >= 0 ? "v2-dlc-up" : "v2-dlc-down"}>
                    {row.delta >= 0 ? "+" : ""}
                    {(row.delta * 100).toFixed(1)}pp
                  </span>
                ),
            },
            {
              key: "p",
              title: "p",
              render: (row) =>
                row.p_value === undefined || row.p_value === null ? (
                  "—"
                ) : (
                  <>
                    {row.p_value.toFixed(3)}
                    {!row.significant && <Tag tone="gray">{t("v2.dlc.compare.noise")}</Tag>}
                  </>
                ),
            },
          ]}
        />
      </Card>

      <Card title={t("v2.dlc.compare.moves")}>
        <div className="v2-dlc-kpis">
          <Kpi label={t("v2.dlc.compare.fixed")} value={String(data.fixed.length)} />
          <Kpi
            label={t("v2.dlc.compare.newFailures")}
            value={String(data.new_failures.length)}
            tone={data.new_failures.length > 0 ? "bad" : undefined}
          />
          <Kpi label={t("v2.dlc.compare.stillFailing")} value={String(data.still_failing.length)} />
        </div>
        {data.new_failures.length > 0 && (
          <>
            <h4>{t("v2.dlc.compare.newFailures")}</h4>
            <ul className="v2-dlc-moves">
              {data.new_failures.slice(0, 20).map((m) => (
                <li key={`${m.criterion_key}:${m.scenario_id}`}>
                  <span className="mono">{m.criterion_key}</span>
                  <span className="mono">{m.scenario_id}</span>
                </li>
              ))}
            </ul>
          </>
        )}
      </Card>
    </>
  );
}

export function WatchBench({ agentId, tab }: { agentId: string; tab: "watch" | "compare" }) {
  const { t } = useTranslation();
  const [nonce, setNonce] = useState(0);
  const [dimension, setDimension] = useState<string>("quality");
  const view = useLoad<WatchView>(() => dlcApi.watch(agentId), `watch:${agentId}:${nonce}`);

  if (tab === "compare") return <ComparePanel agentId={agentId} />;
  if (view.loading && !view.data) return <Spin />;
  if (!view.data) return <Alert tone="error">{view.error ?? t("v2.dlc.watch.notFound")}</Alert>;
  const data = view.data;
  const points = data.series[dimension] ?? [];

  return (
    <>
      <WatchConfigCard view={data} agentId={agentId} onReload={() => setNonce((n) => n + 1)} />
      <Alerts view={data} />
      <Card
        title={t("v2.dlc.watch.series")}
        sub={t("v2.dlc.watch.seriesSub")}
        end={
          <Select
            value={dimension}
            onChange={setDimension}
            options={DLC_DIMENSIONS.filter((d) => (data.series[d] ?? []).length > 0 || d === dimension).map((d) => ({
              value: d,
              label: t(`v2.dlc.dimension.${d}`),
            }))}
          />
        }
      >
        <BandChart points={points} />
      </Card>
      <DriftPanel view={data} />
      <Card title={t("v2.dlc.watch.runs")} flush>
        <Table
          rows={data.runs}
          rowKey={(row) => row.id}
          empty={t("v2.dlc.watch.noRuns")}
          columns={[
            { key: "id", title: t("v2.dlc.watch.run"), render: (row) => <span className="mono">{row.id.slice(0, 8)}</span> },
            { key: "at", title: t("v2.dlc.watch.at"), render: (row) => row.at?.slice(0, 16).replace("T", " ") ?? "—" },
            { key: "split", title: t("v2.dlc.golden.split"), render: (row) => row.split ?? "—" },
            { key: "v", title: t("v2.dlc.release.version"), render: (row) => row.agent_version ?? "—" },
            { key: "cv", title: t("v2.dlc.release.criteriaVersion"), render: (row) => `v${row.criteria_set_version ?? "—"}` },
            {
              key: "act",
              title: "",
              render: (row) => (
                <LinkButton onClick={() => window.open(`/v2/eval/tasks?view=detail&id=${row.id}`, "_self")}>
                  {t("v2.common.open")}
                </LinkButton>
              ),
            },
          ]}
        />
      </Card>
    </>
  );
}
