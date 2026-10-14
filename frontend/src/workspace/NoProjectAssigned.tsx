import { FolderX, LogOut } from "lucide-react";
import { useTranslation } from "react-i18next";

import { useAuth } from "../auth/auth-context";
import { Panel as V2Panel, ViewHead } from "../components";
import { AnnouncementFeed } from "../components/AnnouncementFeed";
import { useUiVersion } from "../lib/ui-version";
import { V2AuthFrame } from "../v2/AuthFrame";
import { V3AuthFrame } from "../v3/AuthFrame";
import { Lamp, PageHead, Panel } from "../v3/ui";
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
 * workspace, and (V2) the installation-wide announcements — V3's announcement list
 * is private to `v3/NoWorkspace.tsx` for now.
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
