/**
 * Typed client of the console rail settings (`routers/nav_settings.py`): the rail
 * entries an administrator hid from members.
 *
 * Hub-global. Every signed-in account reads the list (the V3 rail and ⌘K drop those
 * entries for a member); only an administrator writes it. A view filter, not
 * authorization: a hidden page stays reachable by URL.
 */
import { getJson, sendJson } from "../api";

export interface HiddenModules {
  /** rail entry paths as `v3/nav.ts` names them (`railKey`), sorted */
  hidden: string[];
  updated_by: string;
  updated_at: string | null;
}

export const navSettingsApi = {
  hiddenModules: () => getJson<HiddenModules>("/api/nav/hidden-modules"),
  setHiddenModules: (hidden: string[]) =>
    sendJson<HiddenModules>("/api/nav/hidden-modules", "PUT", { hidden }),
};
