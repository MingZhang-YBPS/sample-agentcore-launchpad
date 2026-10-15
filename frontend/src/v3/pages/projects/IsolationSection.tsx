import { ArrowLeftRight, RotateCcw, ShieldCheck, Undo2, Unplug } from "lucide-react";
import { type CSSProperties, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import { ApiErrorDetail } from "../../../components/ApiErrorDetail";
import {
  type IsolationMode,
  type IsolationWorkspace,
  type MigrationAgent,
  type MigrationRun,
  type ProjectDetail,
  type ProjectIsolation,
  type ProjectMigration,
  projectIsolationApi,
} from "../../../lib/api/projects";
import { useLoad, useToast } from "../../hooks";
import { Reasoned } from "../../reason";
import { firstReason } from "../../reasons";
import { Btn, Chip, Confirm, Lamp, Notice, Panel, Skeleton } from "../../ui";
import "./isolation.css";
import {
  AGENT_STEPS,
  canMigrate,
  canRetire,
  canRetry,
  canRevert,
  isBusy,
  migrationOf,
  migrationSignal,
  provisionSignal,
  quotaMeter,
  runProgress,
  stepSignal,
} from "./isolationModel";
import type { ProjectAbilities } from "./ProjectDetail";

const POLL_MS = 4000;

type Pending = { kind: "enable" } | { kind: "revert" } | { kind: "retire" } | null;

/**
 * §10.14 "Runtime isolation": the project's mode, then one bay per bound workspace —
 * its provisioning state, resources and quota preflight, and the migration board of
 * its agents (grant-new → republish → verify → switch). Every member reads it; an
 * administrator switches isolation on, migrates, retries, reverts (before retire)
 * and retires the old shared grants. Polls while something moves.
 */
export function IsolationSection({ project, abilities }: { project: ProjectDetail; abilities: ProjectAbilities }) {
  const { t } = useTranslation();
  const toast = useToast();
  const [tick, setTick] = useState(0);
  const iso = useLoad(() => projectIsolationApi.get(project.id), `v3-project-isolation:${project.id}:${tick}`);
  const mig = useLoad(() => projectIsolationApi.migration(project.id), `v3-project-migration:${project.id}:${tick}`);
  const [pending, setPending] = useState<Pending>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const busyNow = isBusy(iso.data, mig.data);

  useEffect(() => {
    if (!busyNow) return;
    const id = window.setInterval(() => setTick((n) => n + 1), POLL_MS);
    return () => window.clearInterval(id);
  }, [busyNow]);

  const view = iso.data;
  if (iso.loading && !view) return <Panel title={t("v3.projects.isolation.title")}><Skeleton rows={4} /></Panel>;
  if (!view) {
    return (
      <Panel title={t("v3.projects.isolation.title")}>
        <Notice s="act"><ApiErrorDetail error={iso.failure ?? iso.error} /></Notice>
      </Panel>
    );
  }
  const migration = mig.data;
  const can = abilities.admin && project.status === "active";

  const act = async (fn: () => Promise<unknown>, ok: string) => {
    setBusy(true);
    setError(null);
    try {
      await fn();
      toast("ok", ok);
      setPending(null);
      setTick((n) => n + 1);
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  };
  // why a mode position is off, in the order it would have to be fixed
  const modeLock = (mode: IsolationMode) =>
    firstReason(
      project.status !== "active" && t("story.reason.projectArchived"),
      !abilities.admin && t("v3.projects.isolation.readOnly"),
      mode === "shared" && view.legacy_retired && t("story.reason.sharedRetired"),
      mode === "shared" && view.mode === "shared" && t("story.reason.currentMode"),
      mode === "shared" && isBusy(view, migration) && t("story.reason.waitRunning"),
    );
  const setMode = (mode: IsolationMode) => {
    if (mode === view.mode) return;
    if (mode === "isolated") setPending({ kind: "enable" });
    else setPending({ kind: "revert" });
  };

  return (
    <Panel
      title={t("v3.projects.isolation.title")}
      signal={view.workspaces.some((w) => w.state === "failed") ? "act" : undefined}
      end={busyNow ? <span style={{ display: "inline-flex", gap: 6, alignItems: "center" }}><Lamp s="info" live /> {t("v3.projects.isolation.live")}</span> : undefined}
    >
      <div className="v3-iso" data-testid="project-isolation">
        <small className="v3-hint">{t("v3.projects.isolation.sub")}</small>
        <div className="v3-iso-mode">
          <div className="v3-iso-switch" data-mode={view.mode} role="group" aria-label={t("v3.projects.isolation.mode")}>
            <span className="thumb" aria-hidden="true" />
            {(["shared", "isolated"] as const).map((mode) => (
              <Reasoned key={mode} disabledReason={modeLock(mode)}>
                <button
                  type="button"
                  aria-pressed={view.mode === mode}
                  disabled={!can || busy || (mode === "shared" && view.legacy_retired) || (mode === "shared" && !canRevert(view, migration))}
                  onClick={() => setMode(mode)}
                  data-testid={`project-isolation-mode-${mode}`}
                >
                  <b>{t(`v3.projects.isolation.modes.${mode}`)}</b>
                  <small>{t(`v3.projects.isolation.modes.${mode}Hint`)}</small>
                </button>
              </Reasoned>
            ))}
          </div>
          {view.legacy_retired && <span className="v3-iso-retired">{t("v3.projects.isolation.retired")}</span>}
          {view.updated_by && (
            <small className="v3-hint">{t("v3.projects.isolation.updatedBy", { who: view.updated_by })}</small>
          )}
        </div>

        {view.workspaces.length === 0 && <small className="v3-hint">{t("v3.projects.isolation.noWorkspaces")}</small>}
        {view.workspaces.map((ws) => (
          <WorkspaceBay
            key={ws.workspace_id}
            project={project}
            iso={view}
            ws={ws}
            migration={migration}
            tick={tick}
            can={can}
            busy={busy}
            onProvision={() => void act(() => projectIsolationApi.provision(project.id, ws.workspace_id),
              t("v3.projects.isolation.provisionQueued", { ws: ws.workspace_id }))}
            onMigrate={() => void act(() => projectIsolationApi.migrate(project.id, "isolate", ws.workspace_id),
              t("v3.projects.isolation.migrateQueued", { ws: ws.workspace_id }))}
            onRetry={(run) => void act(() => projectIsolationApi.retry(project.id, run.id),
              t("v3.projects.isolation.retryQueued", { ws: ws.workspace_id }))}
          />
        ))}

        {error != null && <Notice s="act"><ApiErrorDetail error={error} /></Notice>}
        {can && view.mode === "isolated" && (
          <div className="v3-iso-foot">
            <span className="grow v3-hint">{t(view.legacy_retired ? "v3.projects.isolation.retiredHint" : "v3.projects.isolation.retireHint")}</span>
            {!view.legacy_retired && (
              <>
                <Btn kind="ghost" busy={busy} disabled={!canRevert(view, migration)} disabledReason={t("story.reason.waitRunning")} onClick={() => setPending({ kind: "revert" })}>
                  <Undo2 size={14} /> {t("v3.projects.isolation.revert")}
                </Btn>
                <Btn kind="danger" busy={busy} disabled={!canRetire(view, migration)}
                  disabledReason={isBusy(view, migration) ? t("story.reason.waitRunning") : t("story.reason.migrateEveryWorkspace")}
                  onClick={() => setPending({ kind: "retire" })}>
                  <Unplug size={14} /> {t("v3.projects.isolation.retire")}
                </Btn>
              </>
            )}
          </div>
        )}
        {!can && <small className="v3-hint">{t("v3.projects.isolation.readOnly")}</small>}
      </div>

      {pending?.kind === "enable" && (
        <Confirm
          title={t("v3.projects.isolation.enableTitle")}
          confirmLabel={t("v3.projects.isolation.enable")}
          cancelLabel={t("v3.projects.cancel")}
          busy={busy}
          onCancel={() => setPending(null)}
          onConfirm={() => void act(() => projectIsolationApi.put(project.id, "isolated"), t("v3.projects.isolation.enabled", { name: project.name }))}
        >
          {t("v3.projects.isolation.enableBody")}
        </Confirm>
      )}
      {pending?.kind === "revert" && (
        <Confirm
          title={t("v3.projects.isolation.revertTitle")}
          confirmLabel={t("v3.projects.isolation.revert")}
          cancelLabel={t("v3.projects.cancel")}
          danger
          busy={busy}
          onCancel={() => setPending(null)}
          onConfirm={() => void act(() => projectIsolationApi.migrate(project.id, "revert"), t("v3.projects.isolation.revertQueued"))}
        >
          {t("v3.projects.isolation.revertBody")}
        </Confirm>
      )}
      {pending?.kind === "retire" && (
        <Confirm
          title={t("v3.projects.isolation.retireTitle")}
          confirmLabel={t("v3.projects.isolation.retire")}
          cancelLabel={t("v3.projects.cancel")}
          danger
          busy={busy}
          onCancel={() => setPending(null)}
          onConfirm={() => void act(() => projectIsolationApi.retire(project.id), t("v3.projects.isolation.retireQueued"))}
        >
          {t("v3.projects.isolation.retireBody")}
        </Confirm>
      )}
    </Panel>
  );
}

function WorkspaceBay({
  project,
  iso,
  ws,
  migration,
  tick,
  can,
  busy,
  onProvision,
  onMigrate,
  onRetry,
}: {
  project: ProjectDetail;
  iso: ProjectIsolation;
  ws: IsolationWorkspace;
  migration: ProjectMigration | null;
  tick: number;
  can: boolean;
  busy: boolean;
  onProvision: () => void;
  onMigrate: () => void;
  onRetry: (run: MigrationRun) => void;
}) {
  const { t } = useTranslation();
  const prov = useLoad(
    () => projectIsolationApi.provisioning(project.id, ws.workspace_id),
    `v3-project-provisioning:${project.id}:${ws.workspace_id}:${tick}`,
  );
  const entry = migrationOf(migration, ws.workspace_id);
  const run = entry?.run ?? null;
  const preflight = run?.preflight.length ? run.preflight : prov.data?.job?.preflight ?? [];
  const bayState = ws.state === "failed" || entry?.state === "failed" ? "act" : undefined;
  const progress = run ? runProgress(run) : null;

  return (
    <section className="v3-iso-bay" data-s={bayState} data-testid={`project-isolation-ws-${ws.workspace_id}`}>
      <div className="v3-iso-bay-head">
        <span className="ws">{ws.workspace_id}</span>
        <span className="state">
          <Lamp s={provisionSignal(ws.state)} live={ws.state === "provisioning"} />
          {t(`v3.projects.isolation.provision.${ws.state}`)}
        </span>
        {entry && entry.state !== "not_started" && run && (
          <span className="state">
            <Lamp s={migrationSignal(entry.state)} live={entry.state === "running"} />
            {t(`v3.projects.isolation.run.${run.direction}`)} · {t(`v3.projects.isolation.state.${entry.state}`)}
            {progress && <span className="mono">{progress.done}/{progress.total}</span>}
          </span>
        )}
        {can && (
          <span className="end">
            {iso.mode === "isolated" && ws.state !== "provisioning" && (
              <Btn size="sm" kind="ghost" disabled={busy} onClick={onProvision}>
                <RotateCcw size={13} /> {t("v3.projects.isolation.provisionAgain")}
              </Btn>
            )}
            {canRetry(run) && run && (
              <Btn size="sm" disabled={busy} onClick={() => onRetry(run)}>
                <RotateCcw size={13} /> {t("v3.projects.isolation.retry")}
              </Btn>
            )}
            {canMigrate(iso, ws, migration) && !iso.legacy_retired && (
              <Btn size="sm" kind="primary" disabled={busy} onClick={onMigrate}>
                <ArrowLeftRight size={13} /> {t(run?.direction === "isolate" ? "v3.projects.isolation.migrateAgain" : "v3.projects.isolation.migrate")}
              </Btn>
            )}
          </span>
        )}
      </div>
      <div className="v3-iso-bay-body">
        {ws.last_error && ws.state === "failed" && <Notice s="act">{ws.last_error}</Notice>}
        {ws.skipped_stages.length > 0 && (
          <small className="v3-hint">{t("v3.projects.isolation.skipped", { stages: ws.skipped_stages.join(", ") })}</small>
        )}
        {Object.keys(ws.resources).length > 0 && (
          <div>
            <div className="v3-iso-label">{t("v3.projects.isolation.resources")}</div>
            <div className="v3-iso-res">
              {Object.entries(ws.resources).map(([kind, status]) => (
                <Chip key={kind} s={status === "ready" ? "ok" : status === "failed" ? "act" : "wait"}>
                  <span className="mono">{kind}</span>
                </Chip>
              ))}
            </div>
          </div>
        )}
        {preflight.length > 0 && (
          <div>
            <div className="v3-iso-label">{t("v3.projects.isolation.preflight")}</div>
            <div className="v3-iso-quotas">
              {preflight.map((f) => {
                const meter = quotaMeter(f);
                return (
                  <div className="v3-iso-quota" key={f.key} title={f.note || undefined}>
                    <div className="top">
                      <span>{f.label}</span>
                      <span className="num">
                        {meter ? `${f.used}+${f.needed} / ${f.limit}` : t("v3.projects.isolation.quotaUnknown")}
                      </span>
                    </div>
                    <div className="bar"><i data-s={meter?.s ?? "off"} style={{ width: `${meter?.pct ?? 0}%` }} /></div>
                  </div>
                );
              })}
            </div>
          </div>
        )}
        {run && run.error && entry?.state === "failed" && <Notice s="act">{run.error}</Notice>}
        {run && run.agents.length > 0 && <Board run={run} />}
        {run && run.agents.length === 0 && entry?.state !== "running" && (
          <small className="v3-hint">{t("v3.projects.isolation.noAgents")}</small>
        )}
        {!run && iso.mode === "isolated" && (ws.state === "ready" || ws.state === "partial") && (
          <small className="v3-hint"><ShieldCheck size={13} /> {t("v3.projects.isolation.readyToMigrate")}</small>
        )}
      </div>
    </section>
  );
}

function Board({ run }: { run: MigrationRun }) {
  const { t } = useTranslation();
  const steps = AGENT_STEPS[run.direction];
  const cols = { gridTemplateColumns: `repeat(${steps.length}, minmax(0, 1fr))` };
  return (
    <div className="v3-iso-board" data-testid="project-isolation-board">
      <div className="v3-iso-row head" aria-hidden="true">
        <span className="v3-iso-label" style={{ margin: 0 }}>{t("v3.projects.isolation.agent")}</span>
        <div className="v3-iso-steps heads" style={cols}>
          {steps.map((s) => <span key={s}>{t(`v3.projects.isolation.step.${s}`)}</span>)}
        </div>
        <span className="v3-iso-label" style={{ margin: 0 }}>{t("v3.projects.isolation.detail")}</span>
      </div>
      {run.agents.map((a) => <AgentLine key={a.agent_id} agent={a} steps={steps} cols={cols} />)}
    </div>
  );
}

function AgentLine({ agent, steps, cols }: { agent: MigrationAgent; steps: string[]; cols: CSSProperties }) {
  const { t } = useTranslation();
  const note = agent.status === "failed" ? "act" : agent.status === "awaiting_release" ? "wait" : undefined;
  return (
    <div className="v3-iso-row" data-skipped={agent.status === "skipped"} data-testid={`project-isolation-agent-${agent.name}`}>
      <div className="name">
        <span>{agent.name}</span>
        <small>{agent.method}{agent.endpoint_mode === "live" ? " · live" : ""}</small>
      </div>
      <div className="v3-iso-steps" style={cols} role="list" aria-label={agent.name}>
        {steps.map((s) => {
          const st = agent.steps[s]?.status;
          return (
            <span
              key={s}
              role="listitem"
              className="v3-iso-step"
              data-s={stepSignal(st)}
              title={`${t(`v3.projects.isolation.step.${s}`)}: ${t(`v3.projects.isolation.agentStatus.${st ?? "pending"}`)}${agent.steps[s]?.detail ? ` — ${agent.steps[s]?.detail}` : ""}`}
            />
          );
        })}
      </div>
      <span className="note" data-s={note}>
        {agent.status === "awaiting_release"
          ? t("v3.projects.isolation.awaiting")
          : agent.error || agent.detail || t(`v3.projects.isolation.agentStatus.${agent.status}`)}
      </span>
    </div>
  );
}
