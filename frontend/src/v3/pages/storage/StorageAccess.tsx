import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import { ErrorCallout } from "../../../components/ErrorCallout";
import { projectsApi } from "../../../lib/api/projects";
import { type StorageEntry, storageApi } from "../../../lib/api/storage";
import { useWorkspace } from "../../../workspace/workspace-context";
import { useFailure } from "../../failure";
import { useLoad, useToast } from "../../hooks";
import { Btn, Notice, Panel, Skeleton } from "../../ui";

/**
 * The per-project allowed set (storage-mounts §4.1, `PUT /api/storage-mounts/projects/{id}`):
 * which live entries a project's agents may mount in this workspace. Entries outside
 * the set are invisible to the project's members.
 */
export function StorageAccess({ entries, onSaved }: { entries: StorageEntry[]; onSaved: () => void }) {
  const { t } = useTranslation();
  const toast = useToast();
  const failed = useFailure();
  const { current } = useWorkspace();
  const workspaceId = current?.id ?? "";
  const projects = useLoad(
    () => projectsApi.list({ status: "active", ...(workspaceId ? { workspace_id: workspaceId } : {}) }),
    `v3-storage-projects:${workspaceId}`,
  );
  const [projectId, setProjectId] = useState("");
  const allowed = useLoad(
    async () => (projectId ? storageApi.forProject(projectId) : null),
    `v3-storage-access:${workspaceId}:${projectId}`,
  );
  const [chosen, setChosen] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const live = entries.filter((e) => !e.archived_at);

  useEffect(() => {
    if (!projectId && projects.data?.projects.length) setProjectId(projects.data.projects[0].id);
  }, [projectId, projects.data]);
  useEffect(() => {
    setChosen(allowed.data?.storage.map((e) => e.id) ?? []);
  }, [allowed.data]);

  const toggle = (id: string) => setChosen((prev) => (prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id]));
  const save = async () => {
    setBusy(true);
    try {
      failed.clear();
      await storageApi.setForProject(projectId, chosen);
      toast("ok", t("v3.storage.access.saved", { project: projectId }));
      allowed.reload();
      onSaved();
    } catch (err) {
      failed.fail(err);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Panel title={t("v3.storage.access.title")}>
      <p className="v3-hint" style={{ marginTop: 0 }}>{t("v3.storage.access.sub")}</p>
      <ErrorCallout error={failed.error} onDismiss={failed.clear} data-testid="v3-storage-access-error" />
      {projects.loading && !projects.data ? <Skeleton rows={2} /> : projects.error ? <Notice s="act">{projects.error}</Notice> : (
        <div style={{ display: "grid", gap: 10 }} data-testid="v3-storage-access">
          <label className="v3-field">
            <span>{t("v3.storage.access.project")}</span>
            <select className="v3-select" value={projectId} onChange={(e) => setProjectId(e.target.value)}>
              {(projects.data?.projects ?? []).map((p) => <option key={p.id} value={p.id}>{p.name} ({p.id})</option>)}
            </select>
          </label>
          {allowed.error && <Notice s="act">{allowed.error}</Notice>}
          {live.length === 0 ? <small className="v3-hint">{t("v3.storage.empty")}</small> : (
            <table className="v3-table">
              <thead><tr><th>{t("v3.storage.access.allowed")}</th><th>{t("v3.storage.col.name")}</th></tr></thead>
              <tbody>
                {live.map((e) => (
                  <tr key={e.id}>
                    <td style={{ width: 1 }}>
                      <input type="checkbox" aria-label={e.name} checked={chosen.includes(e.id)} onChange={() => toggle(e.id)} />
                    </td>
                    <td><b>{e.name}</b> <small className="mono">{e.id}</small></td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          <div><Btn kind="primary" disabled={busy || !projectId} onClick={() => void save()}>{t("v3.storage.access.save")}</Btn></div>
        </div>
      )}
    </Panel>
  );
}
