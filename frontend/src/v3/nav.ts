import { Building2, Cpu, DownloadCloud, FolderCog, FolderKanban, HardDrive, KeySquare, Landmark, PiggyBank, Tags, UserCog, Waypoints, Wrench } from "lucide-react";

import { V2_NAV } from "../v2/nav";
import type { V2NavItem } from "../v2/nav";

const BATCH2: Record<string, string> = {
  "/v2/promotions": "/v3/releases",
  "/v2/issues": "/v3/issues",
  "/v2/environments": "/v3/environments",
  "/v2/observability": "/v3/observability",
  "/v2/memory": "/v3/memory",
  "/v2/governance": "/v3/governance",
  "/v2/my-connections": "/v3/connections",
  "/v2/costs": "/v3/costs",
};

/**
 * Batches 3-4: a module's V2 landing maps onto its V3 page with the same search;
 * a `view=` sub-page does too when the V3 page rebuilt it (listed), and otherwise
 * stays hosted. `views: "all"` — every `view=` of that page is rebuilt.
 */
const MODULES: Record<string, { to: string; views: string[] | "all" }> = {
  "/v2/eval/insights": { to: "/v3/insights", views: [] },
  "/v2/eval/intents": { to: "/v3/intents", views: [] },
  "/v2/eval/data": { to: "/v3/data", views: ["trace", "dataset"] },
  "/v2/eval/evaluators": { to: "/v3/evaluators", views: "all" },
  "/v2/eval/tasks": { to: "/v3/tasks", views: ["new", "detail"] },
  "/v2/eval/online": { to: "/v3/online", views: ["new", "edit", "detail"] },
  "/v2/eval/experiments": { to: "/v3/experiments", views: "all" },
  "/v2/eval/standards": { to: "/v3/standards", views: "all" },
  "/v2/skill-lab": { to: "/v3/skill-lab", views: "all" },
  "/v2/users": { to: "/v3/users", views: ["detail"] },
  "/v2/workspaces": { to: "/v3/workspaces", views: ["detail"] },
  "/v2/connections": { to: "/v3/identity", views: ["targets"] },
  "/v2/fleet": { to: "/v3/fleet", views: "all" },
  "/v2/announcements": { to: "/v3/announcements", views: "all" },
  "/v2/videos": { to: "/v3/videos", views: "all" },
  "/v2/video-management": { to: "/v3/video-management", views: "all" },
};

/** V2 entries V3 has rebuilt natively — the rail shows the V3 page instead. */
const REBUILT = new Set(["/v2", "/v2/chat"]);

/**
 * V2's agent entry is the list and the wizard: V3 has its own list (top of the
 * rail), so the module entry becomes "New agent", V3's launch page.
 */
const RETARGET: Record<string, Pick<V2NavItem, "to" | "labelKey">> = {
  "/v2/agents": { to: "/v3/create", labelKey: "v3.nav.create" },
};

/** Pages only V3 has (no V2 twin), appended to their V2 group. */
export const V3_ONLY: Record<string, V2NavItem[]> = {
  // the platform's model roles (judge, actor, defaults…) — hub-global, admin-only
  // and the console's own online update (online-update §2.7) — hub-global, admin-only
  admin: [
    { to: "/v3/models", labelKey: "v3.models.nav", icon: Cpu, admin: true },
    { to: "/v3/system-update", labelKey: "v3.systemUpdate.nav", icon: DownloadCloud, admin: true },
  ],
};

/**
 * V3-only pages placed right after a V2 entry rather than at the group's end:
 * Configure reads Users → Administrators → Projects → Departments
 * (projects §7.9), all hub-global and administrator-only (phase 1) — the last two
 * also a department administrator's (§11.1, `DEPARTMENT_ADMIN_ENTRIES`) — then
 * Project settings for an administrator or a `project.members` holder.
 */
export const V3_AFTER: Record<string, V2NavItem[]> = {
  "/v2/users": [
    { to: "/v3/admins", labelKey: "v3.admins.nav", icon: UserCog, admin: true },
    { to: "/v3/projects", labelKey: "v3.projects.title", icon: FolderKanban, admin: true },
    { to: "/v3/departments", labelKey: "v3.departments.nav", icon: Building2, admin: true },
    // a project owner's way in (§6.4): the current project's members and settings
    { to: "/v3/project-settings", labelKey: "v3.nav.projectSettings", icon: FolderCog, perm: "project.members" },
    // console single sign-on (sso §2.7): identity providers, administrator-only
    { to: "/v3/sso", labelKey: "v3.sso.nav", icon: KeySquare, admin: true },
  ],
  // Gateway tool targets (Lambda tools, externally hosted MCP servers) sit with
  // the agent's connections and governance
  "/v2/my-connections": [{ to: "/v3/tools/gateway", labelKey: "v3.nav.gatewayTools", icon: Wrench }],
  // the workspace's VPC network profiles (projects §12.12), administrator-only
  // and their storage catalog (storage-mounts §10), administrator-only
  "/v2/workspaces": [
    { to: "/v3/network", labelKey: "v3.network.nav", icon: Waypoints, admin: true },
    { to: "/v3/storage", labelKey: "v3.storage.nav", icon: HardDrive, admin: true },
    // cost allocation tags and their activation in Billing (cost-tags §1.6), administrator-only
    { to: "/v3/cost-tags", labelKey: "v3.costTags.nav", icon: Tags, admin: true },
  ],
  // the three budget dashboards (projects §11.3), each shown only to whoever can open
  // it: the current project's budget (every member), the department's (its
  // administrators — `DEPARTMENT_ADMIN_ENTRIES` — and platform administrators), the
  // platform's (administrators). The backend enforces the same split.
  "/v2/costs": [
    { to: "/v3/costs?view=project", labelKey: "v3.costs.nav.project", icon: PiggyBank },
    { to: "/v3/costs?view=department", labelKey: "v3.costs.nav.department", icon: Building2, admin: true },
    { to: "/v3/costs?view=platform", labelKey: "v3.costs.nav.platform", icon: Landmark, admin: true },
  ],
};

/** `admin` entries a department administrator (non-empty `managed_departments`) sees too. */
export const DEPARTMENT_ADMIN_ENTRIES: ReadonlySet<string> = new Set([
  "/v3/projects", "/v3/departments", "/v3/costs?view=department",
]);

/**
 * The rail of a steward (organization admin, projects §11.4): the structure entries of
 * Configure and nothing else — the business groups would only open its empty state.
 * Listed by their rail path (before the V3 twin mapping: `/v2/users` opens `/v3/users`).
 */
export const STEWARD_ENTRIES: ReadonlySet<string> = new Set([
  "/v2/users", "/v3/admins", "/v3/projects", "/v3/departments",
  "/v2/workspaces", "/v3/models", "/v3/network", "/v3/storage",
]);

export interface V3ModuleGroup {
  key: string;
  labelKey: string;
  items: V2NavItem[];
}

/**
 * Every module page V3 hosts (renders inside its own shell, on its theme) rather
 * than redesigns, in V2's groups. Derived from `V2_NAV` so a page added to V2
 * shows up in V3 too, instead of becoming unreachable from it.
 */
export function hostedGroups(
  isAdmin: boolean,
  perms: readonly string[] = [],
  managesDepartment = false,
  steward = false,
): V3ModuleGroup[] {
  return V2_NAV.map((group) => ({
    key: group.key,
    labelKey: group.labelKey,
    items: [...group.items.flatMap((item) => [item, ...(V3_AFTER[item.to] ?? [])]), ...(V3_ONLY[group.key] ?? [])]
      .filter((item) => !REBUILT.has(item.to)
        && (steward ? STEWARD_ENTRIES.has(item.to) : visible(item, isAdmin, perms, managesDepartment)))
      .map((item) => {
        const [path, query] = item.to.split("?");
        const twin = v3TwinOf(path, query ? `?${query}` : "");
        // a rebuilt module opens its V3 page, and stays lit on the V2 sub-pages
        // it still hosts (a register form, an edit view)
        return twin
          ? { ...item, to: twin, also: [...(item.also ?? []), path] }
          : { ...item, ...RETARGET[item.to] };
      }),
  })).filter((group) => group.items.length > 0);
}

/** `admin` entries are an administrator's; `perm` ones also a holder of that permission;
 *  the structure pages also a department administrator's. */
function visible(item: V2NavItem, isAdmin: boolean, perms: readonly string[], managesDepartment: boolean): boolean {
  if (isAdmin) return true;
  if (item.perm) return perms.includes(item.perm);
  if (managesDepartment && DEPARTMENT_ADMIN_ENTRIES.has(item.to)) return true;
  return !item.admin;
}

/**
 * The V3 page a V2 URL is rebuilt as, or null when V3 hosts the V2 page itself
 * (its sub-pages V3 has not redesigned: forms, editors). `full=1` always keeps
 * the V2 page — V3 links there for what its own page does not do.
 */
export function v3TwinOf(pathname: string, search: string): string | null {
  const params = new URLSearchParams(search);
  if (params.has("full")) return null;
  const view = params.get("view");
  const id = params.get("id");
  switch (pathname.replace(/\/$/, "") || "/") {
    case "/v2":
      return "/v3";
    case "/v2/chat":
      return `/v3/chat${search}`;
    case "/v2/agents":
      // the bare wizard; a prefilled one (method, scenario, template, gateway,
      // skill) is the full form the launch page hands over to
      if (view === "new" && ![...params.keys()].some((k) => k !== "view")) return "/v3/create";
      // the detail page is rebuilt (v3-agent-management §2.5); the editor and the
      // identity page stay hosted until V3 has its own
      if (view === "detail" && id) return `/v3/agents?id=${encodeURIComponent(id)}`;
      return null;
    case "/v2/promotions":
      if (view === "detail" && id) return `/v3/releases?id=${encodeURIComponent(id)}`;
      return view ? null : `/v3/releases${search}`;
    // their `view=` is a tab of the landing, not a sub-page: it rides along
    case "/v2/costs":
      return `/v3/costs${search}`;
    // the session and trace views are rebuilt as well
    case "/v2/observability":
      return !view || ((view === "session" || view === "trace") && id) ? `/v3/observability${search}` : null;
    case "/v2/issues":
      return `/v3/issues${search}`;
    // batch 2 (Agent run): the module's landing — its tabs ride along (`?tab=`,
    // `?range=`); a `view=` sub-page (a detail, an editor) stays hosted
    case "/v2/environments":
    case "/v2/memory":
    case "/v2/governance":
    case "/v2/my-connections":
      return view ? null : `${BATCH2[pathname.replace(/\/$/, "")]}${search}`;
    case "/v2/assistant":
      return view ? null : "/v3/assistant";
    case "/v2/registry":
      if (!view) return "/v3/registry";
      if (view === "detail" && id) return `/v3/registry?id=${encodeURIComponent(id)}`;
      return null;
    case "/v2/knowledge-bases":
      if (!view) return "/v3/knowledge";
      if (view === "detail" && id) return `/v3/knowledge?id=${encodeURIComponent(id)}`;
      return null;
    default: {
      const mod = MODULES[pathname.replace(/\/$/, "")];
      if (!mod) return null;
      const rebuilt = !view || mod.views === "all" || mod.views.includes(view);
      return rebuilt ? `${mod.to}${search}` : null;
    }
  }
}

/** Same rule as V2's sidebar: a `?view=` entry is active only on that sub-page. */
export function isActive(item: Pick<V2NavItem, "to" | "also">, pathname: string, search: string): boolean {
  const [path, query] = item.to.split("?");
  if (query) {
    if (pathname !== path) return false;
    const have = new URLSearchParams(search);
    return [...new URLSearchParams(query)].every(([k, v]) => have.get(k) === v);
  }
  return [path, ...(item.also ?? [])].some((prefix) => pathname === prefix || pathname.startsWith(`${prefix}/`));
}
