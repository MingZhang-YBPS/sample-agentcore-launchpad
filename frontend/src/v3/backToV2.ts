import { hostedGroups, v3TwinOf } from "./nav";

/**
 * Each rebuilt V3 page → the V2 module paths it stands in for, read off the rail:
 * `hostedGroups` lists a rebuilt module at its V3 page with the V2 path in `also`.
 * Built with the administrator's rail so an admin page maps too (V2 guards it).
 */
function v2PathsByV3Page(): Map<string, string[]> {
  const out = new Map<string, string[]>();
  for (const item of hostedGroups(true).flatMap((g) => g.items)) {
    const page = item.to.split("?")[0];
    if (!page.startsWith("/v3/")) continue;
    const v2 = (item.also ?? []).filter((p) => p.startsWith("/v2"));
    if (v2.length) out.set(page, [...(out.get(page) ?? []), ...v2]);
  }
  return out;
}

/** V3 pages with no rail twin, or whose twin is not a plain V2 path. */
function special(pathname: string, search: string): string | null {
  const id = new URLSearchParams(search).get("id");
  switch (pathname) {
    case "/v3":
      return "/v2";
    case "/v3/chat":
      return `/v2/chat${search}`;
    case "/v3/agents":
      return id ? `/v2/agents?view=detail&id=${encodeURIComponent(id)}` : "/v2/agents";
    case "/v3/create":
      return "/v2/agents?view=new";
    case "/v3/gate":
      return "/v2/eval/standards";
    default:
      return null;
  }
}

/**
 * The V2 URL a V3 page switches back to: its V2 module, with the same record
 * (`?id=` / `?view=detail&id=`) or tab when V3 maps that URL back onto this very
 * page, else the module's landing. A V3-only page (Models) goes to `/v2`; a path
 * outside `/v3` is a hosted V2 page and stays as it is (null).
 */
export function v2CounterpartOf(pathname: string, search: string): string | null {
  const path = pathname.replace(/\/$/, "") || "/";
  if (path !== "/v3" && !path.startsWith("/v3/")) return null;
  const fixed = special(path, search);
  if (fixed) return fixed;
  const candidates = v2PathsByV3Page().get(path);
  if (!candidates) return "/v2";
  const here = `${path}${search}`;
  const params = new URLSearchParams(search);
  const id = params.get("id");
  // a bare `?id=` is V3's record view: V2's is `view=detail`, tried first since
  // some V2 landings also take the search as is
  const detail = id && !params.has("view") ? [`?view=detail&id=${encodeURIComponent(id)}`] : [];
  const searches = [...detail, search];
  for (const v2 of candidates) {
    for (const s of searches) {
      // a V2 URL that V3 would open as this exact page is its counterpart
      if (v3TwinOf(v2, s) === here) return `${v2}${s}`;
    }
  }
  return candidates[0];
}
