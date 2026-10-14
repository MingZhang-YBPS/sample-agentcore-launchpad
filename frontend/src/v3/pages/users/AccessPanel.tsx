import { Pencil } from "lucide-react";
import { useTranslation } from "react-i18next";

import type { ProjectUser, UserAccess } from "../../../lib/api/users";
import { Btn, Chip, Notice, Panel, Skeleton } from "../../ui";
import { sourceGroup } from "./forms";
import { useRoleLabel } from "./structure";

/** Where one project's role comes from: "direct" / "group «name» (role)". */
function Sources({ sources, groupName }: { sources: { source: string; role_key: string }[]; groupName: (id: string) => string }) {
  const { t } = useTranslation();
  const label = useRoleLabel();
  return (
    <span style={{ display: "inline-flex", gap: 4, flexWrap: "wrap" }}>
      {sources.map((s) => {
        const group = sourceGroup(s.source);
        return (
          <Chip key={s.source} s={group ? "info" : undefined}>
            {group
              ? t("v3.users.access.fromGroup", { group: groupName(group), role: label(s.role_key) })
              : t("v3.users.access.direct", { role: label(s.role_key) })}
          </Chip>
        );
      })}
    </span>
  );
}

/**
 * One account's projects, groups and reachable workspaces (projects §7.4 "user
 * detail"): the rows from the account (`projects` with role and sources, `groups`)
 * and the effective keys from `GET /api/users/{id}/access`. An administrator
 * reaches every project by role, so there is nothing per project to show.
 */
export function AccessPanel({
  user,
  access,
  loading,
  error,
  busy,
  onEdit,
}: {
  user: ProjectUser;
  access: UserAccess | null;
  loading: boolean;
  error: string | null;
  busy?: boolean;
  onEdit?: () => void;
}) {
  const { t } = useTranslation();
  const label = useRoleLabel();
  const isAdminRole = user.role === "admin";
  const groups = user.groups ?? [];
  const groupName = (id: string) => groups.find((g) => g.id === id)?.name ?? id;
  // the effective keys of a project, wherever the account reaches it
  const keysOf = (projectId: string) =>
    access?.workspaces.flatMap((w) => w.projects).find((p) => p.id === projectId)?.permissions ?? null;
  const projects = user.projects ?? [];

  return (
    <>
      <Panel
        title={t("v3.users.access.projectsTitle")}
        end={!isAdminRole && onEdit ? <Btn size="sm" disabled={busy} onClick={onEdit}><Pencil size={13} /> {t("v3.users.memberships.open")}</Btn> : undefined}
        flush={!isAdminRole && projects.length > 0}
      >
        {isAdminRole ? (
          <span className="v3-usr-muted">{t("v3.users.access.allProjects")}</span>
        ) : projects.length === 0 ? (
          <Notice s="wait">{t("v3.users.access.noProjects")}</Notice>
        ) : (
          <table className="v3-table" data-testid="v3-user-projects-table">
            <thead>
              <tr>
                <th>{t("v3.users.access.project")}</th>
                <th>{t("v3.users.access.role")}</th>
                <th>{t("v3.users.access.source")}</th>
                <th className="num">{t("v3.users.access.keys")}</th>
              </tr>
            </thead>
            <tbody>
              {projects.map((p) => {
                const keys = keysOf(p.id);
                return (
                  <tr key={p.id}>
                    <td>
                      <div className="v3-name"><div><b>{p.name}</b><small className="mono">{p.id}</small></div></div>
                      {p.status === "archived" && <Chip>{t("v3.users.access.archived")}</Chip>}
                    </td>
                    <td>{label(p.role)}</td>
                    <td><Sources sources={p.sources} groupName={groupName} /></td>
                    <td className="num" title={keys?.join(", ")}>{keys ? keys.length : "—"}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </Panel>

      {!isAdminRole && (
        <Panel title={t("v3.users.access.groupsTitle")}>
          {groups.length === 0 ? (
            <span className="v3-usr-muted">{t("v3.users.access.noGroups")}</span>
          ) : (
            <div className="v3-usr-chips" data-testid="v3-user-groups">
              {groups.map((g) => (
                <Chip key={g.id} s={g.archived ? undefined : "info"}>
                  {g.name} · {label(g.role)}
                  {g.archived && ` · ${t("v3.users.access.archived")}`}
                </Chip>
              ))}
            </div>
          )}
        </Panel>
      )}

      <Panel title={t("v3.users.access.reachTitle")} end={<span>{t("v3.users.access.reachSub")}</span>}>
        {loading && !access ? (
          <Skeleton rows={2} />
        ) : error ? (
          <Notice s="act">{error}</Notice>
        ) : !access || access.workspaces.length === 0 ? (
          <Notice s="wait">{t("v3.users.ws.none")}</Notice>
        ) : (
          <dl className="v3-kv" data-testid="v3-user-reach">
            {access.workspaces.map((w) => (
              <div key={w.workspace_id} style={{ display: "contents" }}>
                <dt>{w.name || w.workspace_id}</dt>
                <dd>
                  {w.projects.length === 0
                    ? <span className="v3-usr-muted">{t("v3.users.access.grantOnly")}</span>
                    : t("v3.users.access.via", { projects: w.projects.map((p) => p.name).join(", ") })}
                </dd>
              </div>
            ))}
          </dl>
        )}
      </Panel>
    </>
  );
}
