import { Plus, Search, Sparkles, Trash2 } from "lucide-react";
import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link, useNavigate } from "react-router-dom";

import { api, errorMessage, type SkillLabJobInfo, type SkillLabTasksetInfo } from "../../../lib/api";
import { isLiveJob, jobSkillName, RESUMABLE_STATUSES, tasksetLabel } from "../../../lib/skillLab";
import { countsLabel } from "../../../lib/skillLabTasksets";
import { bestScoreCache, passRateCache, taskgenTarget } from "../../../v2/pages/skilllab/state";
import { ago } from "../../format";
import { useToast } from "../../hooks";
import { Btn, Chip, Confirm, Empty, Filters, Lamp, Notice, Panel, Skeleton } from "../../ui";
import { jobSignal, slUrl } from "./signal";

/** Job status chip, with the queue position while it waits behind another job. */
export function JobStatus({ job }: { job: SkillLabJobInfo }) {
  const { t } = useTranslation();
  const s = jobSignal(job);
  return (
    <Chip s={s === "off" ? undefined : s}>
      {t(`skillLab.eval.status.${job.status}`, { defaultValue: job.status })}
      {job.status === "queued" && job.queue_position > 0 ? ` #${job.queue_position}` : ""}
    </Chip>
  );
}

function SearchBox({ value, onChange, placeholder }: { value: string; onChange: (v: string) => void; placeholder: string }) {
  return (
    <div style={{ marginLeft: "auto", position: "relative", flex: "1 1 200px", maxWidth: 300 }}>
      <Search size={14} style={{ position: "absolute", left: 12, top: 11, color: "var(--v3-text-3)" }} aria-hidden="true" />
      <input className="v3-input" style={{ paddingLeft: 34 }} value={value} onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder} aria-label={placeholder} />
    </div>
  );
}

type JobAction = "cancel" | "delete" | "resume";

/** Cancel / resume / delete, each behind a confirm with V2's copy. */
function useJobActions(type: "eval" | "train" | "taskgen", onDone: () => void) {
  const { t } = useTranslation();
  const toast = useToast();
  const [pending, setPending] = useState<{ job: SkillLabJobInfo; action: JobAction } | null>(null);
  const [busy, setBusy] = useState(false);
  const act = async () => {
    if (!pending) return;
    const { job, action } = pending;
    setBusy(true);
    try {
      if (action === "cancel") {
        await api.skillLabJobCancel(job.id);
        toast("ok", t("skillLab.eval.cancelRequested"));
      } else if (action === "resume") {
        await api.skillLabJobResume(job.id);
        toast("ok", t("skillLab.train.resumed"));
      } else {
        await api.skillLabJobDelete(job.id);
        toast("ok", t("skillLab.eval.deleted"));
      }
      setPending(null);
      onDone();
    } catch (err) {
      toast("act", errorMessage(err));
    } finally {
      setBusy(false);
    }
  };
  const title = !pending
    ? ""
    : pending.action === "resume"
      ? t("skillLab.train.confirmResume.title")
      : pending.action === "cancel"
        ? t(type === "taskgen" ? "v2.skillLab.confirmCancelGen" : type === "eval" ? "skillLab.eval.confirmCancel.title" : "skillLab.train.confirmCancel.title")
        : t(type === "eval" ? "skillLab.eval.confirmDelete.title" : "skillLab.train.confirmDelete.title");
  const body = !pending
    ? ""
    : pending.action === "resume"
      ? t("skillLab.train.confirmResume.body")
      : pending.action === "cancel"
        ? t("skillLab.eval.confirmCancel.body")
        : t("skillLab.eval.confirmDelete.body", { name: pending.job.skill_source?.name ?? pending.job.id });
  const dialog = pending ? (
    <Confirm
      title={title}
      confirmLabel={
        pending.action === "resume"
          ? t("skillLab.train.resume")
          : pending.action === "cancel"
            ? t(type === "taskgen" ? "skillLab.taskgen.job.cancel" : "v2.skillLab.cancelRun")
            : t("v3.skilllab.delete")
      }
      cancelLabel={t("v3.common.cancel")}
      danger={pending.action !== "resume"}
      busy={busy}
      onCancel={() => setPending(null)}
      onConfirm={() => void act()}
    >
      {body}
    </Confirm>
  ) : null;
  return { ask: (job: SkillLabJobInfo, action: JobAction) => setPending({ job, action }), busy, dialog };
}

const STATUS_FILTERS = ["all", "wait", "ok", "act", "off"] as const;
type StatusFilter = (typeof STATUS_FILTERS)[number];

/** Evaluation and optimization jobs — same shape, one score column apart. */
export function JobTable({
  type,
  rows: all,
  loading,
  error,
  reload,
}: {
  type: "eval" | "train";
  rows: SkillLabJobInfo[];
  loading: boolean;
  error: string | null;
  reload: () => void;
}) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const actions = useJobActions(type, reload);
  const [state, setState] = useState<StatusFilter>("all");
  const [skill, setSkill] = useState("");
  const [q, setQ] = useState("");
  const skills = useMemo(() => [...new Set(all.map((j) => j.skill_source?.name ?? "").filter(Boolean))].sort(), [all]);
  const rows = useMemo(() => {
    const needle = q.trim().toLowerCase();
    return all
      .filter((j) => state === "all" || jobSignal(j) === state)
      .filter((j) => !skill || j.skill_source?.name === skill)
      .filter((j) => !needle || `${j.id} ${j.skill_source?.name ?? ""} ${j.taskset_name}`.toLowerCase().includes(needle));
  }, [all, state, skill, q]);
  const count = (s: StatusFilter) => (s === "all" ? all.length : all.filter((j) => jobSignal(j) === s).length);
  // scores are known only after a detail read (the lists do not open result files)
  const score = (job: SkillLabJobInfo) => {
    if (job.status !== "succeeded") return "—";
    if (type === "eval") {
      const rate = passRateCache.get(job.id);
      return rate === undefined ? "—" : `${(rate * 100).toFixed(1)}%`;
    }
    const best = bestScoreCache.get(job.id);
    return best === undefined ? "—" : best.toFixed(3);
  };

  return (
    <div style={{ display: "grid", gap: 12 }}>
      <div style={{ display: "flex", gap: 12, alignItems: "center", flexWrap: "wrap" }}>
        <Filters
          value={state}
          onChange={setState}
          options={STATUS_FILTERS.map((s) => ({
            value: s,
            label: t(`v3.skilllab.state.${s}`),
            s: s === "all" ? undefined : s,
            count: count(s),
          }))}
        />
        {skills.length > 1 && (
          <select className="v3-select" style={{ width: 200 }} value={skill} onChange={(e) => setSkill(e.target.value)}
            aria-label={t("skillLab.eval.col.skill")}>
            <option value="">{t("v3.skilllab.allSkills")}</option>
            {skills.map((s) => <option key={s} value={s}>{s}</option>)}
          </select>
        )}
        <SearchBox value={q} onChange={setQ} placeholder={t("v2.skillLab.searchJobs")} />
      </div>
      <Panel flush>
        {loading && all.length === 0 ? (
          <div style={{ padding: 20 }}><Skeleton rows={4} /></div>
        ) : error ? (
          <div style={{ padding: 20 }}><Notice s="act">{error}</Notice></div>
        ) : rows.length === 0 ? (
          <Empty title={all.length ? t("v2.skillLab.noMatch") : t(type === "eval" ? "skillLab.eval.empty" : "skillLab.train.empty")} />
        ) : (
          <table className="v3-table">
            <thead>
              <tr>
                <th />
                <th>{t("v2.skillLab.colSkillJob")}</th>
                <th>{t("skillLab.eval.col.taskset")}</th>
                <th>{type === "eval" ? t("skillLab.backend.judgeMode") : t("skillLab.train.field.loop")}</th>
                <th className="num">{type === "eval" ? t("skillLab.eval.col.passRate") : t("skillLab.train.col.best")}</th>
                <th>{t("skillLab.eval.col.status")}</th>
                <th className="num">{t("skillLab.eval.col.created")}</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {rows.map((job) => {
                const s = jobSignal(job);
                return (
                  <tr key={job.id} className="click" onClick={() => navigate(slUrl({ tab: type, view: "detail", id: job.id }))}>
                    <td style={{ width: 30 }}><Lamp s={s} live={job.status === "running"} /></td>
                    <td>
                      <div className="v3-name">
                        <div>
                          <b>{job.skill_source?.name ?? t("skillLab.eval.unknownSkill")}</b>
                          <small>
                            {job.skill_source
                              ? job.skill_source.kind === "registry" ? job.skill_source.version || "registry" : t("v2.skillLab.uploaded")
                              : ""}
                            {" · "}{job.id.slice(0, 12)}
                          </small>
                        </div>
                      </div>
                    </td>
                    <td className="v3-sl-clip" title={tasksetLabel(job)}>{tasksetLabel(job)}</td>
                    <td className="mono" style={{ color: "var(--v3-text-2)", whiteSpace: "nowrap" }}>
                      {type === "eval"
                        ? job.params.judge_mode ?? "auto"
                        : `${t("skillLab.train.field.epochs", { n: job.params.epochs ?? 1 })} · ${job.params.gate_metric ?? "hard"}`}
                    </td>
                    <td className="num">{score(job)}</td>
                    <td><JobStatus job={job} /></td>
                    <td className="num" style={{ color: "var(--v3-text-3)" }}>{ago(job.created_at)}</td>
                    <td style={{ width: 1, whiteSpace: "nowrap" }} onClick={(e) => e.stopPropagation()}>
                      <div style={{ display: "flex", gap: 4, justifyContent: "flex-end" }}>
                        {isLiveJob(job) && (
                          <Btn size="sm" kind="ghost" disabled={actions.busy} onClick={() => actions.ask(job, "cancel")}>{t("v2.skillLab.cancelRun")}</Btn>
                        )}
                        {type === "train" && RESUMABLE_STATUSES.includes(job.status) && (
                          <Btn size="sm" disabled={actions.busy} onClick={() => actions.ask(job, "resume")}>{t("skillLab.train.resume")}</Btn>
                        )}
                        {!isLiveJob(job) && (
                          <Btn size="sm" kind="ghost" disabled={actions.busy} onClick={() => actions.ask(job, "delete")} title={t("v3.skilllab.delete")}>
                            <Trash2 size={13} />
                          </Btn>
                        )}
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </Panel>
      {actions.dialog}
    </div>
  );
}

/** Task sets, with V2's guards: a built-in sample is read-only. */
export function TasksetTable({
  rows: all,
  loading,
  error,
  reload,
}: {
  rows: SkillLabTasksetInfo[];
  loading: boolean;
  error: string | null;
  reload: () => void;
}) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const toast = useToast();
  const [mode, setMode] = useState<"all" | "single" | "split">("all");
  const [q, setQ] = useState("");
  const [pending, setPending] = useState<SkillLabTasksetInfo | null>(null);
  const [busy, setBusy] = useState(false);
  const rows = useMemo(() => {
    const needle = q.trim().toLowerCase();
    return all
      .filter((r) => mode === "all" || r.mode === mode)
      .filter((r) => !needle || `${r.name} ${r.id} ${r.description}`.toLowerCase().includes(needle));
  }, [all, mode, q]);
  const remove = async () => {
    if (!pending) return;
    setBusy(true);
    try {
      await api.skillLabTasksetDelete(pending.id);
      toast("ok", t("skillLab.tasksets.deleted"));
      setPending(null);
      reload();
    } catch (err) {
      toast("act", errorMessage(err));
    } finally {
      setBusy(false);
    }
  };
  return (
    <div style={{ display: "grid", gap: 12 }}>
      <div style={{ display: "flex", gap: 12, alignItems: "center", flexWrap: "wrap" }}>
        <Filters
          value={mode}
          onChange={setMode}
          options={[
            { value: "all", label: t("v3.skilllab.state.all"), count: all.length },
            { value: "single", label: t("skillLab.tasksets.mode.single"), count: all.filter((r) => r.mode === "single").length },
            { value: "split", label: t("skillLab.tasksets.mode.split"), count: all.filter((r) => r.mode === "split").length },
          ]}
        />
        <SearchBox value={q} onChange={setQ} placeholder={t("v2.skillLab.searchTasksets")} />
      </div>
      <Panel flush>
        {loading && all.length === 0 ? (
          <div style={{ padding: 20 }}><Skeleton rows={4} /></div>
        ) : error ? (
          <div style={{ padding: 20 }}><Notice s="act">{error}</Notice></div>
        ) : rows.length === 0 ? (
          <Empty title={all.length ? t("v2.skillLab.noMatch") : t("skillLab.tasksets.empty")} />
        ) : (
          <table className="v3-table">
            <thead>
              <tr>
                <th>{t("v2.skillLab.colNameId")}</th>
                <th>{t("skillLab.tasksets.col.mode")}</th>
                <th>{t("skillLab.tasksets.col.counts")}</th>
                <th className="num">{t("skillLab.tasksets.col.updated")}</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.id} className="click" onClick={() => navigate(slUrl({ tab: "tasksets", view: "detail", id: r.id }))}>
                  <td>
                    <div className="v3-name">
                      <div>
                        <b>{r.name} {r.sample && <Chip s="info">{t("skillLab.tasksets.sampleChip")}</Chip>}</b>
                        <small>{r.description ? r.description.slice(0, 100) : r.id}</small>
                      </div>
                    </div>
                  </td>
                  <td><Chip s={r.mode === "split" ? "info" : undefined}>{t(`skillLab.tasksets.mode.${r.mode}`)}</Chip></td>
                  <td className="mono" style={{ color: "var(--v3-text-2)", whiteSpace: "nowrap" }}>{countsLabel(r.counts)}</td>
                  <td className="num" style={{ color: "var(--v3-text-3)" }}>{ago(r.updated_at)}</td>
                  <td style={{ width: 1, whiteSpace: "nowrap" }} onClick={(e) => e.stopPropagation()}>
                    {!r.sample && (
                      <div style={{ display: "flex", gap: 4, justifyContent: "flex-end" }}>
                        <Link className="v3-btn sm ghost" to={slUrl({ tab: "tasksets", view: "edit", id: r.id })}>{t("v3.skilllab.edit")}</Link>
                        <Btn size="sm" kind="ghost" disabled={busy} onClick={() => setPending(r)} title={t("v3.skilllab.delete")}><Trash2 size={13} /></Btn>
                      </div>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Panel>
      {pending && (
        <Confirm
          title={t("skillLab.tasksets.confirmDelete.title")}
          confirmLabel={t("v3.skilllab.delete")}
          cancelLabel={t("v3.common.cancel")}
          danger
          busy={busy}
          onCancel={() => setPending(null)}
          onConfirm={() => void remove()}
        >
          {t("skillLab.tasksets.confirmDelete.body", { name: pending.name })}
        </Confirm>
      )}
    </div>
  );
}

/** AI generation jobs, under the task sets they feed. */
export function TaskgenTable({
  rows,
  loading,
  error,
  reload,
}: {
  rows: SkillLabJobInfo[];
  loading: boolean;
  error: string | null;
  reload: () => void;
}) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const actions = useJobActions("taskgen", reload);
  // the newest eight by default; V2 pages through all of them, so all stay reachable
  const [showAll, setShowAll] = useState(false);
  return (
    <Panel
      title={t("v2.skillLab.genRecords")}
      flush
      end={
        <Link className="v3-btn sm" to={slUrl({ tab: "tasksets", view: "gen-new" })}>
          <Sparkles size={13} /> {t("skillLab.taskgen.open")}
        </Link>
      }
    >
      <p style={{ margin: "0 20px 10px", color: "var(--v3-text-3)", fontSize: 13 }}>{t("skillLab.taskgen.listSub")}</p>
      {loading && rows.length === 0 ? (
        <div style={{ padding: 20 }}><Skeleton rows={3} /></div>
      ) : error ? (
        <div style={{ padding: 20 }}><Notice s="act">{error}</Notice></div>
      ) : rows.length === 0 ? (
        <Empty title={t("skillLab.taskgen.empty")} />
      ) : (
        <table className="v3-table">
          <thead>
            <tr>
              <th />
              <th>{t("v2.skillLab.colSkillJob")}</th>
              <th>{t("skillLab.taskgen.col.target")}</th>
              <th className="num">{t("skillLab.taskgen.field.count")}</th>
              <th>{t("skillLab.eval.field.models")}</th>
              <th>{t("skillLab.eval.col.status")}</th>
              <th className="num">{t("skillLab.eval.col.created")}</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {(showAll ? rows : rows.slice(0, 8)).map((job) => (
              <tr key={job.id} className="click" onClick={() => navigate(slUrl({ tab: "tasksets", view: "gen", id: job.id }))}>
                <td style={{ width: 30 }}><Lamp s={jobSignal(job)} live={job.status === "running"} /></td>
                <td>
                  <div className="v3-name"><div><b>{jobSkillName(job) || "—"}</b><small>{job.id.slice(0, 12)}</small></div></div>
                </td>
                <td className="v3-sl-clip">{taskgenTarget(job, t("skillLab.taskgen.target.new"))}</td>
                <td className="num">{job.params.count ?? "—"}</td>
                <td className="mono v3-sl-clip" style={{ color: "var(--v3-text-2)" }} title={job.params.target_backend ?? "claude_code_exec"}>
                  {job.params.model ?? "—"}
                </td>
                <td><JobStatus job={job} /></td>
                <td className="num" style={{ color: "var(--v3-text-3)" }}>{ago(job.created_at)}</td>
                <td style={{ width: 1, whiteSpace: "nowrap" }} onClick={(e) => e.stopPropagation()}>
                  {isLiveJob(job) && (
                    <Btn size="sm" kind="ghost" disabled={actions.busy} onClick={() => actions.ask(job, "cancel")}>
                      {t("skillLab.taskgen.job.cancel")}
                    </Btn>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {rows.length > 8 && (
        <div style={{ padding: 12, display: "flex", justifyContent: "center" }}>
          <Btn size="sm" kind="ghost" onClick={() => setShowAll((v) => !v)}>
            {showAll ? t("v3.skilllab.showFewer") : t("v3.skilllab.showAll", { count: rows.length })}
          </Btn>
        </div>
      )}
      {actions.dialog}
    </Panel>
  );
}

export function NewButton({ tab }: { tab: "tasksets" | "eval" | "train" }) {
  const { t } = useTranslation();
  const label = tab === "eval" ? t("skillLab.eval.new") : tab === "train" ? t("skillLab.train.new") : t("skillLab.tasksets.new");
  return (
    <Link className="v3-btn primary" to={slUrl({ tab, view: "new" })}>
      <Plus size={14} /> {label}
    </Link>
  );
}
