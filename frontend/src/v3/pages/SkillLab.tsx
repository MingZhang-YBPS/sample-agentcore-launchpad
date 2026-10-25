import "../../v2/pages/skilllab/skilllab.css";
import "./skilllab.css";

import { useMemo } from "react";
import { useTranslation } from "react-i18next";
import { Navigate, useNavigate, useSearchParams } from "react-router-dom";

import { api, type SkillLabJobInfo } from "../../lib/api";
import { elapsed, jobSkillName } from "../../lib/skillLab";
import { EvalWizard } from "../../v2/pages/skilllab/EvalWizard";
import { TaskgenDetail } from "../../v2/pages/skilllab/TaskgenDetail";
import { TaskgenWizard } from "../../v2/pages/skilllab/TaskgenWizard";
import { TasksetDetail } from "../../v2/pages/skilllab/TasksetDetail";
import { TasksetEditor } from "../../v2/pages/skilllab/TasksetEditor";
import { TrainDetail } from "../../v2/pages/skilllab/TrainDetail";
import { TrainWizard } from "../../v2/pages/skilllab/TrainWizard";
import { type Tab, TABS, useJobList, useTasksets } from "../../v2/pages/skilllab/state";
import { ago } from "../format";
import { useLoad } from "../hooks";
import { Chip, Filters, Lamp, Notice, PageHead, Panel, Stat } from "../ui";
import { EvalView } from "./skilllab/EvalView";
import { Hosted } from "./skilllab/Hosted";
import { JobStatus, JobTable, NewButton, TaskgenTable, TasksetTable } from "./skilllab/Lists";
import { jobSignal, needsLook, slUrl } from "./skilllab/signal";

/** Where a job's detail lives (taskgen sits under the task-set tab). */
const jobUrl = (job: SkillLabJobInfo) =>
  job.type === "taskgen" ? slUrl({ tab: "tasksets", view: "gen", id: job.id }) : slUrl({ tab: job.type, view: "detail", id: job.id });

function Landing({ tab }: { tab: Tab }) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const [, setParams] = useSearchParams();
  const status = useLoad(() => api.skillLabStatus().catch(() => null), "v3-sl-status");
  const tasksets = useTasksets("v3-sl-tasksets");
  // all three lists, polled as V2 polls each (jobs finish on their own): the
  // attention strip needs every type at once
  const evals = useJobList("eval");
  const trains = useJobList("train");
  const gens = useJobList("taskgen");
  const all = useMemo(() => [...evals.rows, ...trains.rows, ...gens.rows], [evals.rows, trains.rows, gens.rows]);
  const attention = all
    .filter(needsLook)
    .sort((a, b) => (b.created_at ?? "").localeCompare(a.created_at ?? ""))
    .slice(0, 6);
  const live = all.filter((j) => j.status === "queued" || j.status === "running").length;
  const broken = all.filter((j) => j.status === "failed" || j.status === "interrupted").length;
  const st = status.data;
  const sets = tasksets.data ?? [];

  return (
    <div className="v3-reveal v3-sl" style={{ display: "grid", gap: 16 }}>
      <PageHead eyebrow={t("v3.skilllab.eyebrow")} title={t("skillLab.title")} sub={t("v3.skilllab.sub")} end={<NewButton tab={tab} />} />

      {st && !(st.provisioned && st.venv_ready) && (
        <Notice s="wait">
          {!st.provisioned && <div>{t("skillLab.unprovisioned.body")}</div>}
          {st.missing.length > 0 && <div className="mono" style={{ opacity: 0.8 }}>{st.missing.join(" · ")}</div>}
          {!st.venv_ready && <div>{t("skillLab.unprovisioned.venv")}</div>}
        </Notice>
      )}

      <div className="v3-grid c4">
        <Panel>
          <Stat label={t("v3.skilllab.tasksets")} value={tasksets.data ? sets.length : "—"}
            foot={t("v3.skilllab.tasksetsFoot", { count: sets.reduce((n, s) => n + Object.values(s.counts).reduce((a, b) => a + b, 0), 0) })} />
        </Panel>
        <Panel signal={live ? "wait" : undefined}>
          <Stat label={t("v3.skilllab.running")} value={live} signal={live ? "wait" : undefined} foot={t("v3.skilllab.runningFoot")} />
        </Panel>
        <Panel signal={broken ? "act" : undefined}>
          <Stat label={t("v3.skilllab.broken")} value={broken} signal={broken ? "act" : undefined} foot={t("v3.skilllab.brokenFoot")} />
        </Panel>
        <Panel>
          <Stat label={t("v3.skilllab.runs")} value={evals.rows.length + trains.rows.length}
            foot={t("v3.skilllab.runsFoot", { evals: evals.rows.length, trains: trains.rows.length })} />
        </Panel>
      </div>

      {attention.length > 0 && (
        <Panel title={t("v3.skilllab.attention")} signal={broken ? "act" : "wait"} flush end={<span className="mono">{attention.length}</span>}>
          <table className="v3-table">
            <tbody>
              {attention.map((job) => (
                <tr key={job.id} className="click" onClick={() => navigate(jobUrl(job))}>
                  <td style={{ width: 30 }}><Lamp s={jobSignal(job)} live={job.status === "running"} /></td>
                  <td><Chip>{t(`v3.skilllab.type.${job.type}`)}</Chip></td>
                  <td>
                    <div className="v3-name">
                      <div>
                        <b>{jobSkillName(job) || t("skillLab.eval.unknownSkill")}</b>
                        <small className="v3-sl-clip">{job.error ? job.error.split("\n")[0].slice(0, 120) : job.progress || job.taskset_name}</small>
                      </div>
                    </div>
                  </td>
                  <td><JobStatus job={job} /></td>
                  <td className="num" style={{ color: "var(--v3-text-3)" }}>{job.started_at ? elapsed(job) : ago(job.created_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </Panel>
      )}

      <Filters
        value={tab}
        onChange={(next) => setParams({ tab: next })}
        options={TABS.map((value) => ({
          value,
          label: t(`v2.skillLab.tab.${value}`),
          count: value === "tasksets" ? sets.length : value === "eval" ? evals.rows.length : trains.rows.length,
        }))}
      />

      {tab === "tasksets" && (
        <>
          <TasksetTable rows={sets} loading={tasksets.loading} error={tasksets.error} reload={tasksets.reload} />
          <TaskgenTable rows={gens.rows} loading={gens.loading} error={gens.error} reload={() => void gens.reload()} />
        </>
      )}
      {tab === "eval" && <JobTable key="eval" type="eval" rows={evals.rows} loading={evals.loading} error={evals.error} reload={() => void evals.reload()} />}
      {tab === "train" && <JobTable key="train" type="train" rows={trains.rows} loading={trains.loading} error={trains.error} reload={() => void trains.reload()} />}
    </div>
  );
}

/**
 * 技能实验室 — evaluate and optimize Agent Skills against owned task sets. Same
 * URL states as V2 (`?tab=tasksets|eval|train&view=new|edit|detail|gen-new|gen
 * [&id=][&record=][&taskset=]`). The landing and an evaluation run are V3
 * pages; the authoring forms, the optimization run and the task-set / generation
 * views are V2's, rendered in place on the V3 theme so they keep their params here.
 */
export function V3SkillLab() {
  const [params] = useSearchParams();
  const rawTab = params.get("tab");
  const tab: Tab = (TABS as string[]).includes(rawTab ?? "") ? (rawTab as Tab) : "tasksets";
  const view = params.get("view");
  const id = params.get("id");
  const status = useLoad(() => api.skillLabStatus().catch(() => null), "v3-sl-status-views");

  // the former AI-generation tab lives inside the task-set tab (as in V2)
  if (rawTab === "taskgen") {
    const next: Record<string, string> = { tab: "tasksets" };
    if (view === "new") next.view = "gen-new";
    else if (view === "detail" && id) Object.assign(next, { view: "gen", id });
    return <Navigate to={slUrl(next)} replace />;
  }
  if (tab === "eval" && view === "detail" && id) return <EvalView key={id} id={id} />;

  const hosted = (() => {
    if (tab === "tasksets") {
      if (view === "gen-new") return <TaskgenWizard status={status.data} />;
      if (view === "gen" && id) return <TaskgenDetail key={id} id={id} />;
      if (view === "new") return <TasksetEditor key="new" id={null} />;
      if (view === "edit" && id) return <TasksetEditor key={`edit:${id}`} id={id} />;
      if (view === "detail" && id) return <TasksetDetail key={id} id={id} />;
    }
    if (tab === "eval" && view === "new") return <EvalWizard key={`${params.get("record")}:${params.get("taskset")}`} status={status.data} />;
    if (tab === "train") {
      if (view === "new") return <TrainWizard key={`${params.get("record")}:${params.get("taskset")}`} status={status.data} />;
      if (view === "detail" && id) return <TrainDetail key={id} id={id} />;
    }
    return null;
  })();
  if (hosted) return <Hosted>{hosted}</Hosted>;
  return <Landing tab={tab} />;
}
