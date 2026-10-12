import { ArrowLeft, ArrowRight, Rocket, Search } from "lucide-react";
import { type ReactNode, useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { useNavigate, useSearchParams } from "react-router-dom";

import { api, dlcApi, errorMessage, type V2Range } from "../../../lib/api";
import { unpricedJudges } from "../../../lib/dlc";
import { CLOUD_VALUE_PREFIX } from "../../../lib/evaluation";
import { evaluatorLabel, type EvaluatorLevel } from "../../../lib/evaluators";
import { INSIGHT_TYPES, insightLabel } from "../../../v2/online";
import { LogSourceFields } from "../../../v2/pages/tasks/LogSourceFields";
import { LogStreamPicker } from "../../../v2/pages/tasks/LogStreamPicker";
import { SERVICE_NAME_RE, type TaskMode } from "../../../v2/tasks";
import { ago } from "../../format";
import { useLoad, useToast } from "../../hooks";
import { Btn, Chip, Empty, Filters, Notice, PageHead, Panel, Skeleton } from "../../ui";
import { HostedV2 } from "./common";
import { detailUrl } from "./model";

/* Mirrors V2's TaskWizard (v2/pages/tasks/TaskWizard.tsx): the same draft, seed,
   validation, scope building and request — only the presentation is V3's. */

type Source = "window" | "sessions" | "logs" | "dataset";
type Target = "agent" | "cloudwatch";
type Strategy = "history" | "continuous";

const LOOKBACKS = [1, 6, 24, 72, 168, 336];
const SESSION_RANGE: V2Range = "7d";
const MAX_EVALUATORS = 10;
/** Insight clustering needs at least this many sessions. */
const MIN_INSIGHT_SESSIONS = 3;

interface Draft {
  name: string;
  description: string;
  target: Target;
  agentId: string;
  serviceName: string;
  logGroups: string[];
  mode: TaskMode;
  source: Source;
  strategy: Strategy;
  lookbackHours: number;
  sessionIds: string[];
  logSessionIds: string[];
  logHours: number;
  /** local dataset id, or `cloud:<datasetId>` */
  dataset: string;
  sampling: number;
  sessionTimeout: number;
  evaluators: string[];
  insights: string[];
  /** pass^k: each dataset scenario runs this many times (1 = off) */
  repeats: number;
  confirmCost: boolean;
}

const EMPTY: Draft = {
  name: "",
  description: "",
  target: "agent",
  agentId: "",
  serviceName: "",
  logGroups: [],
  mode: "evaluators",
  source: "window",
  strategy: "history",
  lookbackHours: 24,
  sessionIds: [],
  logSessionIds: [],
  logHours: 168,
  dataset: "",
  sampling: 10,
  sessionTimeout: 15,
  evaluators: ["Builtin.Correctness", "Builtin.Helpfulness"],
  insights: [...INSIGHT_TYPES],
  repeats: 1,
  confirmCost: false,
};

interface Handoff {
  agent: string | null;
  dataset: string | null;
  evaluators: string[];
}

/** `?from=run:<id>` / `?from=online:<id>` (copy), or hand-off params (agent / dataset / evaluators). */
async function seedDraft(from: string | null, handoff: Handoff, copySuffix: string): Promise<Draft> {
  if (from?.startsWith("run:")) {
    const run = await api.getEvaluationRun(from.slice(4));
    const name = run.dataset_name ?? "";
    const copied = run.mode === "insights" ? { mode: "insights" as const, insights: run.evaluators } : { evaluators: run.evaluators };
    const target = run.log_source
      ? { target: "cloudwatch" as const, serviceName: run.log_source.service_name, logGroups: run.log_source.log_group_names }
      : { agentId: run.agent_id };
    const base: Draft = { ...EMPTY, name: `${run.name || run.agent_name}${copySuffix}`.slice(0, 64), description: run.description ?? "", ...target, ...copied };
    if (name.startsWith("window:")) return { ...base, source: "window", lookbackHours: parseInt(name.slice(7), 10) || 24 };
    if (name.startsWith("cloud:") && run.dataset_id) return { ...base, source: "dataset", dataset: `${CLOUD_VALUE_PREFIX}${run.dataset_id}` };
    if (run.dataset_id) return { ...base, source: "dataset", dataset: run.dataset_id };
    if (name.startsWith("logs:")) return { ...base, source: "logs", logSessionIds: run.session_ids };
    return { ...base, source: "sessions", sessionIds: run.session_ids };
  }
  if (from?.startsWith("online:")) {
    const cfg = await api.v2OnlineConfig(from.slice(7));
    return {
      ...EMPTY,
      name: `${cfg.description || cfg.name || cfg.config_id}${copySuffix}`.slice(0, 64),
      agentId: cfg.agent_id ?? "",
      strategy: "continuous",
      sampling: cfg.sampling_percentage ?? 10,
      sessionTimeout: cfg.session_timeout_minutes ?? 15,
      evaluators: cfg.evaluators,
    };
  }
  const seeded: Draft = { ...EMPTY, agentId: handoff.agent ?? "" };
  if (handoff.evaluators.length) seeded.evaluators = handoff.evaluators;
  if (handoff.dataset) return { ...seeded, source: "dataset", dataset: handoff.dataset };
  return seeded;
}

function Option({ on, disabled, title, desc, badge, onClick }: {
  on: boolean;
  disabled?: boolean;
  title: ReactNode;
  desc: ReactNode;
  badge?: ReactNode;
  onClick: () => void;
}) {
  return (
    <button type="button" role="radio" aria-checked={on} disabled={disabled}
      className={on ? "v3-scenario on v3-task-opt" : "v3-scenario v3-task-opt"} onClick={onClick}>
      <b>{title}</b>
      <small>{desc}</small>
      {badge && <span className="tags">{badge}</span>}
    </button>
  );
}

function Field({ label, hint, children }: { label: ReactNode; hint?: ReactNode; children: ReactNode }) {
  return (
    <label className="v3-field">
      <span>{label}</span>
      {children}
      {hint && <small className="v3-hint">{hint}</small>}
    </label>
  );
}

export function TaskWizard() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const toast = useToast();
  const from = params.get("from");
  const handoff: Handoff = {
    agent: params.get("agent"),
    dataset: params.get("dataset"),
    evaluators: (params.get("evaluators") ?? "").split(",").map((s) => s.trim()).filter(Boolean),
  };
  const seed = useLoad(
    () => seedDraft(from, handoff, t("v2.tasks.copySuffix")),
    `v3-seed:${from}:${handoff.agent}:${handoff.dataset}:${handoff.evaluators.join(",")}`,
  );
  const agents = useLoad(() => api.listAgents(), "v3-wiz-agents");
  const datasets = useLoad(() => api.v2Datasets(), "v3-wiz-datasets");
  const evaluators = useLoad(() => api.v2Evaluators(), "v3-wiz-evaluators");
  const [draft, setDraft] = useState<Draft | null>(null);
  const [step, setStep] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [evalLevel, setEvalLevel] = useState<"all" | EvaluatorLevel>("all");
  const [evalQ, setEvalQ] = useState("");

  useEffect(() => {
    if (seed.data && draft === null) setDraft(seed.data);
  }, [seed.data, draft]);

  const activeAgents = useMemo(() => (agents.data?.agents ?? []).filter((a) => a.status === "active"), [agents.data]);
  const agent = activeAgents.find((a) => a.id === draft?.agentId) ?? null;
  const sessions = useLoad(
    () => (draft && (draft.source === "sessions" || draft.source === "window") && agent ? api.obsSessions(SESSION_RANGE) : Promise.resolve(null)),
    `v3-wiz-sessions:${draft?.source}:${agent?.id ?? ""}`,
  );
  const agentSessions = useMemo(
    () => (sessions.data?.sessions ?? []).filter((s) => agent && s.agent === agent.name),
    [sessions.data, agent],
  );
  const cwService = draft?.serviceName.trim() ?? "";
  const cwReady = !!draft && draft.target === "cloudwatch" && SERVICE_NAME_RE.test(cwService) && draft.logGroups.length > 0;
  const cwWindow = useLoad(
    () => (cwReady && draft?.source === "window" ? api.v2LogSessions(cwService, draft.logGroups, draft.lookbackHours) : Promise.resolve(null)),
    `v3-cw-window:${cwReady}:${draft?.source}:${cwService}:${draft?.logGroups.join("|")}:${draft?.lookbackHours}`,
  );
  // pass^k replays dataset scenarios against an agent; it has no meaning for past sessions
  const repeatable =
    !!draft && draft.target === "agent" && draft.source === "dataset" && draft.mode === "evaluators" &&
    draft.strategy === "history" && !!draft.dataset && !draft.dataset.startsWith(CLOUD_VALUE_PREFIX);
  const estimate = useLoad(
    () =>
      repeatable && draft?.agentId
        ? dlcApi.estimate({ agent_id: draft.agentId, dataset_id: draft.dataset, evaluators: draft.evaluators, repeats: draft.repeats })
        : Promise.resolve(null),
    `v3-task-estimate:${repeatable}:${draft?.agentId}:${draft?.dataset}:${draft?.repeats}:${draft?.evaluators.join(",")}`,
  );
  if (seed.error) return <Notice s="act">{seed.error}</Notice>;
  if (!draft) return <Skeleton rows={6} />;
  const set = (patch: Partial<Draft>) => setDraft({ ...draft, ...patch });

  const localDatasets = (datasets.data?.datasets ?? []).filter((d) => d.kind !== "simulated");
  const selectedDataset = localDatasets.find((d) => d.id === draft.dataset) ?? null;
  const windowSessions = agentSessions.filter(
    (s) => !s.last || Date.now() - new Date(s.last).getTime() <= draft.lookbackHours * 3_600_000,
  );
  const allEvaluators = evaluators.data?.evaluators ?? [];
  const evalRows = allEvaluators.filter((e) => {
    if (evalLevel !== "all" && e.level !== evalLevel) return false;
    const needle = evalQ.trim().toLowerCase();
    return !needle || `${e.id} ${e.name ?? ""} ${evaluatorLabel(t, e.id)}`.toLowerCase().includes(needle);
  });

  const validateStep0 = (): string | null => {
    if (!draft.name.trim()) return t("v2.tasks.errName");
    if (draft.target === "agent" && !draft.agentId) return t("v2.tasks.errAgent");
    if (draft.target === "cloudwatch" && !SERVICE_NAME_RE.test(draft.serviceName.trim())) return t("v2.tasks.cw.errService");
    if (draft.target === "cloudwatch" && draft.logGroups.length === 0) return t("v2.tasks.cw.errGroups");
    if (draft.source === "sessions" && draft.sessionIds.length === 0) return t("v2.tasks.errSessions");
    if (draft.source === "dataset" && !draft.dataset) return t("v2.tasks.errDataset");
    if (draft.source === "logs" && draft.logSessionIds.length === 0) return t("v2.tasks.errLogs");
    const picked = draft.source === "sessions" ? draft.sessionIds.length : draft.source === "logs" ? draft.logSessionIds.length : null;
    if (draft.mode === "insights" && picked !== null && picked < MIN_INSIGHT_SESSIONS) {
      return t("v2.tasks.errInsightSessions", { min: MIN_INSIGHT_SESSIONS });
    }
    return null;
  };
  const next = () => {
    const problem = validateStep0();
    setError(problem);
    if (!problem) setStep(1);
  };

  const submit = async () => {
    const insights = draft.mode === "insights";
    if (insights && draft.insights.length === 0) return setError(t("v2.tasks.errInsights"));
    if (!insights && draft.evaluators.length === 0) return setError(t("v2.tasks.errEvaluators"));
    if (!insights && draft.evaluators.length > MAX_EVALUATORS) return setError(t("v2.tasks.errTooMany", { max: MAX_EVALUATORS }));
    setSubmitting(true);
    setError(null);
    try {
      if (draft.strategy === "continuous" && !insights) {
        const cfg = await api.v2CreateOnlineConfig({
          agent_id: draft.agentId,
          mode: "scores",
          evaluators: draft.evaluators,
          sampling_percentage: draft.sampling,
          session_timeout_minutes: draft.sessionTimeout,
          filters: [],
          description: draft.name.trim().slice(0, 200),
          enable_on_create: true,
        });
        toast("ok", t("v2.tasks.createdOnline"));
        navigate(detailUrl({ kind: "online", id: cfg.config_id }));
      } else {
        const scope =
          draft.source === "window"
            ? { lookback_hours: draft.lookbackHours }
            : draft.source === "sessions"
              ? { session_ids: draft.sessionIds }
              : draft.source === "logs"
                ? { session_ids: draft.logSessionIds, session_source: "logs" as const }
                : draft.dataset.startsWith(CLOUD_VALUE_PREFIX)
                  ? { cloud_dataset_id: draft.dataset.slice(CLOUD_VALUE_PREFIX.length) }
                  : { dataset_id: draft.dataset };
        const run = await api.v2CreateRun({
          ...(draft.target === "cloudwatch"
            ? { log_source: { service_name: draft.serviceName.trim(), log_group_names: draft.logGroups } }
            : { agent_id: draft.agentId }),
          name: draft.name.trim(),
          description: draft.description || undefined,
          ...(insights ? { mode: "insights" as const, evaluators: [], insights: draft.insights } : { evaluators: draft.evaluators }),
          ...scope,
          ...(repeatable ? { repeats: draft.repeats, confirm_cost: draft.confirmCost } : {}),
        });
        toast("ok", t("v2.tasks.createdRun"));
        navigate(detailUrl({ kind: "run", id: run.id }));
      }
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setSubmitting(false);
    }
  };

  const toggleEvaluator = (id: string) =>
    set({ evaluators: draft.evaluators.includes(id) ? draft.evaluators.filter((e) => e !== id) : [...draft.evaluators, id] });
  // without a platform agent there is nothing to invoke (dataset) and no
  // agent-attributed trajectory list (sessions): only passive CloudWatch scopes
  const cloudwatch = draft.target === "cloudwatch";
  const sourceCards: { key: Source; disabled?: boolean }[] = [
    { key: "window" },
    { key: "sessions", disabled: cloudwatch },
    { key: "logs" },
    { key: "dataset", disabled: cloudwatch },
  ];
  const chooseTarget = (target: Target) =>
    set({
      target,
      source: target === "cloudwatch" && (draft.source === "sessions" || draft.source === "dataset") ? "window" : draft.source,
      strategy: target === "cloudwatch" ? "history" : draft.strategy,
      logSessionIds: [],
    });
  const chooseSource = (source: Source) => set({ source, strategy: source === "window" ? draft.strategy : "history" });
  // continuous insights are an online-evaluation config with a report schedule — not a task
  const chooseMode = (mode: TaskMode) => set({ mode, strategy: mode === "insights" ? "history" : draft.strategy });
  const toggleInsight = (id: string) =>
    set({ insights: INSIGHT_TYPES.filter((x) => (x === id ? !draft.insights.includes(id) : draft.insights.includes(x))) });
  const fewSessions =
    draft.mode === "insights" &&
    ((draft.source === "window" && !sessions.loading && windowSessions.length < MIN_INSIGHT_SESSIONS) ||
      (draft.source === "dataset" && !!selectedDataset && selectedDataset.item_count < MIN_INSIGHT_SESSIONS) ||
      (cloudwatch && draft.source === "window" && !!cwWindow.data && cwWindow.data.streams.length < MIN_INSIGHT_SESSIONS));
  const stepLabels = [t("v2.tasks.stepData"), draft.mode === "insights" ? t("v2.tasks.stepInsights") : t("v2.tasks.stepEvaluators")];

  return (
    <div className="v3-reveal v3-task" style={{ display: "grid", gap: 16 }}>
      <div>
        <button type="button" className="v3-btn ghost sm" onClick={() => navigate("/v3/tasks")}>
          <ArrowLeft size={14} /> {t("v3.tasks.title")}
        </button>
      </div>
      <PageHead
        eyebrow={t("v3.tasks.eyebrow")}
        title={t("v2.tasks.newTitle")}
        sub={t("v3.tasks.newSub")}
        end={
          <div className="v3-task-steps" role="tablist">
            {stepLabels.map((label, i) => (
              <button key={label} type="button" role="tab" aria-selected={step === i} className={step === i ? "on" : i < step ? "done" : ""}
                onClick={() => (i === 0 ? setStep(0) : next())}>
                <span className="num">{i + 1}</span>
                {label}
              </button>
            ))}
          </div>
        }
      />
      {error && <Notice s="act">{error}</Notice>}

      {step === 0 && (
        <>
          <Panel title={t("v2.tasks.basic")}>
            <div style={{ display: "grid", gap: 16 }}>
              <div className="v3-field">
                <span>{t("v2.tasks.cw.target")}</span>
                <div className="v3-task-opts c2" role="radiogroup">
                  <Option on={!cloudwatch} title={t("v2.tasks.cw.targetAgent")} desc={t("v2.tasks.cw.targetAgentDesc")} onClick={() => chooseTarget("agent")} />
                  <Option on={cloudwatch} title={t("v2.tasks.cw.targetCw")} desc={t("v2.tasks.cw.targetCwDesc")} onClick={() => chooseTarget("cloudwatch")} />
                </div>
              </div>
              <div className="v3-grid c2" style={{ alignItems: "start" }}>
                <Field label={`${t("v2.tasks.colName")} *`}>
                  <input className="v3-input" value={draft.name} maxLength={64} onChange={(e) => set({ name: e.target.value })} />
                </Field>
                {!cloudwatch && (
                  <Field label={`${t("v2.tasks.colAgent")} *`} hint={agents.loading ? undefined : activeAgents.length === 0 ? t("v2.tasks.noAgents") : undefined}>
                    <select className="v3-select" value={draft.agentId} onChange={(e) => set({ agentId: e.target.value, sessionIds: [], logSessionIds: [] })}>
                      <option value="">{t("v2.common.choose")}</option>
                      {activeAgents.map((a) => <option key={a.id} value={a.id}>{a.name}</option>)}
                    </select>
                  </Field>
                )}
              </div>
              <Field label={t("v2.tasks.description")}>
                <textarea className="v3-input" rows={2} value={draft.description} maxLength={1000} placeholder={t("v2.tasks.descPlaceholder")}
                  onChange={(e) => set({ description: e.target.value })} style={{ minHeight: 64 }} />
              </Field>
              <div className="v3-field">
                <span>{t("v2.tasks.mode")}</span>
                <div className="v3-task-opts c2" role="radiogroup">
                  <Option on={draft.mode === "evaluators"} title={t("v2.tasks.modeEvaluators")} desc={t("v2.tasks.modeEvaluatorsDesc")} onClick={() => chooseMode("evaluators")} />
                  <Option on={draft.mode === "insights"} title={t("v2.tasks.modeInsights")} desc={t("v2.tasks.modeInsightsDesc")} onClick={() => chooseMode("insights")} />
                </div>
              </div>
            </div>
          </Panel>

          {cloudwatch && (
            <Panel title={t("v2.tasks.cw.title")} end={<span>{t("v2.tasks.cw.sub")}</span>}>
              <HostedV2>
                <LogSourceFields
                  serviceName={draft.serviceName}
                  logGroups={draft.logGroups}
                  onChange={(patch) => set({ ...patch, logSessionIds: [] })}
                  onUseAgent={(agentId) => set({ target: "agent", agentId, logSessionIds: [] })}
                />
              </HostedV2>
            </Panel>
          )}

          <Panel title={t("v2.tasks.dataConfig")}>
            <div className="v3-task-opts c4" role="radiogroup">
              {sourceCards.map(({ key, disabled }) => (
                <Option key={key} on={draft.source === key} disabled={disabled} title={t(`v2.tasks.src.${key}`)} desc={t(`v2.tasks.src.${key}Desc`)}
                  badge={disabled ? <Chip>{t("v2.tasks.cw.needsAgent")}</Chip> : undefined} onClick={() => chooseSource(key)} />
              ))}
            </div>
            <div className="v3-grid c2" style={{ marginTop: 20, alignItems: "start" }}>
              <div className="v3-field">
                <span>{t("v2.tasks.strategy")}</span>
                <div className="v3-task-opts c2" role="radiogroup">
                  <Option on={draft.strategy === "history"} title={t("v2.tasks.strategyHistory")}
                    desc={t(draft.source === "dataset" ? "v2.tasks.strategyReplayDesc" : "v2.tasks.strategyHistoryDesc")} onClick={() => set({ strategy: "history" })} />
                  <Option on={draft.strategy === "continuous"} disabled={draft.source !== "window" || draft.mode === "insights" || cloudwatch}
                    title={t("v2.tasks.strategyContinuous")} desc={t("v2.tasks.strategyContinuousDesc")} onClick={() => set({ strategy: "continuous" })} />
                </div>
                {draft.mode === "insights" && draft.source === "window" && <small className="v3-hint">{t("v2.tasks.insightsNoContinuous")}</small>}
              </div>
              <div className="v3-field">
                <span>{draft.strategy === "continuous" ? t("v2.tasks.sampling") : t("v2.tasks.scope")}</span>
                {draft.strategy === "continuous" ? (
                  <div className="v3-grid c2" style={{ alignItems: "start" }}>
                    <Field label={t("v2.tasks.samplingRate")} hint={t("v2.tasks.samplingHint")}>
                      <input className="v3-input" type="number" min={0.01} max={100} step={1} value={draft.sampling}
                        onChange={(e) => set({ sampling: Math.max(0.01, Math.min(100, Number(e.target.value) || 10)) })} />
                    </Field>
                    <Field label={t("v2.tasks.sessionTimeout")} hint={t("v2.tasks.sessionTimeoutHint")}>
                      <input className="v3-input" type="number" min={1} max={1440} value={draft.sessionTimeout}
                        onChange={(e) => set({ sessionTimeout: Math.max(1, Math.min(1440, Number(e.target.value) || 15)) })} />
                    </Field>
                  </div>
                ) : draft.source === "window" || draft.source === "logs" ? (
                  <Field label={draft.source === "logs" ? t("v2.tasks.logs.window") : t("v2.tasks.lookback")}
                    hint={draft.source === "logs" ? t("v2.tasks.logs.picked", { count: draft.logSessionIds.length }) : t("v2.tasks.lookbackHint")}>
                    <select className="v3-select" value={String(draft.source === "logs" ? draft.logHours : draft.lookbackHours)}
                      onChange={(e) => set(draft.source === "logs" ? { logHours: Number(e.target.value) } : { lookbackHours: Number(e.target.value) })}>
                      {LOOKBACKS.map((h) => <option key={h} value={String(h)}>{t("v2.tasks.hours", { count: h })}</option>)}
                    </select>
                  </Field>
                ) : draft.source === "dataset" ? (
                  <Field label={`${t("v2.tasks.dataset")} *`} hint={selectedDataset && !selectedDataset.has_ground_truth ? t("v2.tasks.noGroundTruth") : undefined}>
                    <select className="v3-select" value={draft.dataset} onChange={(e) => set({ dataset: e.target.value })}>
                      <option value="">{t("v2.common.choose")}</option>
                      {draft.dataset && !selectedDataset && <option value={draft.dataset}>{draft.dataset}</option>}
                      {localDatasets.map((d) => <option key={d.id} value={d.id}>{`${d.name} · ${t("v2.datasets.items", { count: d.item_count })}`}</option>)}
                    </select>
                  </Field>
                ) : (
                  <small className="v3-hint">{t("v2.tasks.sessionsPicked", { count: draft.sessionIds.length })}</small>
                )}
              </div>
            </div>
          </Panel>

          <Panel
            title={draft.source === "logs" ? t("v2.tasks.logs.title") : t("v2.tasks.preview")}
            end={<span>{draft.source === "logs" ? t(cloudwatch ? "v2.tasks.cw.logsSub" : "v2.tasks.logs.sub") : t("v2.tasks.previewSub")}</span>}
            flush
          >
            <div style={{ padding: "0 20px 16px" }}>
              {cloudwatch && !cwReady ? (
                <p className="v3-task-muted">{t("v2.tasks.cw.previewPick")}</p>
              ) : cloudwatch && draft.source === "window" ? (
                <>
                  <Notice>
                    {cwWindow.data
                      ? t(cwWindow.data.truncated ? "v2.tasks.cw.previewWindowMore" : "v2.tasks.cw.previewWindow", {
                          count: cwWindow.data.streams.length, hours: draft.lookbackHours, service: cwService,
                        })
                      : t("v2.common.loading")}
                  </Notice>
                  {cwWindow.error ? <Notice s="act">{cwWindow.error}</Notice> : (cwWindow.data?.streams.length ?? 0) > 0 && (
                    <table className="v3-table" style={{ marginTop: 10 }}>
                      <thead><tr><th>{t("v2.traces.colSession")}</th><th className="num">{t("v2.pipelines.traceCount")}</th><th className="num">{t("v2.pipelines.lastActive")}</th></tr></thead>
                      <tbody>
                        {(cwWindow.data?.streams ?? []).slice(0, 8).map((r) => (
                          <tr key={r.session_id ?? r.stream}><td className="mono">{r.session_id}</td><td className="num">{r.traces ?? "—"}</td><td className="num">{ago(r.last_event)}</td></tr>
                        ))}
                      </tbody>
                    </table>
                  )}
                </>
              ) : !cloudwatch && !agent ? (
                <p className="v3-task-muted">{t("v2.tasks.previewPickAgent")}</p>
              ) : draft.source === "logs" ? (
                <>
                  <HostedV2>
                    {cloudwatch ? (
                      <LogStreamPicker
                        load={(hours, q) => api.v2LogSessions(cwService, draft.logGroups, hours, q)}
                        loadKey={`cw:${cwService}:${draft.logGroups.join("|")}`}
                        hint={t("v2.tasks.cw.keywordHint")}
                        hours={draft.logHours}
                        selected={draft.logSessionIds}
                        onChange={(logSessionIds) => set({ logSessionIds })}
                      />
                    ) : (
                      <LogStreamPicker
                        load={(hours, q) => api.v2AgentLogStreams(agent?.id ?? "", hours, q)}
                        loadKey={`agent:${agent?.id}`}
                        hint={t("v2.tasks.logs.keywordHint")}
                        hours={draft.logHours}
                        selected={draft.logSessionIds}
                        onChange={(logSessionIds) => set({ logSessionIds })}
                      />
                    )}
                  </HostedV2>
                  {draft.logSessionIds.length > 0 && <div style={{ marginTop: 12 }}><Notice>{t("v2.tasks.logs.hintSelected")}</Notice></div>}
                </>
              ) : draft.source === "dataset" ? (
                selectedDataset ? (
                  <table className="v3-table">
                    <thead><tr><th style={{ width: 48 }}>#</th><th>Input</th><th>{t("v2.datasets.expected")}</th></tr></thead>
                    <tbody>
                      {selectedDataset.items.slice(0, 5).map((item, i) => {
                        const turns = item.turns as { input?: unknown; expected_response?: unknown }[] | undefined;
                        const input = String(turns?.[0]?.input ?? item.prompt ?? item.input ?? "");
                        const expected = String(turns?.[0]?.expected_response ?? item.expected ?? "");
                        return (
                          <tr key={i}>
                            <td className="num">{i + 1}</td>
                            <td><span className="v3-task-clip2">{input}</span></td>
                            <td style={{ color: "var(--v3-text-2)" }}><span className="v3-task-clip2">{expected || "—"}</span></td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                ) : (
                  <p className="v3-task-muted">{t("v2.tasks.previewPickDataset")}</p>
                )
              ) : draft.strategy === "continuous" ? (
                <Notice>{t("v2.tasks.previewContinuous", { rate: draft.sampling })}</Notice>
              ) : (
                <>
                  {draft.source === "window" && <Notice>{t("v2.tasks.previewWindow", { count: windowSessions.length, hours: draft.lookbackHours })}</Notice>}
                  {sessions.loading ? (
                    <Skeleton rows={3} />
                  ) : sessions.error ? (
                    <Notice s="act">{sessions.error}</Notice>
                  ) : (draft.source === "window" ? windowSessions : agentSessions).length === 0 ? (
                    <Empty title={t("v2.tasks.previewNoSessions")} />
                  ) : (
                    <table className="v3-table" style={{ marginTop: 10 }}>
                      <thead>
                        <tr>
                          {draft.source === "sessions" && <th style={{ width: 36 }} />}
                          <th>{t("v2.traces.colSession")}</th>
                          <th className="num">{t("v2.pipelines.traceCount")}</th>
                          <th>{t("v2.traces.colStatus")}</th>
                          <th className="num">{t("v2.pipelines.lastActive")}</th>
                        </tr>
                      </thead>
                      <tbody>
                        {(draft.source === "window" ? windowSessions : agentSessions).slice(0, draft.source === "sessions" ? 50 : 8).map((s) => (
                          <tr key={s.session_id}>
                            {draft.source === "sessions" && (
                              <td>
                                <input type="checkbox" aria-label={s.session_id} checked={draft.sessionIds.includes(s.session_id)}
                                  onChange={() => set({
                                    sessionIds: draft.sessionIds.includes(s.session_id)
                                      ? draft.sessionIds.filter((id) => id !== s.session_id)
                                      : [...draft.sessionIds, s.session_id],
                                  })} />
                              </td>
                            )}
                            <td className="mono">{s.session_id}</td>
                            <td className="num">{s.traces}</td>
                            <td>{s.errors ? <Chip s="act">{t("v2.traces.error", { count: s.errors })}</Chip> : <Chip s="ok">{t("v2.traces.ok")}</Chip>}</td>
                            <td className="num" style={{ color: "var(--v3-text-3)" }}>{ago(s.last)}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  )}
                </>
              )}
            </div>
          </Panel>
        </>
      )}

      {step === 1 && draft.mode === "insights" && (
        <Panel title={t("v2.tasks.pickInsights")} end={<span>{t("v2.tasks.pickedCount", { count: draft.insights.length, max: INSIGHT_TYPES.length })}</span>}>
          <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
            {INSIGHT_TYPES.map((id) => {
              const on = draft.insights.includes(id);
              return (
                <button key={id} type="button" aria-pressed={on} className={`v3-btn sm${on ? "" : " ghost"}`} onClick={() => toggleInsight(id)}>
                  {insightLabel(t, id)}
                </button>
              );
            })}
          </div>
          <div style={{ display: "grid", gap: 10, marginTop: 14 }}>
            <Notice>{draft.source === "dataset" ? t("evalPage.newRun.insightsHint") : t("evalPage.newRun.insightsWindowHint")}</Notice>
            {fewSessions && <Notice s="wait">{t("v2.tasks.insightsFewSessions", { min: MIN_INSIGHT_SESSIONS })}</Notice>}
          </div>
        </Panel>
      )}

      {step === 1 && repeatable && (
        <Panel title={t("v2.tasks.passK")} end={<span>{t("v2.tasks.passKSub")}</span>} signal={estimate.data?.over_limit ? "act" : estimate.data?.confirm_required ? "wait" : undefined}>
          <div className="v3-grid c2" style={{ alignItems: "start" }}>
            <Field label={t("v2.tasks.repeats")} hint={t("v2.tasks.repeatsHint")}>
              <input className="v3-input" type="number" min={1} max={10} value={draft.repeats}
                onChange={(e) => set({ repeats: Math.max(1, Math.min(10, Number(e.target.value) || 1)), confirmCost: false })} />
            </Field>
            <div className="v3-field">
              <span>{t("v2.dlc.cost.estimate")}</span>
              {estimate.loading ? (
                <Skeleton rows={1} />
              ) : estimate.data ? (
                <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
                  <b className="mono" style={{ fontSize: 20 }}>{estimate.data.total_usd === null ? t("v2.dlc.cost.unknown") : `$${estimate.data.total_usd.toFixed(2)}`}</b>
                  <span style={{ color: "var(--v3-text-2)" }}>{t("v2.dlc.cost.sessions", { sessions: estimate.data.sessions, minutes: estimate.data.duration_minutes ?? 0 })}</span>
                  {estimate.data.unpriced && <Chip s="wait">{t("v2.dlc.cost.unpriced")}</Chip>}
                  {unpricedJudges(estimate.data).length > 0 && (
                    <Chip s="wait">{t("v2.dlc.cost.judgeUnpriced", { count: unpricedJudges(estimate.data).length })}</Chip>
                  )}
                  {estimate.data.over_limit && <Chip s="act">{t("v2.dlc.cost.overLimit")}</Chip>}
                </div>
              ) : (
                <span>—</span>
              )}
              <small className="v3-hint">{t("v2.dlc.cost.basisHint")}</small>
            </div>
          </div>
          {draft.repeats > 1 && <div style={{ marginTop: 12 }}><Notice s="wait">{t("v2.dlc.release.passKCost", { k: draft.repeats })}</Notice></div>}
          {estimate.data?.confirm_required && (
            <label className="v3-task-check" style={{ marginTop: 12 }}>
              <input type="checkbox" checked={draft.confirmCost} onChange={(e) => set({ confirmCost: e.target.checked })} />
              {t("v2.tasks.confirmCost", { usd: estimate.data.total_usd ?? "—" })}
            </label>
          )}
        </Panel>
      )}

      {step === 1 && draft.mode === "evaluators" && (
        <Panel title={t("v2.tasks.pickEvaluators")} flush end={<span>{t("v2.tasks.pickedCount", { count: draft.evaluators.length, max: MAX_EVALUATORS })}</span>}>
          <div style={{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap", padding: "0 20px 12px" }}>
            <Filters
              value={evalLevel}
              onChange={setEvalLevel}
              options={[
                { value: "all", label: t("v3.tasks.anyLevel") },
                ...(["SESSION", "TRACE", "TOOL_CALL"] as EvaluatorLevel[]).map((l) => ({ value: l, label: t(`v2.level.${l}`) })),
              ]}
            />
            <div style={{ marginLeft: "auto", position: "relative", flex: "1 1 200px", maxWidth: 280 }}>
              <Search size={14} style={{ position: "absolute", left: 12, top: 11, color: "var(--v3-text-3)" }} aria-hidden="true" />
              <input className="v3-input" style={{ paddingLeft: 34 }} value={evalQ} onChange={(e) => setEvalQ(e.target.value)}
                placeholder={t("v2.evaluators.search")} aria-label={t("v2.evaluators.search")} />
            </div>
          </div>
          {draft.evaluators.length > 0 && (
            <div style={{ display: "flex", gap: 6, flexWrap: "wrap", padding: "0 20px 12px" }}>
              {draft.evaluators.map((id) => <Chip key={id} s="info">{evaluatorLabel(t, id)}</Chip>)}
            </div>
          )}
          {evaluators.loading && !evaluators.data ? (
            <div style={{ padding: 20 }}><Skeleton rows={4} /></div>
          ) : evaluators.error ? (
            <div style={{ padding: "0 20px 16px" }}><Notice s="act">{evaluators.error}</Notice></div>
          ) : (
            <table className="v3-table">
              <thead>
                <tr>
                  <th style={{ width: 36 }} />
                  <th>{t("v2.evaluators.colName")}</th>
                  <th>{t("v2.evaluators.colSource")}</th>
                  <th>{t("v2.evaluators.colLevel")}</th>
                  <th>{t("v2.evaluators.colGroundTruth")}</th>
                </tr>
              </thead>
              <tbody>
                {evalRows.map((e) => {
                  const on = draft.evaluators.includes(e.id);
                  const locked = !on && draft.evaluators.length >= MAX_EVALUATORS;
                  return (
                    <tr key={e.id} className={locked ? undefined : "click"} onClick={() => !locked && toggleEvaluator(e.id)}>
                      <td><input type="checkbox" aria-label={e.id} checked={on} disabled={locked} onChange={() => toggleEvaluator(e.id)} onClick={(ev) => ev.stopPropagation()} /></td>
                      <td>
                        <div className="v3-name"><div><b>{e.source === "custom" ? (e.name ?? e.id) : evaluatorLabel(t, e.id)}</b><small>{e.id}</small></div></div>
                      </td>
                      <td style={{ color: "var(--v3-text-2)" }}>{t(`v2.evaluators.source.${e.source}`)}</td>
                      <td style={{ color: "var(--v3-text-2)" }}>{t(`v2.level.${e.level}`, { defaultValue: e.level })}</td>
                      <td>
                        {e.requires_ground_truth ? (
                          <Chip s={draft.source === "dataset" && selectedDataset?.has_ground_truth ? "ok" : "wait"}>{t("v2.evaluators.needsGt")}</Chip>
                        ) : (
                          <span style={{ color: "var(--v3-text-3)" }}>{t("v2.common.no")}</span>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
          {draft.evaluators.some((id) => allEvaluators.find((e) => e.id === id)?.requires_ground_truth) &&
            !(draft.source === "dataset" && selectedDataset?.has_ground_truth) && (
              <div style={{ padding: "12px 20px 16px" }}><Notice s="wait">{t("v2.tasks.gtWarning")}</Notice></div>
            )}
        </Panel>
      )}

      <div className="v3-task-foot">
        <Btn kind="ghost" disabled={step === 0} onClick={() => setStep(0)}><ArrowLeft size={14} /> {t("v2.common.prev")}</Btn>
        {step === 0 ? (
          <Btn kind="primary" onClick={next}>{t("v2.common.next")} <ArrowRight size={14} /></Btn>
        ) : (
          <Btn kind="primary" disabled={submitting} onClick={() => void submit()}>
            <Rocket size={14} /> {draft.strategy === "continuous" ? t("v2.tasks.submitOnline") : t("v2.tasks.submitRun")}
          </Btn>
        )}
      </div>
    </div>
  );
}
