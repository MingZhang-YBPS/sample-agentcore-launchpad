// Codex reports paths absolutely (`/mnt/workspace/project/README.md`), while the IDE's
// tree, tabs and Changes tab are repo-relative (`README.md`). Every codex path that
// reaches the UI goes through `repoPath` — one rule, unit-tested.

/** The repo's working directory in the coding runtime (parent design §2.1). The
 *  microVM's directory layout is part of the runtime image and still calls it
 *  `project/` — installed runtimes and their snapshots depend on that name. */
export const REPO_ROOT = "/mnt/workspace/project";

const ROOT_SEGMENTS = REPO_ROOT.split("/").filter(Boolean);

/**
 * A path as codex reported it → repo-relative (`src/app.py`). Absolute paths must
 * lie under `REPO_ROOT`; relative paths are relative to the repo (codex's cwd)
 * and come back as they were, minus `.` / empty segments. `..` is resolved and never
 * climbs out. `null` when the path is empty, outside the repo (another absolute
 * path, `~`, a `..` escape) or the repo root itself (no file to open).
 */
export function repoPath(raw: string | null | undefined): string | null {
  if (typeof raw !== "string" || raw === "" || raw.startsWith("~")) return null;
  const absolute = raw.startsWith("/");
  const segments: string[] = [];
  for (const segment of raw.split("/")) {
    if (segment === "" || segment === ".") continue;
    if (segment === "..") {
      if (segments.length === 0) return null;
      segments.pop();
      continue;
    }
    segments.push(segment);
  }
  if (absolute) {
    if (segments.length <= ROOT_SEGMENTS.length) return null;
    if (ROOT_SEGMENTS.some((segment, i) => segments[i] !== segment)) return null;
    return segments.slice(ROOT_SEGMENTS.length).join("/");
  }
  return segments.length ? segments.join("/") : null;
}

/** How a codex path is shown: repo-relative when inside the repo, else as reported. */
export function displayPath(raw: string): string {
  return repoPath(raw) ?? raw;
}
