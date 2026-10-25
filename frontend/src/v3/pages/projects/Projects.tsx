import "./projects.css";

import { Navigate, useSearchParams } from "react-router-dom";

import { useProject } from "../../../workspace/project-context";
import { ProjectDetailPage } from "./ProjectDetail";
import { ProjectList } from "./ProjectList";
import { isProjectId, isProjectTab, projectHref } from "./model";

/**
 * "Configure → Projects" (projects design §6.3): the list, or one project's detail
 * with `?id=<project>&tab=<tab>`. Hub-global — the path stays `/v3/projects`, so a
 * project switch does not remount it. Administrators manage everything; a member
 * sees their own projects, and one holding `project.members` edits that project's
 * members, roles and settings (§6.4).
 */
export function V3Projects() {
  const [params] = useSearchParams();
  const id = params.get("id");
  if (id && isProjectId(id)) {
    const tab = params.get("tab");
    return <ProjectDetailPage key={id} projectId={id} tab={isProjectTab(tab) ? tab : "overview"} />;
  }
  return <ProjectList />;
}

/** §6.4 "Project settings": the current project's detail, on its Members tab. */
export function V3ProjectSettings() {
  const { current } = useProject();
  if (!current) return <Navigate to="/v3/projects" replace />;
  return <Navigate to={projectHref(current.id, "members")} replace />;
}
