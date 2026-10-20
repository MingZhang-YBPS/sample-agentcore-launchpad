import { useState } from "react";
import { useTranslation } from "react-i18next";

import { ApiErrorDetail } from "../../../components/ApiErrorDetail";
import { movableAgents, projectAgents } from "../../../lib/api/project-transfer";
import { type ProjectDetail, projectsApi } from "../../../lib/api/projects";
import { formatRowCounts } from "../../../lib/workspaces";
import { useProject } from "../../../workspace/project-context";
import { useLoad, useToast } from "../../hooks";
import { Reasoned } from "../../reason";
import { Btn, Dialog, Notice } from "../../ui";
import { Select } from "../../select";

/**
 * Move one agent — with every row derived from it — to another project bound to
 * the same workspace (§7.10 `transfer`, admin-only; phase 1 rewrites the ledger
 * only). Used to split real projects out of the default one.
 */
export function TransferDialog({
  project,
  onClose,
  onDone,
}: {
  project: ProjectDetail;
  onClose: () => void;
  onDone: () => void;
}) {
  const { t } = useTranslation();
  const toast = useToast();
  const { refresh } = useProject();
  const [workspaceId, setWorkspaceId] = useState(project.workspaces[0]?.workspace_id ?? "");
  const [agentId, setAgentId] = useState("");
  const [target, setTarget] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const agents = useLoad(
    () => (workspaceId ? projectAgents(project.id, workspaceId) : Promise.resolve([])),
    `v3-project-transfer-agents:${project.id}:${workspaceId}`,
  );
  const targets = useLoad(
    () => (workspaceId ? projectsApi.list({ workspace_id: workspaceId, status: "active" }) : Promise.resolve({ projects: [] })),
    `v3-project-transfer-targets:${workspaceId}`,
  );
  const movable = movableAgents(agents.data ?? []);
  const others = (targets.data?.projects ?? []).filter((p) => p.id !== project.id);

  const pickWorkspace = (ws: string) => {
    setWorkspaceId(ws);
    setAgentId("");
    setTarget("");
  };
  const submit = async () => {
    setBusy(true);
    setError(null);
    try {
      const result = await projectsApi.transfer(project.id, { resource_id: agentId, to_project_id: target });
      const agent = movable.find((a) => a.id === agentId);
      toast("ok", t("v3.projects.transfer.done", {
        agent: agent?.display_name || agent?.name || agentId,
        project: others.find((p) => p.id === target)?.name ?? target,
        rows: formatRowCounts(result.rows),
      }));
      await refresh();
      onDone();
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Dialog
      wide
      title={t("v3.projects.transfer.title", { name: project.name })}
      onClose={onClose}
      foot={
        <>
          <Btn kind="ghost" onClick={onClose} disabled={busy}>{t("v3.projects.cancel")}</Btn>
          <Btn kind="primary" onClick={() => void submit()} busy={busy} disabled={!agentId || !target}
            disabledReason={!agentId ? t("story.reason.pickAgent") : t("story.reason.pickProject")}>
            {t("v3.projects.transfer.submit")}
          </Btn>
        </>
      }
    >
      <div className="v3-prj-form" data-testid="project-transfer">
        <p style={{ margin: 0 }}>{t("v3.projects.transfer.body")}</p>
        <label className="v3-field">
          <span>{t("v3.projects.transfer.workspace")}</span>
          <Select className="v3-input" value={workspaceId} onChange={(e) => pickWorkspace(e.target.value)}>
            {project.workspaces.map((w) => <option key={w.workspace_id} value={w.workspace_id}>{w.name}</option>)}
          </Select>
        </label>
        <label className="v3-field">
          <span>{t("v3.projects.transfer.agent")}</span>
          <Reasoned disabledReason={agents.loading ? null : t("v3.projects.transfer.noAgents")}>
            <Select className="v3-input" value={agentId} onChange={(e) => setAgentId(e.target.value)} disabled={movable.length === 0}>
              <option value="">{agents.loading ? "…" : movable.length === 0 ? t("v3.projects.transfer.noAgents") : "—"}</option>
              {movable.map((a) => <option key={a.id} value={a.id}>{a.display_name || a.name} · {a.status}</option>)}
            </Select>
          </Reasoned>
        </label>
        <label className="v3-field">
          <span>{t("v3.projects.transfer.target")}</span>
          <Reasoned disabledReason={targets.loading ? null : t("v3.projects.transfer.noTargets")}>
            <Select className="v3-input" value={target} onChange={(e) => setTarget(e.target.value)} disabled={others.length === 0}>
              <option value="">{targets.loading ? "…" : others.length === 0 ? t("v3.projects.transfer.noTargets") : "—"}</option>
              {others.map((p) => <option key={p.id} value={p.id}>{p.name} ({p.id})</option>)}
            </Select>
          </Reasoned>
        </label>
        {agents.error && <Notice s="act"><ApiErrorDetail error={agents.failure ?? agents.error} /></Notice>}
        {error != null && <Notice s="act"><ApiErrorDetail error={error} /></Notice>}
      </div>
    </Dialog>
  );
}
