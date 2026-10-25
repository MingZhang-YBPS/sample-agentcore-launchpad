import { Link2, Star, Unlink } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";

import { ApiErrorDetail } from "../../../components/ApiErrorDetail";
import { ErrorCallout } from "../../../components/ErrorCallout";
import { api } from "../../../lib/api";
import { type BindingsPut, type ProjectBinding, type ProjectDetail, projectsApi } from "../../../lib/api/projects";
import { formatRowCounts } from "../../../lib/workspaces";
import { useProject } from "../../../workspace/project-context";
import { useFailure } from "../../failure";
import { ago } from "../../format";
import { useLoad, useToast } from "../../hooks";
import { Btn, Chip, Confirm, Empty, Notice, Panel, Skeleton } from "../../ui";
import { inUseRows } from "./model";
import type { ProjectAbilities } from "./ProjectDetail";
import { Select } from "../../select";

/**
 * §6.3 "Workspaces": the bindings; an administrator binds, unbinds and makes the
 * project a workspace's default. A department administrator (§11.1) binds to the
 * workspaces open to the project's department and unbinds; no defaults. An unbind is refused while the workspace holds
 * any of the project's rows — the refusal lists them by table.
 */
export function WorkspacesTab({
  project,
  abilities,
  onChanged,
}: {
  project: ProjectDetail;
  abilities: ProjectAbilities;
  onChanged: () => void;
}) {
  const { t } = useTranslation();
  const toast = useToast();
  const failed = useFailure();
  const [tick, setTick] = useState(0);
  const bindings = useLoad(() => projectsApi.workspaces(project.id), `v3-project-ws:${project.id}:${tick}`);
  const all = useLoad(
    () => (abilities.admin ? api.listWorkspaces() : Promise.resolve(null)),
    `v3-project-ws-all:${abilities.admin}`,
  );
  const { managedDepartments } = useProject();
  const [pick, setPick] = useState("");
  const [unbinding, setUnbinding] = useState<ProjectBinding | null>(null);
  const [refusal, setRefusal] = useState<{ ws: string; rows: Record<string, number>; err: unknown } | null>(null);
  const [busy, setBusy] = useState(false);
  const rows = bindings.data?.workspaces ?? [];
  const bound = new Set(rows.map((b) => b.workspace_id));
  // an administrator: every workspace; a department administrator: the department's open ones
  const open = managedDepartments.find((d) => d.id === project.department_id)?.workspaces ?? [];
  const free: { id: string; name: string; region: string | null }[] = abilities.admin
    ? (all.data?.workspaces ?? []).filter((w) => !bound.has(w.id))
    : open.filter((ws) => !bound.has(ws)).map((ws) => ({ id: ws, name: ws, region: null }));
  const active = project.status === "active";

  const put = async (body: BindingsPut, ok: string) => {
    setBusy(true);
    setRefusal(null);
    try {
      failed.clear();
      await projectsApi.putWorkspaces(project.id, body);
      toast("ok", ok);
      setTick((n) => n + 1);
      setPick("");
      onChanged();
    } catch (err) {
      const held = inUseRows(err);
      const ws = body.unbind?.[0] ?? "";
      if (held) setRefusal({ ws, rows: held, err });
      else failed.fail(err);
    } finally {
      setBusy(false);
      setUnbinding(null);
    }
  };

  return (
    <div style={{ display: "grid", gap: 12 }} data-testid="project-workspaces">
      {abilities.structure && active && (
        <div className="v3-prj-filters">
          <Select className="v3-input" aria-label={t("v3.projects.workspaces.pick")} value={pick} onChange={(e) => setPick(e.target.value)}>
            <option value="">{t("v3.projects.workspaces.pick")}</option>
            {free.map((w) => <option key={w.id} value={w.id}>{w.region ? `${w.name} · ${w.region}` : w.name}</option>)}
          </Select>
          <Btn kind="primary" busy={busy} disabled={!pick} disabledReason={t("story.reason.pickWorkspace")}
            onClick={() => void put({ bind: [pick] }, t("v3.projects.workspaces.bound", { workspace: free.find((w) => w.id === pick)?.name ?? pick }))}>
            <Link2 size={14} /> {t("v3.projects.workspaces.bind")}
          </Btn>
        </div>
      )}
      {refusal && (
        <Notice s="act">
          <div data-testid="unbind-refused">
            {t("v3.projects.workspaces.inUse", {
              workspace: rows.find((b) => b.workspace_id === refusal.ws)?.name ?? refusal.ws,
              rows: formatRowCounts(refusal.rows),
            })}
          </div>
          <ApiErrorDetail error={refusal.err} />
        </Notice>
      )}
      <ErrorCallout error={failed.error} onDismiss={failed.clear} data-testid="v3-project-workspaces-error" />
      <Panel flush>
        {bindings.loading && !bindings.data ? (
          <div style={{ padding: 20 }}><Skeleton rows={3} /></div>
        ) : bindings.error ? (
          <div style={{ padding: 20 }}><Notice s="act"><ApiErrorDetail error={bindings.failure ?? bindings.error} /></Notice></div>
        ) : rows.length === 0 ? (
          <Empty title={t("v3.projects.workspaces.empty")} />
        ) : (
          <table className="v3-table" data-testid="project-bindings">
            <thead>
              <tr>
                <th>{t("v3.projects.workspaces.col.name")}</th>
                <th>{t("v3.projects.workspaces.col.region")}</th>
                <th>{t("v3.projects.workspaces.col.tier")}</th>
                <th>{t("v3.projects.workspaces.col.default")}</th>
                <th className="num">{t("v3.projects.workspaces.col.enabled")}</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {rows.map((b) => (
                <tr key={b.workspace_id}>
                  <td><div className="v3-name"><div><b>{b.name}</b><small>{b.workspace_id}</small></div></div></td>
                  <td className="mono">{b.region ?? "—"}</td>
                  <td>{b.tier ? <Chip s={b.tier === "prod" ? "act" : undefined}>{b.tier}</Chip> : "—"}</td>
                  <td>{b.is_workspace_default ? <Chip s="info"><Star size={11} /> {t("v3.projects.workspaces.isDefault")}</Chip> : "—"}</td>
                  <td className="num v3-prj-muted">{ago(b.enabled_at)}</td>
                  <td style={{ width: 1, whiteSpace: "nowrap" }}>
                    {abilities.structure && (
                      <span style={{ display: "inline-flex", gap: 6 }}>
                        {abilities.admin && !b.is_workspace_default && active && (
                          <Btn size="sm" disabled={busy}
                            onClick={() => void put({ make_default: [b.workspace_id] }, t("v3.projects.workspaces.madeDefault", { workspace: b.name }))}>
                            <Star size={12} /> {t("v3.projects.workspaces.makeDefault")}
                          </Btn>
                        )}
                        {!project.is_default && (
                          <Btn size="sm" kind="danger" busy={busy} disabled={b.is_workspace_default}
                            disabledReason={t("v3.projects.workspaces.defaultFirst")}
                            onClick={() => setUnbinding(b)}>
                            <Unlink size={12} /> {t("v3.projects.workspaces.unbind")}
                          </Btn>
                        )}
                      </span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Panel>
      {project.is_default && <small className="v3-hint">{t("v3.projects.defaultLocked")}</small>}

      {unbinding && (
        <Confirm
          title={t("v3.projects.workspaces.unbindTitle", { workspace: unbinding.name })}
          confirmLabel={t("v3.projects.workspaces.unbind")}
          cancelLabel={t("v3.projects.cancel")}
          danger
          busy={busy}
          onConfirm={() => void put({ unbind: [unbinding.workspace_id] }, t("v3.projects.workspaces.unbound", { workspace: unbinding.name }))}
          onCancel={() => setUnbinding(null)}
        >
          {t("v3.projects.workspaces.unbindBody", { workspace: unbinding.name, project: project.name })}
        </Confirm>
      )}
    </div>
  );
}
