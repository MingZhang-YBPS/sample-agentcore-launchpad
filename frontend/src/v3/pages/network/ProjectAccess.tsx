import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import { errorMessage } from "../../../lib/api";
import type { NetworkProfile } from "../../../lib/api/networkProfiles";
import { networkApi } from "../../../lib/api/networkProfiles";
import { projectsApi } from "../../../lib/api/projects";
import { useWorkspace } from "../../../workspace/workspace-context";
import { useLoad, useToast } from "../../hooks";
import { Btn, Notice, Panel, Skeleton } from "../../ui";

/**
 * The per-project allowed set (§12.3, `PUT /api/network-profiles/projects/{id}`): which
 * live profiles a project's agents may name in this workspace, and the one the wizard
 * preselects. Profiles outside the set are invisible to the project's members.
 */
export function ProjectAccess({ profiles, onSaved }: { profiles: NetworkProfile[]; onSaved: () => void }) {
  const { t } = useTranslation();
  const toast = useToast();
  const { current } = useWorkspace();
  const workspaceId = current?.id ?? "";
  const projects = useLoad(
    () => projectsApi.list({ status: "active", ...(workspaceId ? { workspace_id: workspaceId } : {}) }),
    `v3-network-projects:${workspaceId}`,
  );
  const [projectId, setProjectId] = useState("");
  const allowed = useLoad(
    async () => (projectId ? networkApi.forProject(projectId) : null),
    `v3-network-access:${workspaceId}:${projectId}`,
  );
  const [chosen, setChosen] = useState<string[]>([]);
  const [defaultId, setDefaultId] = useState<string>("");
  const [busy, setBusy] = useState(false);
  const live = profiles.filter((p) => !p.archived_at);

  useEffect(() => {
    if (!projectId && projects.data?.projects.length) setProjectId(projects.data.projects[0].id);
  }, [projectId, projects.data]);
  useEffect(() => {
    setChosen(allowed.data?.profiles.map((p) => p.id) ?? []);
    setDefaultId(allowed.data?.default_profile_id ?? "");
  }, [allowed.data]);

  const toggle = (id: string) => {
    setChosen((prev) => (prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id]));
    if (defaultId === id) setDefaultId("");
  };
  const save = async () => {
    setBusy(true);
    try {
      await networkApi.setForProject(projectId, { profile_ids: chosen, default_profile_id: defaultId || null });
      toast("ok", t("v3.network.access.saved", { project: projectId }));
      allowed.reload();
      onSaved();
    } catch (err) {
      toast("act", errorMessage(err), err);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Panel title={t("v3.network.access.title")}>
      <p className="v3-hint" style={{ marginTop: 0 }}>{t("v3.network.access.sub")}</p>
      {projects.loading && !projects.data ? <Skeleton rows={2} /> : projects.error ? <Notice s="act">{projects.error}</Notice> : (
        <div style={{ display: "grid", gap: 10 }} data-testid="v3-network-access">
          <label className="v3-field">
            <span>{t("v3.network.access.project")}</span>
            <select className="v3-select" value={projectId} onChange={(e) => setProjectId(e.target.value)}>
              {(projects.data?.projects ?? []).map((p) => <option key={p.id} value={p.id}>{p.name} ({p.id})</option>)}
            </select>
          </label>
          {allowed.error && <Notice s="act">{allowed.error}</Notice>}
          {live.length === 0 ? <small className="v3-hint">{t("v3.network.empty")}</small> : (
            <table className="v3-table">
              <thead><tr><th>{t("v3.network.access.allowed")}</th><th>{t("v3.network.col.name")}</th><th>{t("v3.network.access.default")}</th></tr></thead>
              <tbody>
                {live.map((p) => (
                  <tr key={p.id}>
                    <td style={{ width: 1 }}>
                      <input type="checkbox" aria-label={p.name} checked={chosen.includes(p.id)} onChange={() => toggle(p.id)} />
                    </td>
                    <td><b>{p.name}</b> <small className="mono">{p.id}</small></td>
                    <td style={{ width: 1 }}>
                      <input type="radio" name="v3-network-default" aria-label={t("v3.network.access.default")}
                        disabled={!chosen.includes(p.id)} checked={defaultId === p.id} onChange={() => setDefaultId(p.id)} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          <div style={{ display: "flex", gap: 8 }}>
            <Btn kind="primary" disabled={busy || !projectId} onClick={() => void save()}>{t("v3.network.access.save")}</Btn>
            {defaultId && <Btn kind="ghost" disabled={busy} onClick={() => setDefaultId("")}>{t("v3.network.access.noDefault")}</Btn>}
          </div>
        </div>
      )}
    </Panel>
  );
}
