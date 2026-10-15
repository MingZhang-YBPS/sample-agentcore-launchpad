import { CheckCircle2, CircleDashed, Copy, Loader2, RefreshCw, RotateCcw, ShieldAlert, XCircle } from "lucide-react";
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import { useAuth } from "../../../auth/auth-context";
import { errorMessage } from "../../../lib/api";
import {
  LIVE_STATUSES,
  scheduleAt,
  stepStates,
  type SystemUpdate,
  systemUpdateApi,
  type UpdateState,
} from "../../../lib/api/systemUpdate";
import { ago } from "../../format";
import { useLoad, useToast } from "../../hooks";
import { Btn, Chip, Empty, Notice, PageHead, Panel, type Signal, Skeleton } from "../../ui";

const POLL_MS = 5000;

export const STATUS_SIGNAL: Record<SystemUpdate["status"], Signal> = {
  preparing: "wait",
  prepare_failed: "act",
  ready: "ok",
  scheduled: "wait",
  applying: "wait",
  done: "ok",
  rolled_back: "act",
  failed: "act",
  superseded: "off",
};

function when(iso: string | null | undefined): string {
  return iso ? new Date(iso).toLocaleString() : "—";
}

function StepTrack({ update }: { update: SystemUpdate }) {
  const { t } = useTranslation();
  return (
    <div style={{ display: "flex", flexWrap: "wrap", gap: 12 }} data-testid="v3-sysupdate-steps">
      {stepStates(update).map(({ step, state }) => (
        <span key={step} style={{ display: "inline-flex", gap: 4, alignItems: "center" }}>
          {state === "done" ? <CheckCircle2 size={14} aria-hidden="true" />
            : state === "failed" ? <XCircle size={14} aria-hidden="true" />
            : state === "running" ? <Loader2 size={14} className="spin" aria-hidden="true" />
            : <CircleDashed size={14} aria-hidden="true" />}
          {t(`v3.systemUpdate.step.${step}`)}
        </span>
      ))}
    </div>
  );
}

function SchedulePanel({ state, update, onDone }: { state: UpdateState; update: SystemUpdate; onDone: () => void }) {
  const { t } = useTranslation();
  const toast = useToast();
  const [mode, setMode] = useState<"now" | "at">("now");
  const [at, setAt] = useState("");
  const [ack, setAck] = useState(false);
  const [busy, setBusy] = useState(false);
  const interrupted = state.inflight?.interrupted ?? 0;
  const run = async (work: () => Promise<unknown>, okKey: string) => {
    setBusy(true);
    try {
      await work();
      toast("ok", t(okKey));
      onDone();
    } catch (err) {
      toast("act", errorMessage(err), err);
    } finally {
      setBusy(false);
    }
  };

  if (update.status === "scheduled") {
    return (
      <Panel title={t("v3.systemUpdate.scheduledTitle")}>
        <div style={{ display: "grid", gap: 10 }}>
          <span>{t("v3.systemUpdate.scheduledFor", { when: when(update.scheduled_at) })}</span>
          {update.announcement_id && <small className="muted">{t("v3.systemUpdate.announced")}</small>}
          <span>
            <Btn kind="ghost" disabled={busy} onClick={() => void run(() => systemUpdateApi.cancel(update.id), "v3.systemUpdate.cancelled")}>
              {t("v3.systemUpdate.cancel")}
            </Btn>
          </span>
        </div>
      </Panel>
    );
  }
  if (!state.version.can_schedule) {
    return (
      <Panel title={t("v3.systemUpdate.manualTitle")}>
        <div style={{ display: "grid", gap: 10 }}>
          <Notice s="info">{t("v3.systemUpdate.manualBody")}</Notice>
          {state.manual_commands && (
            <>
              <pre className="mono" style={{ whiteSpace: "pre-wrap", margin: 0 }} data-testid="v3-sysupdate-manual">
                {state.manual_commands.join("\n")}
              </pre>
              <span>
                <Btn kind="ghost" size="sm" onClick={() => void navigator.clipboard?.writeText(state.manual_commands?.join("\n") ?? "")}>
                  <Copy size={13} /> {t("v3.systemUpdate.copy")}
                </Btn>
              </span>
            </>
          )}
        </div>
      </Panel>
    );
  }
  const items = state.inflight?.items ?? [];
  return (
    <Panel title={t("v3.systemUpdate.scheduleTitle")}>
      <div style={{ display: "grid", gap: 10 }}>
        <label style={{ display: "flex", gap: 6, alignItems: "center" }}>
          <input type="radio" name="v3-sysupdate-when" checked={mode === "now"} onChange={() => setMode("now")} />
          {t("v3.systemUpdate.now")}
        </label>
        <label style={{ display: "flex", gap: 6, alignItems: "center" }}>
          <input type="radio" name="v3-sysupdate-when" checked={mode === "at"} onChange={() => setMode("at")} />
          {t("v3.systemUpdate.at")}
          <input type="datetime-local" className="v3-input" value={at} disabled={mode !== "at"}
            aria-label={t("v3.systemUpdate.at")} onChange={(e) => setAt(e.target.value)} />
        </label>
        {items.length > 0 && (
          <details>
            <summary>{t("v3.systemUpdate.inflight", { count: items.length, interrupted })}</summary>
            <ul style={{ margin: "6px 0 0", paddingLeft: 18 }}>
              {items.map((i) => (
                <li key={`${i.kind}:${i.id}`}>
                  <small>
                    <Chip s={i.restart_outcome === "resumes" || i.restart_outcome === "reconciles" || i.restart_outcome === "restart_safe" ? "ok" : "act"}>
                      {t(`v3.systemUpdate.outcome.${i.restart_outcome}`, { defaultValue: i.restart_outcome })}
                    </Chip>{" "}
                    {i.label}
                  </small>
                </li>
              ))}
            </ul>
          </details>
        )}
        {interrupted > 0 && (
          <label style={{ display: "flex", gap: 6, alignItems: "center" }}>
            <input type="checkbox" checked={ack} onChange={(e) => setAck(e.target.checked)} data-testid="v3-sysupdate-ack" />
            {t("v3.systemUpdate.ack", { count: interrupted })}
          </label>
        )}
        <span>
          <Btn kind="primary" disabled={busy || (interrupted > 0 && !ack) || (mode === "at" && !at)}
            onClick={() => void run(() => systemUpdateApi.schedule(update.id, mode === "at" ? scheduleAt(at) : null, ack), "v3.systemUpdate.scheduled")}>
            {t("v3.systemUpdate.schedule")}
          </Btn>
        </span>
      </div>
    </Panel>
  );
}

function LogPanel({ update }: { update: SystemUpdate }) {
  const { t } = useTranslation();
  const log = useLoad(() => systemUpdateApi.log(update.id), `v3-sysupdate-log:${update.id}:${update.status}:${update.updated_at}`);
  return (
    <details>
      <summary>{t("v3.systemUpdate.log")}</summary>
      {log.error ? <Notice s="act">{log.error}</Notice> : (
        <pre className="mono" style={{ whiteSpace: "pre-wrap", maxHeight: 280, overflow: "auto", margin: 0 }}>
          {(log.data?.events ?? []).map((e) => `${e.t}  [${e.step}] ${e.message}`).join("\n")}
        </pre>
      )}
    </details>
  );
}

/**
 * Configure → System update (online-update §2.7; administrators): the running and the
 * latest `console-v3` commit, the commits in between, the prepare progress, the restart
 * schedule with the tasks it interrupts, rollback and history. Hub-global.
 */
export function V3SystemUpdate() {
  const { t } = useTranslation();
  const { isAdmin } = useAuth();
  const toast = useToast();
  const [version, setVersion] = useState(0);
  const [busy, setBusy] = useState(false);
  const state = useLoad(async () => (isAdmin ? systemUpdateApi.state() : null), `v3-sysupdate:${isAdmin}:${version}`);
  const reload = () => setVersion((v) => v + 1);
  const current = state.data?.current ?? null;
  const live = current !== null && LIVE_STATUSES.has(current.status);

  useEffect(() => {
    if (!live) return undefined;
    const timer = window.setTimeout(() => setVersion((v) => v + 1), POLL_MS);
    return () => window.clearTimeout(timer);
  }, [live, state.data]);

  if (!isAdmin) {
    return (
      <div className="v3-reveal" style={{ display: "grid", gap: 16 }}>
        <PageHead eyebrow={t("v3.systemUpdate.eyebrow")} title={t("v3.systemUpdate.title")} sub={t("auth.adminRequired.meta")} />
        <Panel>
          <Empty title={<span style={{ display: "inline-flex", gap: 8, alignItems: "center" }}><ShieldAlert size={16} /> {t("auth.adminRequired.title")}</span>}>
            {t("usersPage.forbiddenBody")}
          </Empty>
        </Panel>
      </div>
    );
  }

  const act = async (work: () => Promise<unknown>, okKey: string) => {
    setBusy(true);
    try {
      await work();
      toast("ok", t(okKey));
      reload();
    } catch (err) {
      toast("act", errorMessage(err), err);
    } finally {
      setBusy(false);
    }
  };

  const data = state.data;
  const remote = data?.remote ?? null;
  const commits = remote?.commits ?? [];
  const blocked = current !== null && ["preparing", "scheduled", "applying"].includes(current.status);
  const canPrepare = !!remote && !remote.error && remote.fast_forward === true && !remote.up_to_date && !blocked;
  const canRollback = !blocked && (data?.history ?? []).some((h) => h.status === "done" && h.kind === "update" && h.target_sha === data?.version.sha);

  return (
    <div className="v3-reveal" style={{ display: "grid", gap: 16 }}>
      <PageHead
        eyebrow={t("v3.systemUpdate.eyebrow")}
        title={t("v3.systemUpdate.title")}
        sub={data ? t("v3.systemUpdate.sub", { repo: data.source.repo, branch: data.source.branch }) : undefined}
        end={
          <Btn kind="ghost" disabled={busy || !data?.version.update_enabled} onClick={() => void act(systemUpdateApi.check, "v3.systemUpdate.checked")}>
            <RefreshCw size={14} /> {t("v3.systemUpdate.check")}
          </Btn>
        }
      />
      {state.loading && !data ? (
        <Panel><Skeleton rows={4} /></Panel>
      ) : state.error ? (
        <Notice s="act">{state.error}</Notice>
      ) : data ? (
        <>
          {!data.version.update_enabled && <Notice s="info">{t("v3.systemUpdate.disabled")}</Notice>}
          <Panel>
            <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(260px, 1fr))", gap: 16 }}>
              <div style={{ display: "grid", gap: 4 }} data-testid="v3-sysupdate-current">
                <small className="muted">{t("v3.systemUpdate.current")}</small>
                <b className="mono">{data.version.short ?? "—"}</b>
                <small className="muted">{when(data.version.committed_at)}</small>
              </div>
              <div style={{ display: "grid", gap: 4 }} data-testid="v3-sysupdate-remote">
                <small className="muted">{t("v3.systemUpdate.latest", { branch: data.source.branch })}</small>
                {remote?.error ? (
                  <Notice s="act">{remote.error.message}</Notice>
                ) : remote ? (
                  <>
                    <b className="mono">{remote.short ?? "—"}</b>
                    <span>
                      {remote.up_to_date ? <Chip s="ok">{t("v3.systemUpdate.upToDate")}</Chip>
                        : remote.fast_forward ? <Chip s="wait">{t("v3.systemUpdate.behind", { count: remote.behind ?? 0 })}</Chip>
                        : <Chip s="act">{t("v3.systemUpdate.notFastForward")}</Chip>}
                    </span>
                    <small className="muted">{t("v3.systemUpdate.checkedAt", { when: ago(remote.checked_at) })}</small>
                  </>
                ) : <small className="muted">—</small>}
              </div>
              <div style={{ display: "grid", gap: 4 }}>
                <small className="muted">{t("v3.systemUpdate.updater")}</small>
                <span>
                  {data.version.updater_installed ? <Chip s="ok">{t("v3.systemUpdate.updaterInstalled")}</Chip>
                    : <Chip s="off">{t("v3.systemUpdate.updaterMissing")}</Chip>}
                </span>
              </div>
            </div>
          </Panel>

          {commits.length > 0 && !remote?.up_to_date && (
            <Panel title={t("v3.systemUpdate.commits", { count: remote?.behind ?? commits.length })}>
              <ul style={{ margin: 0, paddingLeft: 18, display: "grid", gap: 4 }} data-testid="v3-sysupdate-commits">
                {commits.map((c) => (
                  <li key={c.sha}>
                    <span className="mono">{c.sha.slice(0, 7)}</span> {c.title}{" "}
                    <small className="muted">· {c.author} · {when(c.time)}</small>
                  </li>
                ))}
              </ul>
            </Panel>
          )}

          <Panel
            title={t("v3.systemUpdate.updateTitle")}
            end={
              <span style={{ display: "inline-flex", gap: 8 }}>
                <Btn kind="primary" disabled={busy || !canPrepare} onClick={() => void act(systemUpdateApi.prepare, "v3.systemUpdate.preparing")}>
                  {t("v3.systemUpdate.prepare")}
                </Btn>
                <Btn kind="ghost" disabled={busy || !canRollback} title={t("v3.systemUpdate.rollbackHint")}
                  onClick={() => void act(systemUpdateApi.rollback, "v3.systemUpdate.rollbackPreparing")}>
                  <RotateCcw size={14} /> {t("v3.systemUpdate.rollback")}
                </Btn>
              </span>
            }
          >
            {current ? (
              <div style={{ display: "grid", gap: 10 }} data-testid="v3-sysupdate-row">
                <span style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
                  <Chip s={STATUS_SIGNAL[current.status]}>{t(`v3.systemUpdate.status.${current.status}`)}</Chip>
                  <span>{t(current.kind === "rollback" ? "v3.systemUpdate.rollbackTo" : "v3.systemUpdate.updateTo", { sha: current.short })}</span>
                  <small className="muted">{t("v3.systemUpdate.requestedBy", { who: current.requested_by, when: ago(current.created_at) })}</small>
                </span>
                {(current.status === "preparing" || current.status === "prepare_failed" || current.status === "ready") && <StepTrack update={current} />}
                {current.error && <Notice s="act">{current.error}</Notice>}
                <LogPanel update={current} />
              </div>
            ) : (
              <small className="muted">{t("v3.systemUpdate.none")}</small>
            )}
          </Panel>

          {current && (current.status === "ready" || current.status === "scheduled") && (
            <SchedulePanel state={data} update={current} onDone={reload} />
          )}

          {data.history.length > 0 && (
            <Panel title={t("v3.systemUpdate.history")}>
              <ul style={{ margin: 0, paddingLeft: 18, display: "grid", gap: 4 }} data-testid="v3-sysupdate-history">
                {data.history.map((h) => (
                  <li key={h.id}>
                    <small>
                      {when(h.finished_at ?? h.created_at)} · <span className="mono">{h.short}</span>{" "}
                      {h.kind === "rollback" ? `(${t("v3.systemUpdate.kindRollback")}) ` : ""}
                      <Chip s={STATUS_SIGNAL[h.status]}>{t(`v3.systemUpdate.status.${h.status}`)}</Chip>
                      {h.error ? <span className="muted"> · {h.error}</span> : null}
                    </small>
                  </li>
                ))}
              </ul>
            </Panel>
          )}
        </>
      ) : null}
    </div>
  );
}
