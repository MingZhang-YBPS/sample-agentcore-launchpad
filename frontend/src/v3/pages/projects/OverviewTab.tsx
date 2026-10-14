import { useTranslation } from "react-i18next";

import { useAuth } from "../../../auth/auth-context";
import { type ProjectDetail, projectsApi } from "../../../lib/api/projects";
import { ago } from "../../format";
import { useLoad } from "../../hooks";
import { Chip, Panel, Stat } from "../../ui";
import { projectOwners } from "./model";
import { useStructureNames } from "./useStructureNames";

/** Counts, bindings, owners (§6.3 "Overview"). */
export function OverviewTab({ project }: { project: ProjectDetail }) {
  const { t } = useTranslation();
  const { isAdmin } = useAuth();
  const names = useStructureNames(isAdmin);
  const members = useLoad(() => projectsApi.members(project.id), `v3-project-overview:${project.id}:${project.member_count}`);
  const owners = projectOwners(members.data?.members ?? []);

  return (
    <div style={{ display: "grid", gap: 16 }} data-testid="project-overview">
      <div className="v3-grid c4">
        <Panel><Stat label={t("v3.projects.overview.members")} value={project.member_count} /></Panel>
        <Panel><Stat label={t("v3.projects.overview.agents")} value={project.agent_count} /></Panel>
        <Panel><Stat label={t("v3.projects.overview.workspaces")} value={project.workspaces.length} /></Panel>
        <Panel><Stat label={t("v3.projects.overview.myKeys")} value={project.my_permissions.length} /></Panel>
      </div>
      <div className="v3-grid c2">
        <Panel title={t("v3.projects.overview.about")}>
          <dl className="v3-prj-dl">
            <dt>{t("v3.projects.create.department")}</dt>
            <dd>{names.department(project.department_id) ?? "—"}</dd>
            <dt>{t("v3.projects.create.group")}</dt>
            <dd>{names.group(project.group_id) ?? "—"}</dd>
            <dt>{t("v3.projects.overview.createdBy")}</dt>
            <dd>{project.created_by_name ?? "—"}{project.created_at && <span className="v3-prj-muted"> · {ago(project.created_at)}</span>}</dd>
            {project.archived_at && (
              <>
                <dt>{t("v3.projects.overview.archivedAt")}</dt>
                <dd>{ago(project.archived_at)}</dd>
              </>
            )}
            <dt>{t("v3.projects.create.description")}</dt>
            <dd style={{ whiteSpace: "pre-wrap" }}>{project.description || "—"}</dd>
          </dl>
        </Panel>
        <Panel title={t("v3.projects.overview.owners")}>
          {owners.length === 0 ? (
            <span className="v3-prj-muted">{members.loading ? "…" : t("v3.projects.overview.noOwners")}</span>
          ) : (
            <span className="v3-prj-chips">
              {owners.map((m) => <Chip key={m.user_id}>{m.username ?? m.user_id}</Chip>)}
            </span>
          )}
          <div style={{ marginTop: 14 }}>
            <div className="v3-eyebrow" style={{ marginBottom: 6 }}>{t("v3.projects.overview.workspaces")}</div>
            <span className="v3-prj-chips">
              {project.workspaces.map((w) => (
                <Chip key={w.workspace_id} s={w.is_workspace_default ? "info" : undefined}
                  title={w.is_workspace_default ? t("v3.projects.workspaces.isDefault") : undefined}>
                  {w.name}{w.region ? ` · ${w.region}` : ""}
                </Chip>
              ))}
            </span>
          </div>
        </Panel>
      </div>
    </div>
  );
}
