import { Archive, ArchiveRestore, Download, FileArchive, Trash2, Undo2 } from "lucide-react";
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import { ApiErrorDetail } from "../../../components/ApiErrorDetail";
import { errorMessage } from "../../../lib/api";
import { type PurgePlan, projectLifecycleApi } from "../../../lib/api/project-lifecycle";
import { type ProjectDetail, projectsApi } from "../../../lib/api/projects";
import { useLoad, useToast } from "../../hooks";
import { Btn, Chip, Confirm, Notice, Panel, Skeleton } from "../../ui";
import { countdownParts } from "./model";

type Pending = "archive" | "unarchive" | "delete" | "cancel" | null;

/**
 * §11.5 "Lifecycle" (administrators): archive / unarchive, an export bundle with its
 * download link, the deletion request with its cooling-off countdown and cancel, and
 * the purge — a dry run lists what goes, and the confirmation asks for the project id.
 */
export function LifecycleSection({ project, onChanged }: { project: ProjectDetail; onChanged: () => void }) {
  const { t } = useTranslation();
  const toast = useToast();
  const [tick, setTick] = useState(0);
  const loaded = useLoad(() => projectLifecycleApi.get(project.id), `v3-project-lifecycle:${project.id}:${tick}`);
  const [pending, setPending] = useState<Pending>(null);
  const [plan, setPlan] = useState<PurgePlan | null>(null);
  const [typed, setTyped] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [now, setNow] = useState(() => Date.now());
  const life = loaded.data;
  const deletion = life?.deletion ?? null;
  // the countdown ticks locally from the moment the view was read
  const [readAt, setReadAt] = useState(() => Date.now());
  useEffect(() => setReadAt(Date.now()), [life]);
  useEffect(() => {
    if (!deletion || deletion.purge_ready) return;
    const id = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(id);
  }, [deletion]);

  const reload = () => {
    setTick((n) => n + 1);
    onChanged();
  };
  const run = async <T,>(action: () => Promise<T>, done: string | ((result: T) => string)) => {
    setBusy(true);
    setError(null);
    try {
      const result = await action();
      toast("ok", typeof done === "string" ? done : done(result));
      reload();
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
      setPending(null);
    }
  };
  const openPurge = async () => {
    setBusy(true);
    setError(null);
    try {
      setTyped("");
      setPlan(await projectLifecycleApi.purge(project.id, { dryRun: true }));
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  };
  const purge = async () => {
    setBusy(true);
    try {
      const started = await projectLifecycleApi.purge(project.id);
      toast("ok", t("v3.projects.lifecycle.purgeStarted", { name: project.name, job: started.job_id ?? "" }));
      setPlan(null);
      reload();
    } catch (err) {
      toast("act", errorMessage(err), err);
    } finally {
      setBusy(false);
    }
  };

  if (loaded.loading && !life) return <Panel title={t("v3.projects.lifecycle.title")}><Skeleton rows={3} /></Panel>;
  if (!life) {
    return (
      <Panel title={t("v3.projects.lifecycle.title")}>
        <Notice s="act"><ApiErrorDetail error={loaded.failure ?? loaded.error} /></Notice>
      </Panel>
    );
  }

  const left = deletion ? Math.max(0, deletion.seconds_left - Math.floor((now - readAt) / 1000)) : 0;
  const parts = countdownParts(left);
  const ready = Boolean(deletion?.purge_ready) || (deletion != null && left === 0);
  const archived = life.status === "archived";
  const job = life.purge_job;

  return (
    <Panel title={t("v3.projects.lifecycle.title")} end={<Chip>{t(`v3.projects.lifecycle.status.${life.status}`)}</Chip>}>
      <div style={{ display: "grid", gap: 12 }} data-testid="project-lifecycle">
        <small className="v3-hint">{t("v3.projects.lifecycle.sub", { days: life.cooling_off_days })}</small>
        {life.is_default && <Notice>{t("v3.projects.lifecycle.defaultLocked")}</Notice>}

        {!life.is_default && life.status !== "deleted" && !deletion && (
          <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
            {archived ? (
              <Btn onClick={() => setPending("unarchive")} disabled={busy}><ArchiveRestore size={14} /> {t("v3.projects.archive.unarchive")}</Btn>
            ) : (
              <Btn onClick={() => setPending("archive")} disabled={busy}><Archive size={14} /> {t("v3.projects.archive.action")}</Btn>
            )}
            {archived && (
              <Btn kind="danger" onClick={() => setPending("delete")} disabled={busy}><Trash2 size={14} /> {t("v3.projects.lifecycle.requestDeletion")}</Btn>
            )}
          </div>
        )}
        {!life.is_default && !archived && !deletion && life.status === "active" && (
          <small className="v3-hint">{t("v3.projects.lifecycle.archiveFirst")}</small>
        )}

        {deletion && life.status !== "deleted" && (
          <Notice s={ready ? "act" : "wait"}>
            <div style={{ display: "grid", gap: 8 }}>
              <span data-testid="project-lifecycle-countdown">
                {ready
                  ? t("v3.projects.lifecycle.purgeReady")
                  : t("v3.projects.lifecycle.countdown", { ...parts, at: new Date(deletion.purge_after).toLocaleString() })}
              </span>
              <small>{t("v3.projects.lifecycle.requestedBy", { who: deletion.requested_by, at: new Date(deletion.requested_at).toLocaleString() })}</small>
              <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
                <Btn onClick={() => setPending("cancel")} disabled={busy}><Undo2 size={14} /> {t("v3.projects.lifecycle.cancel")}</Btn>
                {ready && (
                  <Btn kind="danger" onClick={() => void openPurge()} disabled={busy}><Trash2 size={14} /> {t("v3.projects.lifecycle.purge")}</Btn>
                )}
              </div>
            </div>
          </Notice>
        )}

        {job && (
          <small className="v3-hint" data-testid="project-lifecycle-job">
            {t("v3.projects.lifecycle.job", { id: job.id, status: job.status, stage: job.stage ?? "—" })}
            {job.error ? ` · ${job.error}` : ""}
          </small>
        )}
        {life.deleted && (
          <Notice s="ok">{t("v3.projects.lifecycle.deleted", { at: new Date(life.deleted.at).toLocaleString() })}</Notice>
        )}

        <div style={{ display: "grid", gap: 6 }}>
          <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
            <strong>{t("v3.projects.lifecycle.exports")}</strong>
            {life.status !== "deleted" && (
              <Btn size="sm" disabled={busy}
                onClick={() => void run(() => projectLifecycleApi.exportBundle(project.id), t("v3.projects.lifecycle.exported", { name: project.name }))}>
                <FileArchive size={13} /> {t("v3.projects.lifecycle.export")}
              </Btn>
            )}
          </div>
          {life.exports.length === 0 ? (
            <small className="v3-hint">{t("v3.projects.lifecycle.noExports")}</small>
          ) : (
            <ul style={{ margin: 0, paddingLeft: 18 }}>
              {life.exports.map((x) => (
                <li key={x.id}>
                  <a href={projectLifecycleApi.exportUrl(project.id, x.id)} download data-testid="project-export-link">
                    <Download size={12} /> {new Date(x.created_at).toLocaleString()}
                  </a>{" "}
                  <small className="v3-hint">{t("v3.projects.lifecycle.exportMeta", { who: x.created_by, kb: Math.max(1, Math.round(x.bytes / 1024)) })}</small>
                </li>
              ))}
            </ul>
          )}
        </div>

        {error != null && <Notice s="act"><ApiErrorDetail error={error} /></Notice>}
      </div>

      {pending && (
        <Confirm
          title={t(`v3.projects.lifecycle.confirm.${pending}.title`, { name: project.name })}
          confirmLabel={t(`v3.projects.lifecycle.confirm.${pending}.action`)}
          cancelLabel={t("v3.projects.cancel")}
          danger={pending === "archive" || pending === "delete"}
          busy={busy}
          onConfirm={() => {
            const name = project.name;
            if (pending === "archive") void run(() => projectsApi.archive(project.id), (r) => t("v3.projects.archive.done", { name, links: r.links_revoked, keys: r.keys_disabled }));
            else if (pending === "unarchive") void run(() => projectsApi.unarchive(project.id), t("v3.projects.archive.unarchived", { name }));
            else if (pending === "delete") void run(() => projectLifecycleApi.requestDeletion(project.id), t("v3.projects.lifecycle.requested", { name, days: life.cooling_off_days }));
            else void run(() => projectLifecycleApi.cancelDeletion(project.id), t("v3.projects.lifecycle.cancelled", { name }));
          }}
          onCancel={() => setPending(null)}
        >
          {t(`v3.projects.lifecycle.confirm.${pending}.body`, { name: project.name, days: life.cooling_off_days })}
        </Confirm>
      )}

      {plan && (
        <Confirm
          title={t("v3.projects.lifecycle.confirm.purge.title", { name: project.name })}
          confirmLabel={t("v3.projects.lifecycle.confirm.purge.action")}
          cancelLabel={t("v3.projects.cancel")}
          danger
          busy={busy}
          confirmDisabled={typed.trim() !== project.id}
          onConfirm={() => void purge()}
          onCancel={() => setPlan(null)}
        >
          <div style={{ display: "grid", gap: 8 }} data-testid="project-purge-plan">
            <span>{t("v3.projects.lifecycle.confirm.purge.body", { name: project.name })}</span>
            <strong>{t("v3.projects.lifecycle.planAgents", { count: plan.agents.length })}</strong>
            {plan.agents.length > 0 && (
              <ul style={{ margin: 0, paddingLeft: 18 }}>
                {plan.agents.map((a) => (
                  <li key={a.id}><span className="mono">{a.name}</span> · {a.method}{a.workspace_id ? ` · ${a.workspace_id}` : ""}</li>
                ))}
              </ul>
            )}
            <strong>{t("v3.projects.lifecycle.planRows")}</strong>
            <span className="mono" style={{ fontSize: 12 }}>
              {Object.entries(plan.rows).map(([table, n]) => `${table}: ${n}`).join(" · ") || "—"}
            </span>
            {plan.retained.length > 0 && (
              <small className="v3-hint">{t("v3.projects.lifecycle.planRetained", { list: plan.retained.join(", ") })}</small>
            )}
            <label className="v3-field">
              <span>{t("v3.projects.lifecycle.typeId", { id: project.id })}</span>
              <input className="v3-input mono" value={typed} onChange={(e) => setTyped(e.target.value)} data-testid="project-purge-id" />
            </label>
          </div>
        </Confirm>
      )}
    </Panel>
  );
}
