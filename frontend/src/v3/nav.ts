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
export function hostedGroups(isAdmin: boolean): V3ModuleGroup[] {
  return V2_NAV.map((group) => ({
    key: group.key,
    labelKey: group.labelKey,
    items: group.items
      .filter((item) => !REBUILT.has(item.to) && (!item.admin || isAdmin))
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
