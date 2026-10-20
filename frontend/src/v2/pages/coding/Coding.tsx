import { useSearchParams } from "react-router-dom";

import "./coding.css";
import { RepoIde } from "./RepoIde";
import { RepoList } from "./RepoList";

/**
 * Coding Agent — a member's coding repos on the workspace's platform coding
 * runtime (codex app-server in an AgentCore microVM). The list is the landing page;
 * `?view=repo&id=<repo_id>` opens a repo's IDE (file tree, editor and diffs,
 * the conversation with the coding agent, deploy as an Agent).
 */
export function V2Coding() {
  const [params] = useSearchParams();
  const id = params.get("id");
  if (params.get("view") === "repo" && id) return <RepoIde key={id} id={id} />;
  return <RepoList />;
}
