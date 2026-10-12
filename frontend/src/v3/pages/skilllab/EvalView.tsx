import { ArrowLeft, Square, Trash2 } from "lucide-react";
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { useNavigate } from "react-router-dom";

import { api, errorMessage, type SkillLabJobInfo, type SkillLabJobResults } from "../../../lib/api";
import { elapsed, isLiveJob, LIVE_STATUSES, tasksetLabel } from "../../../lib/skillLab";
import { ArtifactBrowser } from "../../../v2/pages/skilllab/ArtifactBrowser";
import { EvalResults } from "../../../v2/pages/skilllab/EvalResults";
import { passRateCache, useJob } from "../../../v2/pages/skilllab/state";
import { ago } from "../../format";
import { useToast } from "../../hooks";
import { Btn, Chip, Confirm, Lamp, Notice, PageHead, Panel, Skeleton, Stat } from "../../ui";
import { Hosted } from "./Hosted";
import { JobLog } from "./JobLog";
import { JobStatus } from "./Lists";
import { jobSignal, slUrl } from "./signal";

/** The overview rows eval jobs show (V2's JobOverview, as key/value rows). */
function Overview({ job }: { job: SkillLabJobInfo }) {
  const { t } = useTranslation();
  return (
    <dl className="v3-kv">
      <dt>ID</dt>
      <dd className="mono">{job.id}</dd>
      <dt>{t("skillLab.eval.field.skillSource")}</dt>
      <dd>
        {job.skill_source ? (
          <span style={{ display: "inline-flex", gap: 8, alignItems: "center" }}>
            <Chip s={job.skill_source.kind === "registry" ? "info" : undefined}>{t(`skillLab.eval.wizard.source.${job.skill_source.kind}`)}</Chip>
            <span className="mono">{job.skill_source.version || ""}</span>
          </span>
        ) : "—"}
      </dd>
      <dt>{t("skillLab.eval.col.taskset")}</dt>
      <dd>{tasksetLabel(job)}</dd>
      <dt>{t("skillLab.backend.field")}</dt>
      <dd className="mono">{job.params.target_backend ?? "claude_code_exec"}</dd>
      <dt>{t("skillLab.eval.wizard.field.targetModel")}</dt>
      <dd className="mono">{job.params.target_model}</dd>
      <dt>{t("skillLab.eval.wizard.field.judgeModel")}</dt>
      <dd className="mono">{job.params.judge_model} · {job.params.judge_mode ?? "auto"}</dd>
      <dt>{t("skillLab.eval.field.execution")}</dt>
      <dd className="mono">
        workers {job.params.workers} · timeout {job.params.timeout}s{job.params.limit > 0 ? ` · limit ${job.params.limit}` : ""}
      </dd>
    </dl>
  );
}

/**
 * One evaluation run: what ran, how far it is, and — once it ends — the pass
 * rate and per-task verdicts. The result tables and the artifact browser are
 * V2's components on the V3 theme.
 */
export function EvalView({ id }: { id: string }) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const toast = useToast();
  const { job, setJob, missing, error } = useJob(id);
  const [results, setResults] = useState<SkillLabJobResults | null>(null);
  const [resultsPending, setResultsPending] = useState(true);
  const [confirm, setConfirm] = useState<"cancel" | "delete" | null>(null);
  const [busy, setBusy] = useState(false);
  const back = () => navigate(slUrl({ tab: "eval" }));

  // results.json lands when the run ends: a terminal status is the trigger, and a
  // 404 is a normal answer (cancelled, or failed before scoring)
  const status = job?.status ?? null;
  useEffect(() => {
    if (status === null || LIVE_STATUSES.includes(status)) {
      setResults(null);
      return;
    }
    let stale = false;
    setResultsPending(true);
    api
      .skillLabJobResults(id)
      .then((data) => {
        if (stale) return;
        setResults(data);
        passRateCache.set(id, data.summary.pass_rate);
      })
      .catch(() => {
        if (!stale) setResults(null);
      })
      .finally(() => {
        if (!stale) setResultsPending(false);
      });
    return () => {
      stale = true;
    };
  }, [id, status]);

  if (missing) {
    return (
      <Notice s="wait">
        <b>{t("skillLab.eval.gone.title")}</b> — {t("skillLab.eval.gone.body")}{" "}
        <button type="button" className="v3-btn sm" onClick={back}>{t("v3.skilllab.back")}</button>
      </Notice>
    );
  }
  if (!job) return error ? <Notice s="act">{error}</Notice> : <Skeleton rows={6} />;

  const live = isLiveJob(job);
  const s = jobSignal(job);
  const act = async () => {
    setBusy(true);
    try {
      if (confirm === "cancel") {
        setJob(await api.skillLabJobCancel(job.id));
        toast("ok", t("skillLab.eval.cancelRequested"));
      } else if (confirm === "delete") {
        await api.skillLabJobDelete(job.id);
        toast("ok", t("skillLab.eval.deleted"));
        back();
      }
    } catch (err) {
      toast("act", errorMessage(err));
    } finally {
      setBusy(false);
      setConfirm(null);
    }
  };
  const summary = results?.summary;

  return (
    <div className="v3-reveal v3-sl" style={{ display: "grid", gap: 16 }}>
      <div>
        <button type="button" className="v3-btn ghost sm" onClick={back}><ArrowLeft size={14} /> {t("v3.skilllab.evalTab")}</button>
      </div>
      <PageHead
        eyebrow={`${t("v3.skilllab.evalRun")} · ${job.id.slice(0, 12)}`}
        title={
          <span style={{ display: "inline-flex", alignItems: "center", gap: 14 }}>
            <Lamp s={s} live={job.status === "running"} />
            {job.skill_source?.name ?? t("skillLab.eval.unknownSkill")}
          </span>
        }
        sub={<span style={{ display: "inline-flex", gap: 8, alignItems: "center" }}><JobStatus job={job} /> {tasksetLabel(job)}</span>}
        end={
          live ? (
            <Btn kind="danger" disabled={busy} onClick={() => setConfirm("cancel")}><Square size={13} /> {t("v2.skillLab.cancelRun")}</Btn>
          ) : (
            <Btn kind="danger" disabled={busy} onClick={() => setConfirm("delete")}><Trash2 size={13} /> {t("v3.skilllab.delete")}</Btn>
          )
        }
      />
      {job.error && <Notice s="act"><span style={{ whiteSpace: "pre-wrap" }}>{job.error}</span></Notice>}

      <div className="v3-grid c4">
        <Panel signal={summary ? (summary.pass_rate >= 0.8 ? "ok" : summary.pass_rate >= 0.5 ? "wait" : "act") : undefined}>
          <Stat label={t("skillLab.eval.col.passRate")} value={summary ? `${(summary.pass_rate * 100).toFixed(1)}%` : "—"}
            foot={live ? job.progress || t(`skillLab.eval.status.${job.status}`) : undefined} />
        </Panel>
        <Panel><Stat label={t("v3.skilllab.elapsed")} value={elapsed(job)} foot={job.finished_at ? ago(job.finished_at) : undefined} /></Panel>
        <Panel><Stat label={t("v3.skilllab.created")} value={ago(job.created_at)} /></Panel>
        <Panel>
          <Stat label={t("v3.skilllab.queue")} value={job.status === "queued" && job.queue_position > 0 ? `#${job.queue_position}` : "—"}
            foot={job.status === "queued" && job.queue_position > 0 ? t("skillLab.eval.queuedAt", { n: job.queue_position }) : undefined} />
        </Panel>
      </div>

      <div className="v3-grid v3-split" style={{ alignItems: "start" }}>
        <Panel title={t("v2.skillLab.overview")}><Overview job={job} /></Panel>
        <Panel title={live ? t("skillLab.eval.progress") : t("skillLab.eval.log.title")}
          signal={live ? "wait" : undefined}
          end={live ? <Chip s="wait">{job.progress || job.status}</Chip> : undefined}>
          <JobLog jobId={job.id} live={live} />
        </Panel>
      </div>

      {!live && (
        results !== null ? (
          <Hosted><EvalResults results={results} /></Hosted>
        ) : (
          <Panel title={t("v2.skillLab.taskResults")}>
            {resultsPending ? <Skeleton rows={3} /> : <Notice>{t("skillLab.eval.noResults")}</Notice>}
          </Panel>
        )
      )}

      <Panel title={t("skillLab.eval.artifacts.title")}>
        <Hosted><ArtifactBrowser jobId={job.id} live={live} /></Hosted>
      </Panel>

      {confirm && (
        <Confirm
          title={confirm === "cancel" ? t("skillLab.eval.confirmCancel.title") : t("skillLab.eval.confirmDelete.title")}
          confirmLabel={confirm === "cancel" ? t("v2.skillLab.cancelRun") : t("v3.skilllab.delete")}
          cancelLabel={t("v3.common.cancel")}
          danger
          busy={busy}
          onCancel={() => setConfirm(null)}
          onConfirm={() => void act()}
        >
          {confirm === "cancel" ? t("skillLab.eval.confirmCancel.body") : t("skillLab.eval.confirmDelete.body", { name: job.skill_source?.name ?? job.id })}
        </Confirm>
      )}
    </div>
  );
}
