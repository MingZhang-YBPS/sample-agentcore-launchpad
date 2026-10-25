import { useCallback } from "react";
import { useTranslation } from "react-i18next";

import type { CustomRole } from "../../../lib/api/projects";

/** A role's label: a custom role's own name, a template's translation (the
 *  switcher's `v3.project.roles.*`), else the raw key. */
export function useRoleLabel(custom: readonly CustomRole[] = []) {
  const { t } = useTranslation();
  return useCallback(
    (key: string) => custom.find((r) => r.key === key)?.name ?? t(`v3.project.roles.${key}`, key),
    [custom, t],
  );
}
