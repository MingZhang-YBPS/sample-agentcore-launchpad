import { useTranslation } from "react-i18next";

import { useDeployProgress } from "../../../lib/agent-admin/deploy";
import type { AgentInfo, DeploymentInfo, JobInfo, StageInfo } from "../../../lib/api";
import { Chip, Empty, Lamp, Notice, Panel, type Signal } from "../../ui";

const STAGE_SIGNAL: Record<StageInfo["status"], Signal> = {
  succeeded: "ok",
  running: "wait",
  failed: "act",
  pending: "off",
  skipped: "off",
};

function Progress({ deployment, job, method }: { deployment: DeploymentInfo; job: JobInfo | null; method: string }) {
  const { t } = useTranslation();
  const { failed, inFlight, succeeded, elapsed, progressKey } = useDeployProgress(deployment, method);
  return (
    <div style={{ display: "grid", gap: 12, padding: 16 }} data-testid="v3-agent-deploy-progress">
      {inFlight && (
        <Notice s="wait">
          {t(`v3.agent.progress.line.${progressKey}`)}
          {elapsed && <span className="mono" style={{ marginLeft: 10 }}>{t("v3.agent.progress.elapsed", { time: elapsed })}</span>}
        </Notice>
      )}
      {succeeded && elapsed && <span className="v3-hint">{t("v3.agent.progress.done", { time: elapsed })}</span>}
      {failed && <Notice s="act">{t("v3.agent.progress.failed")}</Notice>}
      {/* technical detail is collapsed unless the deploy failed */}
      <details key={failed ? "failed" : "ok"} open={failed}>
        <summary style={{ cursor: "pointer", color: "var(--v3-text-2)" }}>{t("v3.agent.progress.technical")}</summary>
        <div style={{ display: "grid", gap: 10, marginTop: 10 }}>
          {job?.error && <Notice s="act">{job.error}</Notice>}
          <div className="v3-hint">{t("v3.agent.progress.log")}</div>
          <pre className="v3-pre" data-testid="v3-agent-deploy-log">
            {job?.events.length
              ? job.events.map((e) => `${e.ts.slice(11, 19)}  ${e.stage.padEnd(9)} ${e.msg}`).join("\n")
              : t("v3.agent.progress.logEmpty")}
          </pre>
        </div>
      </details>
    </div>
  );
}

/** The latest deploy: its stages, the live progress while it runs, and the job log. */
export function DeployCard({ agent, job }: { agent: AgentInfo; job: JobInfo | null }) {
  const { t } = useTranslation();
  const deploy = agent.deployments?.[0] ?? agent.deployment;
  return (
    <Panel
      title={t("v3.agent.deploy")}
      end={deploy?.job_id ? <span className="mono">job {deploy.job_id.slice(0, 8)}</span> : undefined}
      flush
    >
      {!deploy ? (
        <Empty title={t("v3.agent.noDeploy")} />
      ) : (
        <>
          <table className="v3-table v3-deploy-stages">
            <tbody>
              {deploy.stages.map((st) => (
                <tr key={st.name}>
                  <td style={{ width: 30 }}><Lamp s={STAGE_SIGNAL[st.status]} live={st.status === "running"} /></td>
                  <td className="mono" style={{ width: 120 }}>{st.name}</td>
                  <td><Chip s={STAGE_SIGNAL[st.status]}>{t(`v3.ux.stage.${st.status}`)}</Chip></td>
                  <td style={{ color: "var(--v3-text-2)" }}>{st.detail || "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <Progress deployment={deploy} job={job} method={agent.method} />
        </>
      )}
    </Panel>
  );
}
