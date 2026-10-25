import { FolderX, LogOut } from "lucide-react";
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";

import { useAuth } from "../auth/auth-context";
import { Panel as V2Panel, ViewHead } from "../components";
import { AnnouncementFeed } from "../components/AnnouncementFeed";
import { projectsApi } from "../lib/api/projects";
import { useUiVersion } from "../lib/ui-version";
import { Alert as V2Alert } from "../v2/ui";
import { V2AuthFrame } from "../v2/AuthFrame";
import { V3AuthFrame } from "../v3/AuthFrame";
import { V3Announcements } from "../v3/NoWorkspace";
import { bindHref, showsBindBanner } from "../v3/pages/projects/model";
import { Lamp, Notice, PageHead, Panel } from "../v3/ui";
import { useProject } from "./project-context";
import { useWorkspace } from "./workspace-context";

/** Another reachable workspace may have projects: the way out besides signing out. */
function WorkspacePicker({ skin }: { skin: "v2" | "v3" }) {
  const { t } = useTranslation();
  const { workspaces, current, select } = useWorkspace();
  if (workspaces.length < 2) return null;
  return (
    <label className={`${skin}-field`} data-testid="no-project-workspace">
      <span>{t("v3.project.none.switchWorkspace")}</span>
      <select className={`${skin}-input`} value={current?.id ?? ""} onChange={(e) => select(e.target.value)}>
        {workspaces.map((ws) => (
          <option key={ws.id} value={ws.id}>
            {ws.name} · {ws.region}
          </option>
        ))}
      </select>
    </label>
  );
}

/**
 * Projects design §6.2: a member who can enter this workspace (a legacy grant) but
 * belongs to none of its projects — every project-scoped route would answer 403
 * `project.none`, so the console says who can fix it instead. Modelled on
 * `NoWorkspaceGranted`: the sign-in chrome, signing out, switching to another
 * workspace, and the installation-wide announcements.
 */
export function NoProjectAssigned({ workspaceName }: { workspaceName: string }) {
  const { t } = useTranslation();
  const { authRequired, username, logout } = useAuth();
  const skin = useUiVersion() === "v3" ? "v3" : "v2";
  const logoutButton = authRequired ? (
    <button type="button" className={`${skin}-btn sm`} onClick={() => void logout()} data-testid="no-project-logout">
      <LogOut size={13} aria-hidden="true" />
      {t("auth.logout")}
    </button>
  ) : null;
  const body = t("v3.project.none.body", { workspace: workspaceName });
  const meta = t("v3.project.none.meta", { username: username ?? "—" });

  if (skin === "v3") {
    return (
      <V3AuthFrame testId="no-project-page" end={logoutButton}>
        <div className="v3-nows">
          <PageHead eyebrow={t("v3.project.none.eyebrow")} title={t("v3.project.none.title")} sub={meta} />
          <Panel signal="wait">
            <div className="v3-nows-state" data-testid="no-project-body">
              <span className="ico"><FolderX size={22} aria-hidden="true" /></span>
              <div>
                <b><Lamp s="wait" live /> {t("v3.project.none.waiting")}</b>
                <p>{body}</p>
                <WorkspacePicker skin="v3" />
              </div>
            </div>
          </Panel>
          <V3Announcements />
        </div>
      </V3AuthFrame>
    );
  }
  return (
    <V2AuthFrame testId="no-project-page" narrow end={logoutButton}>
      <div className="v2-auth-narrow v2-classic">
        <ViewHead kicker={t("v3.project.none.eyebrow")} title={t("v3.project.none.title")} meta={meta} />
        <V2Panel brk>
          <div className="empty" data-testid="no-project-body">
            {body}
            <WorkspacePicker skin="v2" />
          </div>
        </V2Panel>
        <AnnouncementFeed />
      </div>
    </V2AuthFrame>
  );
}

/**
 * Projects design §6.2, the administrator's side: the current workspace binds no
 * active project, so every business page is empty — say so above the page, with
 * the way to fix it (bind an existing project, or create one there). The access
 * list's "nothing here" is confirmed against `GET /api/projects?workspace_id=`
 * first, so a failed access read never shows the banner. Renders nothing otherwise.
 */
export function NoBoundProjectBanner() {
  const { t } = useTranslation();
  const { isAdmin } = useAuth();
  const { current: workspace } = useWorkspace();
  const project = useProject();
  const skin = useUiVersion() === "v3" ? "v3" : "v2";
  const workspaceId = workspace?.id ?? null;
  const suspect = showsBindBanner(isAdmin, workspaceId, project);
  const [confirmed, setConfirmed] = useState<string | null>(null);
  useEffect(() => {
    if (!suspect || !workspaceId) return;
    let live = true;
    projectsApi
      .list({ workspace_id: workspaceId, status: "active" })
      .then((res) => live && setConfirmed(res.projects.length === 0 ? workspaceId : null))
      .catch(() => live && setConfirmed(null));
    return () => {
      live = false;
    };
  }, [suspect, workspaceId]);
  if (!suspect || !workspaceId || confirmed !== workspaceId) return null;

  const text = t("v3.projects.banner.text", { workspace: workspace?.name ?? workspaceId });
  const link = (
    <Link to={bindHref(workspaceId)} className={`${skin}-btn sm`} data-testid="no-bound-project-link">
      {t("v3.projects.banner.link")}
    </Link>
  );
  if (skin === "v3") {
    return (
      <div data-testid="no-bound-project" style={{ marginBottom: 16 }}>
        <Notice s="wait">
          <span style={{ display: "inline-flex", gap: 12, alignItems: "center", flexWrap: "wrap" }}>{text} {link}</span>
        </Notice>
      </div>
    );
  }
  return (
    <div data-testid="no-bound-project" style={{ marginBottom: 16 }}>
      <V2Alert tone="warn" action={link}>{text}</V2Alert>
    </div>
  );
}
