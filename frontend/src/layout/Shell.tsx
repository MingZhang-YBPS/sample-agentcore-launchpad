import { useTranslation } from "react-i18next";
import { matchPath, Outlet, useLocation } from "react-router-dom";

import { scopeKey, useProject } from "../workspace/project-context";
import { useWorkspace } from "../workspace/workspace-context";
import { NAV_ENTRIES, navEntryFor, ROUTE_PATHS } from "./nav";
import { RouteChunk } from "./RouteChunk";
import { Sidebar } from "./Sidebar";
import { Topbar } from "./Topbar";
import { useHealth } from "./useHealth";

function crumbKeyFor(pathname: string): string {
  // An unrouted path (typo, stale bookmark) renders the catch-all NotFound
  // view; label it as such instead of the nearest prefix match's module.
  if (!ROUTE_PATHS.some((pattern) => matchPath(pattern, pathname))) return "nav.notFound";
  // longest prefix wins: /create/assistant is the assistant, /create/studio is
  // Agent management
  return (navEntryFor(pathname) ?? NAV_ENTRIES[0]).labelKey;
}

export function Shell() {
  const location = useLocation();
  const { t } = useTranslation();
  const { health, status: healthStatus } = useHealth();
  const { current } = useWorkspace();
  const { current: project } = useProject();

  return (
    <>
      <Topbar
        crumbKey={crumbKeyFor(location.pathname)}
        health={health}
        healthStatus={healthStatus}
      />
      <div className="layout">
        <Sidebar health={health} />
        <main>
          {/* Workspace-bound pages refetch on a workspace or project switch. Announcement drafts
              belong to the installation and must survive a workspace switch. */}
          <div className="view" key={
            location.pathname === "/announcements" ? "announcements" : scopeKey(current?.id, project?.id)
          }>
            {/* Pages are lazily loaded (see App.tsx): the pending line and the
                chunk-load failure state belong inside the content area, and the
                boundary is keyed on the route so navigating away clears a
                previous page's failure. `?view=` sub-pages keep the same key. */}
            <RouteChunk key={location.pathname}>
              <Outlet />
            </RouteChunk>
            <footer>
              {t("footer.phase")}
              <span className="sep">|</span>
              {t("footer.payments")}
              <span className="sep">|</span>
              {t("footer.palette")}
            </footer>
          </div>
        </main>
      </div>
    </>
  );
}
