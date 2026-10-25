import { useTranslation } from "react-i18next";

import { useAuth } from "../auth/auth-context";
import { useWorkspace } from "./workspace-context";

/**
 * T05 — whether the current workspace refuses this caller's agent changes.
 *
 * Mirrors the backend guard (`route_policy.PROD_PROTECTED`): on a `prod`
 * workspace a member's create / edit / delete / convert / import is refused with
 * 403 `workspace.prod_protected`; administrators pass (break-glass, journaled).
 * The console disables those controls with `title` as the explanation — the
 * backend stays the real boundary.
 */
export function useProdLock(): { locked: boolean; title: string | undefined } {
  const { t } = useTranslation();
  const { current } = useWorkspace();
  const { isAdmin } = useAuth();
  const locked = current?.tier === "prod" && !isAdmin;
  return { locked, title: locked ? t("v2.agents.prodLocked") : undefined };
}
