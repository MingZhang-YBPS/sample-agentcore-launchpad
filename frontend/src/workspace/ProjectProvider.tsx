import { type ReactNode, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { useLocation, useNavigate } from "react-router-dom";

import { AuthContext, projectCan, useAuth } from "../auth/auth-context";
import { AUTH_UNAUTHORIZED_EVENT } from "../lib/api";
import { type MyAccess, projectsApi } from "../lib/api/projects";
import { isSteward } from "../lib/api/steward";
import { useUiVersion } from "../lib/ui-version";
import {
  PROJECT_INVALID_EVENT,
  type ProjectInvalidDetail,
  storedProjectId,
  storeProjectId,
} from "../lib/workspace-header";
import { V2AuthLoading } from "../v2/AuthFrame";
import { V3AuthLoading } from "../v3/AuthFrame";
import { NoProjectAssigned } from "./NoProjectAssigned";
import { isSwitchable, ProjectContext, readProjectLink, resolveProject } from "./project-context";
import { useWorkspace } from "./workspace-context";

interface AccessState {
  status: "loading" | "ready" | "error";
  data: MyAccess | null;
}

/** A refusal re-reads the access list at most this often: a backend that keeps
 *  refusing what it just listed must not turn every page request into a refetch. */
const REFETCH_COOLDOWN_MS = 3000;

/**
 * Owns the project selection inside the current workspace (projects design §2.6,
 * §6.1). Nested in `WorkspaceProvider`; reads `GET /api/me/access` once and keeps
 * one remembered project per workspace (`launchpad_project:<workspace_id>`), which
 * the fetch wrapper stamps as `X-Project`.
 *
 * Like the workspace, the selection is written to storage during render, before
 * the children render: a child's mount effect fires before this provider's, and an
 * unstamped request is a 400 `project.header_required` for a member with several
 * projects and no workspace default.
 *
 * It also re-provides the auth context with a `can()` that reads the current
 * project's effective keys, so the console's permission checks need no change.
 */
export function ProjectProvider({ children }: { children: ReactNode }) {
  const auth = useAuth();
  const { current: workspace } = useWorkspace();
  const workspaceId = workspace?.id ?? null;
  const location = useLocation();
  const navigate = useNavigate();
  const [access, setAccess] = useState<AccessState>({ status: "loading", data: null });
  // an explicit choice (the switcher, a deep link) for one workspace
  const [picked, setPicked] = useState<{ workspaceId: string; projectId: string } | null>(null);
  // the id the backend just refused, skipped until the list is re-read
  const [refused, setRefused] = useState<{ workspaceId: string; projectId: string } | null>(null);
  const lastFetch = useRef(0);
  const askedFor = useRef<string | null>(null);

  const refresh = useCallback(async () => {
    lastFetch.current = Date.now();
    try {
      const data = await projectsApi.myAccess();
      setAccess({ status: "ready", data });
      setRefused(null);
    } catch {
      // No list is no switcher, not a dead console: requests go out with whatever
      // is remembered (or none — the backend's default), `can()` stays global.
      setAccess((prev) => ({ status: prev.data ? "ready" : "error", data: prev.data }));
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  useEffect(() => {
    const onUnauthorized = () => setAccess({ status: "loading", data: null });
    window.addEventListener(AUTH_UNAUTHORIZED_EVENT, onUnauthorized);
    return () => window.removeEventListener(AUTH_UNAUTHORIZED_EVENT, onUnauthorized);
  }, []);

  // The fetch wrapper already cleared the stored id; forget the explicit choice,
  // skip the refused id and re-read who may work where.
  useEffect(() => {
    const onInvalid = (event: Event) => {
      const detail = (event as CustomEvent<ProjectInvalidDetail>).detail;
      if (!detail) return;
      if (detail.projectId) setRefused({ workspaceId: detail.workspaceId, projectId: detail.projectId });
      setPicked((prev) => (prev?.workspaceId === detail.workspaceId ? null : prev));
      if (Date.now() - lastFetch.current >= REFETCH_COOLDOWN_MS) void refresh();
    };
    window.addEventListener(PROJECT_INVALID_EVENT, onInvalid);
    return () => window.removeEventListener(PROJECT_INVALID_EVENT, onInvalid);
  }, [refresh]);

  // A workspace the list does not know yet (just registered or bound): re-read once.
  const entry = access.data?.workspaces.find((w) => w.workspace_id === workspaceId);
  useEffect(() => {
    if (access.status !== "ready" || !workspaceId || entry || askedFor.current === workspaceId) return;
    askedFor.current = workspaceId;
    void refresh();
  }, [access.status, entry, refresh, workspaceId]);

  // `?project=<id>` (a shared page link) wins over the remembered project; the
  // parameter is consumed so a later switch or reload does not re-apply it.
  useEffect(() => {
    const link = readProjectLink(location.search);
    if (!link.present) return;
    if (link.projectId && workspaceId) setPicked({ workspaceId, projectId: link.projectId });
    navigate({ pathname: location.pathname, search: link.rest, hash: location.hash }, { replace: true });
  }, [location.hash, location.pathname, location.search, navigate, workspaceId]);

  const projects = useMemo(() => entry?.projects ?? [], [entry]);
  const managedDepartments = useMemo(() => access.data?.managed_departments ?? [], [access.data]);
  const current = useMemo(
    () =>
      resolveProject(
        entry,
        [picked?.workspaceId === workspaceId ? picked?.projectId : null, storedProjectId(workspaceId)],
        refused?.workspaceId === workspaceId ? refused.projectId : null,
      ),
    // storage is re-read with each of these; the wrapper's clear comes with an event that changes them
    [entry, picked, refused, workspaceId],
  );
  // Written during render (idempotent), before any child's first request.
  if (workspaceId && current && storedProjectId(workspaceId) !== current.id) {
    storeProjectId(workspaceId, current.id);
  }

  const currentId = current?.id ?? null;
  const select = useCallback(
    (id: string) => {
      if (!workspaceId || id === currentId) return;
      // Written before the navigation so the remounted subtree's first requests
      // already name the new project.
      storeProjectId(workspaceId, id);
      setPicked({ workspaceId, projectId: id });
      // the old project's ids in the query string would deep-link at a foreign resource
      navigate({ pathname: location.pathname, search: "" });
    },
    [currentId, location.pathname, navigate, workspaceId],
  );

  const value = useMemo(
    () => ({
      projects,
      current,
      switchable: isSwitchable(projects),
      boundProjects: entry?.bound_projects ?? 0,
      select,
      refresh,
      managedDepartments,
    }),
    [current, entry?.bound_projects, managedDepartments, projects, refresh, select],
  );

  const authValue = useMemo(
    () => ({ ...auth, can: projectCan(auth.isAdmin, current?.permissions ?? null, auth.can) }),
    [auth, current?.permissions],
  );

  if (access.status === "loading") return <ProjectLoading />;
  // §6.2: reachable workspace, no project in it (a legacy grant without a default project).
  // A steward is in no project by design (§11.4): the shell shows its own empty state.
  if (access.status === "ready" && !auth.isAdmin && !isSteward(auth.role) && entry && entry.projects.length === 0) {
    return <NoProjectAssigned workspaceName={workspace?.name ?? entry.name} />;
  }
  return (
    <ProjectContext.Provider value={value}>
      <AuthContext.Provider value={authValue}>{children}</AuthContext.Provider>
    </ProjectContext.Provider>
  );
}

function ProjectLoading() {
  const { t } = useTranslation();
  const Loading = useUiVersion() === "v3" ? V3AuthLoading : V2AuthLoading;
  return <Loading label={t("v3.project.resolving")} />;
}
