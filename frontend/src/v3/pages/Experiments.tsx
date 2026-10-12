import "./experiments.css";

import { Plus, RefreshCw, Search } from "lucide-react";
import { type ReactNode, useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { useSearchParams } from "react-router-dom";

import { api, type RuntimeCanaryInfo } from "../../lib/api";
import { type ExperimentInfo, verdictLabel } from "../../lib/experiments";
import { CanaryDetail } from "../../v2/pages/canary/CanaryDetail";
import { CanaryStart } from "../../v2/pages/canary/CanaryStart";
import { versionsLabel, weightsLabel } from "../../v2/pages/canary/common";
import { ExperimentDetail } from "../../v2/pages/experiments/ExperimentDetail";
import { ExperimentStart } from "../../v2/pages/experiments/ExperimentStart";
import { stageLabel } from "../../v2/pages/experiments/common";
import { V2ToastProvider } from "../../v2/ui";
import { useWorkspace } from "../../workspace/workspace-context";
import { ago } from "../format";
import { useLoad } from "../hooks";
import { Btn, Chip, Empty, Filters, Lamp, Notice, PageHead, Panel, type Signal, Skeleton, Stat } from "../ui";
import { canarySignal, experimentSignal } from "./experiments/signal";

type Mode = "configuration" | "canary";
// same cadence as V2: the list follows runs that move on their own
const LIST_POLL_MS = 8000;

/**
 * An experiment's or a canary's working view (the staged flow: recommend →
 * bundles → A/B → traffic → verdict → promote, or setup → ramp → complete) is
 * V2's own module, rendered here on the V3 theme. It drives the same `?view=` /
 * `?canary=` params this page reads, so moving between the list and a flow never
 * leaves V3.
 */
function Hosted({ children }: { children: ReactNode }) {
  return (
    <V2ToastProvider>
      <div className="v2 v3-host">{children}</div>
    </V2ToastProvider>
  );
}

function useStageText() {
  const { t } = useTranslation();
  return {
    experiment: (e: ExperimentInfo) => stageLabel(t, e),
    canary: (c: RuntimeCanaryInfo) =>
      c.running_action
        ? t("v2.canary.actionRunning", { action: t(`v2.canary.action.${c.running_action}`, { defaultValue: c.running_action }) })
        : t(`v2.canary.stage.${c.stage}`, { defaultValue: c.stage }),
  };
}

function ExperimentBoard({ experiments, loading, error, reload }: {
  experiments: ExperimentInfo[];
  loading: boolean;
  error: string | null;
  reload: () => void;
}) {
  const { t } = useTranslation();
  const [, setParams] = useSearchParams();
  const stage = useStageText();
  const [state, setState] = useState<"all" | Signal>("all");
  const [agent, setAgent] = useState("");
  const [q, setQ] = useState("");
  const hasRunning = experiments.some((e) => e.status === "running");
  const agents = useMemo(() => [...new Set(experiments.map((e) => e.agent_name))].sort(), [experiments]);
  const rows = useMemo(() => {
    const needle = q.trim().toLowerCase();
    return experiments
      .filter((e) => state === "all" || experimentSignal(e) === state)
      .filter((e) => !agent || e.agent_name === agent)
      .filter((e) => !needle || `${e.name} ${e.id}`.toLowerCase().includes(needle));
  }, [experiments, state, agent, q]);
  const count = (s: Signal) => experiments.filter((e) => experimentSignal(e) === s).length;
  // what needs someone: a run in flight, or a verdict waiting to be promoted or cleaned
  const attention = experiments.filter((e) => experimentSignal(e) === "info" || experimentSignal(e) === "wait");
  const open = (e: ExperimentInfo) => setParams({ view: "detail", id: e.id });

  return (
    <>
      <div className="v3-grid c4">
        <Panel signal={count("info") ? "info" : undefined}><Stat label={t("v3.experiments.running")} value={count("info")} foot={t("v3.experiments.runningFoot")} /></Panel>
        <Panel signal={count("wait") ? "wait" : undefined}>
          <Stat label={t("v3.experiments.ready")} value={count("wait")} signal={count("wait") ? "wait" : undefined} foot={t("v3.experiments.readyFoot")} />
        </Panel>
        <Panel signal={count("ok") ? "ok" : undefined}><Stat label={t("v3.experiments.promoted")} value={count("ok")} foot={t("v3.experiments.promotedFoot")} /></Panel>
        <Panel signal={count("act") ? "act" : undefined}><Stat label={t("v3.experiments.failed")} value={count("act")} signal={count("act") ? "act" : undefined} foot={t("v3.experiments.totalFoot", { count: experiments.length })} /></Panel>
      </div>

      {attention.length > 0 && (
        <Panel title={t("v3.experiments.needsYou")} signal={attention.some((e) => experimentSignal(e) === "wait") ? "wait" : "info"} flush end={<span className="mono">{attention.length}</span>}>
          <table className="v3-table">
            <tbody>
              {attention.map((e) => {
                const s = experimentSignal(e);
                return (
                  <tr key={e.id} className="click" onClick={() => open(e)}>
                    <td style={{ width: 30 }}><Lamp s={s} live /></td>
                    <td><div className="v3-name"><div><b>{e.name}</b><small>{e.agent_name}</small></div></div></td>
                    <td>{stage.experiment(e)}</td>
                    <td style={{ color: "var(--v3-text-2)" }}>{e.progress ?? (s === "wait" ? t("v3.experiments.awaitDecision") : "—")}</td>
                    <td className="num" style={{ color: "var(--v3-text-3)" }}>{ago(e.created_at)}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </Panel>
      )}

      {hasRunning && <Notice>{t("evalPage.experiment.runningGuard")}</Notice>}

      <div className="v3-exp-bar">
        <Filters
          value={state}
          onChange={setState}
          options={[
            { value: "all", label: t("v3.experiments.all"), count: experiments.length },
            { value: "info", label: t("v2.experiments.status.running"), s: "info", count: count("info") },
            { value: "wait", label: t("v3.experiments.ready"), s: "wait", count: count("wait") },
            { value: "ok", label: t("v2.experiments.status.promoted"), s: "ok", count: count("ok") },
            { value: "act", label: t("v2.experiments.status.failed"), s: "act", count: count("act") },
            { value: "off", label: t("v2.experiments.status.cleaned"), s: "off", count: count("off") },
          ]}
        />
        <select className="v3-select" style={{ width: 200 }} value={agent} onChange={(e) => setAgent(e.target.value)} aria-label={t("v3.experiments.agent")}>
          <option value="">{t("v3.experiments.allAgents")}</option>
          {agents.map((a) => <option key={a} value={a}>{a}</option>)}
        </select>
        <div className="v3-exp-search">
          <Search size={14} aria-hidden="true" />
          <input className="v3-input" value={q} onChange={(e) => setQ(e.target.value)} placeholder={t("v3.experiments.search")} aria-label={t("v3.experiments.search")} />
        </div>
        <Btn kind="ghost" onClick={reload} title={t("v3.experiments.refresh")}><RefreshCw size={14} /></Btn>
        <Btn kind="primary" disabled={hasRunning} title={hasRunning ? t("evalPage.experiment.runningGuard") : undefined}
          onClick={() => setParams({ view: "new" })}>
          <Plus size={14} /> {t("v3.experiments.start")}
        </Btn>
      </div>

      <Panel flush>
        {loading && !experiments.length && !error ? (
          <div style={{ padding: 20 }}><Skeleton rows={5} /></div>
        ) : error ? (
          <div style={{ padding: 20 }}><Notice s="act">{error}</Notice></div>
        ) : rows.length === 0 ? (
          <Empty title={experiments.length ? t("v3.experiments.noMatch") : t("v3.experiments.empty")}>{!experiments.length && t("v3.experiments.emptySub")}</Empty>
        ) : (
          <table className="v3-table">
            <thead>
              <tr>
                <th />
                <th>{t("v3.experiments.colName")}</th>
                <th>{t("v3.experiments.agent")}</th>
                <th>{t("v3.experiments.colStage")}</th>
                <th>{t("v3.experiments.colVerdict")}</th>
                <th>{t("v3.experiments.colStatus")}</th>
                <th className="num">{t("v3.experiments.colCreated")}</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((e) => {
                const s = experimentSignal(e);
                const v = e.artifacts.verdict;
                const weak = !!v && (v.significant === false || v.verdict.includes("insufficient"));
                return (
                  <tr key={e.id} className="click" onClick={() => open(e)}>
                    <td style={{ width: 30 }}><Lamp s={s} live={s === "info"} /></td>
                    <td><div className="v3-name"><div><b>{e.name}</b><small>{e.id}</small></div></div></td>
                    <td style={{ color: "var(--v3-text-2)" }}>{e.agent_name}</td>
                    <td>{e.running_action ? <Chip s="info">{stage.experiment(e)}</Chip> : stage.experiment(e)}</td>
                    <td>{v ? <Chip s={weak ? "wait" : "ok"}>{verdictLabel(t, v)}</Chip> : <span style={{ color: "var(--v3-text-3)" }}>—</span>}</td>
                    <td><Chip s={s === "off" ? undefined : s}>{t(`v2.experiments.status.${e.status}`, { defaultValue: e.status })}</Chip></td>
                    <td className="num" style={{ color: "var(--v3-text-3)" }}>{ago(e.created_at)}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </Panel>
    </>
  );
}

function CanaryBoard({ canaries, loading, error, reload }: {
  canaries: RuntimeCanaryInfo[];
  loading: boolean;
  error: string | null;
  reload: () => void;
}) {
  const { t } = useTranslation();
  const [, setParams] = useSearchParams();
  const stage = useStageText();
  const [state, setState] = useState<"all" | Signal>("all");
  const [agent, setAgent] = useState("");
  const [q, setQ] = useState("");
  const agents = useMemo(() => [...new Set(canaries.map((c) => c.champion_agent_name))].sort(), [canaries]);
  const rows = useMemo(() => {
    const needle = q.trim().toLowerCase();
    return canaries
      .filter((c) => state === "all" || canarySignal(c) === state)
      .filter((c) => !agent || c.champion_agent_name === agent)
      .filter((c) => !needle || `${c.name} ${c.id}`.toLowerCase().includes(needle));
  }, [canaries, state, agent, q]);
  const count = (s: Signal) => canaries.filter((c) => canarySignal(c) === s).length;
  const attention = canaries.filter((c) => canarySignal(c) === "wait" || canarySignal(c) === "info");
  const open = (c: RuntimeCanaryInfo) => setParams({ mode: "canary", canary: c.id });

  return (
    <>
      <div className="v3-grid c4">
        <Panel signal={count("wait") ? "wait" : undefined}>
          <Stat label={t("v3.experiments.canaryDecide")} value={count("wait")} signal={count("wait") ? "wait" : undefined} foot={t("v3.experiments.canaryDecideFoot")} />
        </Panel>
        <Panel signal={count("info") ? "info" : undefined}><Stat label={t("v3.experiments.canaryRamping")} value={count("info")} foot={t("v3.experiments.canaryRampingFoot")} /></Panel>
        <Panel signal={count("ok") ? "ok" : undefined}><Stat label={t("v2.canary.status.completed")} value={count("ok")} foot={t("v3.experiments.canaryDoneFoot")} /></Panel>
        <Panel signal={count("act") ? "act" : undefined}><Stat label={t("v2.canary.status.rolled_back")} value={count("act")} signal={count("act") ? "act" : undefined} foot={t("v3.experiments.totalFoot", { count: canaries.length })} /></Panel>
      </div>

      {attention.length > 0 && (
        <Panel title={t("v3.experiments.needsYou")} signal={attention.some((c) => canarySignal(c) === "wait") ? "wait" : "info"} flush end={<span className="mono">{attention.length}</span>}>
          <table className="v3-table">
            <tbody>
              {attention.map((c) => {
                const s = canarySignal(c);
                return (
                  <tr key={c.id} className="click" onClick={() => open(c)}>
                    <td style={{ width: 30 }}><Lamp s={s} live /></td>
                    <td><div className="v3-name"><div><b>{c.name}</b><small>{c.champion_agent_name}</small></div></div></td>
                    <td className="mono">{versionsLabel(c.artifacts.setup)}</td>
                    <td className="mono">{weightsLabel(c.artifacts.setup)}</td>
                    <td>{stage.canary(c)}</td>
                    <td style={{ color: "var(--v3-text-2)" }}>{c.progress ?? (s === "wait" ? t("v3.experiments.awaitDecision") : "—")}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </Panel>
      )}

      <div className="v3-exp-bar">
        <Filters
          value={state}
          onChange={setState}
          options={[
            { value: "all", label: t("v3.experiments.all"), count: canaries.length },
            { value: "wait", label: t("v3.experiments.canaryDecide"), s: "wait", count: count("wait") },
            { value: "info", label: t("v3.experiments.canaryRamping"), s: "info", count: count("info") },
            { value: "ok", label: t("v2.canary.status.completed"), s: "ok", count: count("ok") },
            { value: "act", label: t("v2.canary.status.rolled_back"), s: "act", count: count("act") },
            { value: "off", label: t("v2.canary.status.cleaned"), s: "off", count: count("off") },
          ]}
        />
        <select className="v3-select" style={{ width: 200 }} value={agent} onChange={(e) => setAgent(e.target.value)} aria-label={t("v3.experiments.agent")}>
          <option value="">{t("v3.experiments.allAgents")}</option>
          {agents.map((a) => <option key={a} value={a}>{a}</option>)}
        </select>
        <div className="v3-exp-search">
          <Search size={14} aria-hidden="true" />
          <input className="v3-input" value={q} onChange={(e) => setQ(e.target.value)} placeholder={t("v3.experiments.search")} aria-label={t("v3.experiments.search")} />
        </div>
        <Btn kind="ghost" onClick={reload} title={t("v3.experiments.refresh")}><RefreshCw size={14} /></Btn>
        <Btn kind="primary" onClick={() => setParams({ mode: "canary", canary: "new" })}><Plus size={14} /> {t("v3.experiments.newCanary")}</Btn>
      </div>

      <Panel flush>
        {loading && !canaries.length && !error ? (
          <div style={{ padding: 20 }}><Skeleton rows={5} /></div>
        ) : error ? (
          <div style={{ padding: 20 }}><Notice s="act">{error}</Notice></div>
        ) : rows.length === 0 ? (
          <Empty title={canaries.length ? t("v3.experiments.noMatch") : t("v3.experiments.canaryEmpty")}>{!canaries.length && t("v3.experiments.canaryEmptySub")}</Empty>
        ) : (
          <table className="v3-table">
            <thead>
              <tr>
                <th />
                <th>{t("v3.experiments.colName")}</th>
                <th>{t("v3.experiments.agent")}</th>
                <th>{t("v3.experiments.colVersions")}</th>
                <th>{t("v3.experiments.colWeights")}</th>
                <th>{t("v3.experiments.colStage")}</th>
                <th>{t("v3.experiments.colStatus")}</th>
                <th className="num">{t("v3.experiments.colCreated")}</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((c) => {
                const s = canarySignal(c);
                return (
                  <tr key={c.id} className="click" onClick={() => open(c)}>
                    <td style={{ width: 30 }}><Lamp s={s} live={s === "info"} /></td>
                    <td><div className="v3-name"><div><b>{c.name}</b><small>{c.id}</small></div></div></td>
                    <td style={{ color: "var(--v3-text-2)" }}>{c.champion_agent_name}</td>
                    <td className="mono">{versionsLabel(c.artifacts.setup)}</td>
                    <td className="mono">{weightsLabel(c.artifacts.setup)}</td>
                    <td>{c.running_action ? <Chip s="info">{stage.canary(c)}</Chip> : stage.canary(c)}</td>
                    <td><Chip s={s === "off" ? undefined : s}>{t(`v2.canary.status.${c.status}`)}</Chip></td>
                    <td className="num" style={{ color: "var(--v3-text-3)" }}>{ago(c.created_at)}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </Panel>
    </>
  );
}

/**
 * Experiments — configuration-bundle A/B experiments and runtime canaries, led
 * by what is in flight or waiting on a decision. Same params as V2: `mode=canary`
 * for the canary board; `view=new` (with `agent=` / `lookback=` / `baselineRun=` /
 * `sourceRun=` prefills) and `view=detail&id=` for an experiment; `canary=new`
 * (with `champion=` / `sourceExp=`) or `canary=<id>` for a canary.
 */
export function V3Experiments() {
  const { t } = useTranslation();
  const [params, setParams] = useSearchParams();
  const { current } = useWorkspace();
  const ws = current?.id ?? "";
  const mode: Mode = params.get("mode") === "canary" ? "canary" : "configuration";
  const view = params.get("view");
  const id = params.get("id");
  const canaryParam = mode === "canary" ? params.get("canary") : null;
  const list = useLoad(() => api.v2Experiments(), `v3-experiments:${ws}`);
  const canaries = useLoad(
    () => (mode === "canary" ? api.listRuntimeCanaries() : Promise.resolve({ canaries: [] as RuntimeCanaryInfo[] })),
    `v3-canaries:${ws}:${mode}`,
  );
  const reload = list.reload;
  const reloadCanaries = canaries.reload;
  useEffect(() => {
    const timer = window.setInterval(() => {
      reload();
      if (mode === "canary") reloadCanaries();
    }, LIST_POLL_MS);
    return () => window.clearInterval(timer);
  }, [reload, reloadCanaries, mode]);
  const experiments = list.data?.experiments ?? [];
  const hasRunning = experiments.some((e) => e.status === "running");

  if (canaryParam === "new") return <Hosted><CanaryStart /></Hosted>;
  if (canaryParam) return <Hosted><CanaryDetail key={canaryParam} id={canaryParam} /></Hosted>;
  if (mode === "configuration" && view === "new") return <Hosted><ExperimentStart hasRunning={hasRunning} /></Hosted>;
  if (mode === "configuration" && view === "detail" && id) {
    return <Hosted><ExperimentDetail key={id} id={id} hasRunning={hasRunning} /></Hosted>;
  }

  return (
    <div className="v3-reveal v3-exp" style={{ display: "grid", gap: 16 }}>
      <PageHead
        eyebrow={t("v3.experiments.eyebrow")}
        title={t("v3.experiments.title")}
        sub={t(mode === "canary" ? "v3.experiments.subCanary" : "v3.experiments.sub")}
        end={
          <Filters
            value={mode}
            onChange={(next) => setParams(next === "canary" ? { mode: "canary" } : {})}
            options={[
              { value: "configuration", label: t("v2.experiments.mode.configuration") },
              { value: "canary", label: t("v2.experiments.mode.canary") },
            ]}
          />
        }
      />
      {mode === "canary" ? (
        <CanaryBoard canaries={canaries.data?.canaries ?? []} loading={canaries.loading} error={canaries.error} reload={reloadCanaries} />
      ) : (
        <ExperimentBoard experiments={experiments} loading={list.loading} error={list.error} reload={reload} />
      )}
    </div>
  );
}
