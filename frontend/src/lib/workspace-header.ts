/**
 * The `X-Workspace` and `X-Project` request headers: storage of the selections,
 * and the global `window.fetch` wrapper that stamps them on every backend call.
 *
 * Wrapping `fetch` rather than the typed client is deliberate: most console
 * pages call `fetch("/api/...")` directly (the SSE chat turn, the studio debug
 * client, `useHealth`, the registry/evaluation/knowledge surfaces), each with
 * its own `res.ok` error handling. One wrapper reaches all of them, including
 * the streams, without rewriting those branches.
 */

export const WORKSPACE_STORAGE_KEY = "launchpad_workspace";
export const WORKSPACE_HEADER = "X-Workspace";

/** Workspace ids the backend can mint (`routers/workspaces.py::_SLUG`) — the
 * same 2-32 characters, so a tampered storage value cannot make every request
 * carry a header the backend would refuse. */
const WORKSPACE_ID = /^[a-z0-9][a-z0-9-]{1,31}$/;

/** The selection is a UI preference, not a credential — a private-mode window
 * that refuses storage simply falls back to the backend's default. */
export function storedWorkspaceId(): string | null {
  try {
    const raw = window.localStorage.getItem(WORKSPACE_STORAGE_KEY);
    return raw && WORKSPACE_ID.test(raw) ? raw : null;
  } catch {
    return null;
  }
}

export function storeWorkspaceId(id: string): void {
  try {
    window.localStorage.setItem(WORKSPACE_STORAGE_KEY, id);
  } catch {
    /* storage unavailable — the header is dropped, not the request */
  }
}

/* ── the project (projects design §2.6) ───────────────────────────────────── */

export const PROJECT_HEADER = "X-Project";
/** One key per workspace: each workspace remembers its own last project. */
export const PROJECT_STORAGE_PREFIX = "launchpad_project:";
/** Dispatched when the backend refused the stamped project (see `projectResetCode`). */
export const PROJECT_INVALID_EVENT = "launchpad-project-invalid";

export interface ProjectInvalidDetail {
  workspaceId: string;
  /** the project the refused request carried; null when it carried none */
  projectId: string | null;
  code: string;
}

/** `models/projects.py::PROJECT_ID_PATTERN` — the same shape as a workspace id. */
const PROJECT_ID = /^[a-z0-9][a-z0-9-]{1,31}$/;

export function isProjectId(raw: string | null | undefined): raw is string {
  return typeof raw === "string" && PROJECT_ID.test(raw);
}

export function storedProjectId(workspaceId: string | null | undefined): string | null {
  if (!workspaceId) return null;
  try {
    const raw = window.localStorage.getItem(PROJECT_STORAGE_PREFIX + workspaceId);
    return isProjectId(raw) ? raw : null;
  } catch {
    return null;
  }
}

export function storeProjectId(workspaceId: string, projectId: string): void {
  try {
    window.localStorage.setItem(PROJECT_STORAGE_PREFIX + workspaceId, projectId);
  } catch {
    /* storage unavailable — the header is dropped, the backend falls back */
  }
}

export function clearProjectId(workspaceId: string): void {
  try {
    window.localStorage.removeItem(PROJECT_STORAGE_PREFIX + workspaceId);
  } catch {
    /* nothing stored then either */
  }
}

/**
 * Stamp the selection on a request's headers, in place. A header the caller set
 * wins; the project is the one remembered for the workspace the request actually
 * targets — a request pinned to another workspace (`pinnedWorkspace`) carries that
 * workspace's project, or none, never the current workspace's.
 * Returns what the request now targets.
 */
export function stampSelection(
  headers: Headers,
  workspaceId: string | null,
): { workspaceId: string | null; projectId: string | null } {
  if (workspaceId && !headers.has(WORKSPACE_HEADER)) headers.set(WORKSPACE_HEADER, workspaceId);
  const target = headers.get(WORKSPACE_HEADER);
  if (target && !headers.has(PROJECT_HEADER)) {
    const projectId = storedProjectId(target);
    if (projectId) headers.set(PROJECT_HEADER, projectId);
  }
  return { workspaceId: target, projectId: headers.get(PROJECT_HEADER) };
}

/**
 * Which refusals mean "the project selection is stale" (`services/access.py::
 * resolve_project`): the stamped project is not reachable (404 `project.not_found`
 * naming it — the same code on `/api/projects/{other}` names that other id and is
 * left alone), several projects and none named (400 `project.header_required`), or
 * none at all here (403 `project.none`). Null for every other response.
 */
export function projectResetCode(
  status: number,
  body: unknown,
  sentProjectId: string | null,
): string | null {
  if (status !== 400 && status !== 403 && status !== 404) return null;
  if (!body || typeof body !== "object") return null;
  const env = body as { code?: unknown; detail?: unknown };
  if (status === 400 && env.code === "project.header_required") return env.code;
  if (status === 403 && env.code === "project.none") return env.code;
  if (status === 404 && env.code === "project.not_found" && sentProjectId) {
    const detail = (env.detail ?? {}) as { project_id?: unknown };
    return detail.project_id === sentProjectId ? env.code : null;
  }
  return null;
}

/** Read a refused response's envelope off a clone; the caller's body stays unread. */
async function watchProjectRefusal(
  res: Response,
  target: { workspaceId: string | null; projectId: string | null },
): Promise<void> {
  if (!target.workspaceId || (res.status !== 400 && res.status !== 403 && res.status !== 404)) return;
  const body: unknown = await res.clone().json().catch(() => null);
  const code = projectResetCode(res.status, body, target.projectId);
  if (!code) return;
  // Cleared before anyone reacts, so the very next request falls back to the
  // backend's own choice (§2.6) instead of repeating the refused id.
  clearProjectId(target.workspaceId);
  const detail: ProjectInvalidDetail = { workspaceId: target.workspaceId, projectId: target.projectId, code };
  window.dispatchEvent(new CustomEvent(PROJECT_INVALID_EVENT, { detail }));
}

function backendPath(input: RequestInfo | URL): string | null {
  const raw =
    typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
  let url: URL;
  try {
    url = new URL(raw, window.location.href);
  } catch {
    return null;
  }
  if (url.origin !== window.location.origin) return null;
  const scoped = url.pathname.startsWith("/api/") || url.pathname.startsWith("/v1/");
  return scoped ? url.pathname : null;
}

let installed = false;

/**
 * Install the wrapper. Must run before the app renders so no page's mount
 * effect can fire an unstamped request.
 */
export function installWorkspaceHeader(): void {
  if (installed) return; // HMR / a second import must not wrap the wrapper
  installed = true;
  const original = window.fetch.bind(window);
  window.fetch = (input: RequestInfo | URL, init?: RequestInit) => {
    if (backendPath(input) === null) return original(input, init);
    // Console API responses are live state and must never be satisfied from the
    // HTTP cache. The backend sends no caching headers, so normally nothing is
    // cached — but whatever once answered on this origin (a port forward that
    // briefly pointed elsewhere, a static preview server on the same port) can
    // leave a heuristically-fresh 200 under an /api URL, and the browser then
    // serves that entry to every fetch() without touching the network.
    const stamped: RequestInit = { ...init, cache: init?.cache ?? "no-store" };
    const headers = new Headers(
      init?.headers ?? (input instanceof Request ? input.headers : undefined),
    );
    // A caller that named a workspace means it (the Workspaces page polls the
    // bootstrap job of a workspace that is not the current selection).
    const target = stampSelection(headers, storedWorkspaceId());
    if (!target.workspaceId) return original(input, stamped);
    const pending = original(input, { ...stamped, headers });
    void pending.then((res) => watchProjectRefusal(res, target)).catch(() => undefined);
    return pending;
  };
}
