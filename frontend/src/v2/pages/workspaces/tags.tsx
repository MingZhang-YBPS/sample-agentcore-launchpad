import { useTranslation } from "react-i18next";

import type { ResourceVisibility, WorkspaceBootstrapStatus, WorkspaceTier } from "../../../lib/api";
import { Tag, type TagTone } from "../../ui";
import { STATUS_TONE } from "./status";

export function StatusTag({ status }: { status: WorkspaceBootstrapStatus }) {
  const { t } = useTranslation();
  return (
    <Tag tone={STATUS_TONE[status]} dot>
      {t(`v2.workspaces.status.${status}`)}
    </Tag>
  );
}

export function HubTag() {
  const { t } = useTranslation();
  return <Tag tone="outline">{t("v2.workspaces.hub")}</Tag>;
}

export function ExternalTag() {
  const { t } = useTranslation();
  return <Tag tone="blue">{t("v2.workspaces.external")}</Tag>;
}

/** T05: prod is the danger tone — members cannot change its agents directly. */
const TIER_TONE: Record<WorkspaceTier, TagTone> = { dev: "gray", staging: "orange", prod: "red" };

export function TierTag({ tier }: { tier: WorkspaceTier | undefined }) {
  const { t } = useTranslation();
  const value = tier ?? "dev";
  return (
    <Tag tone={TIER_TONE[value]} title={t(`v2.workspaces.tierHint.${value}`)}>
      {t(`v2.workspaces.tier.${value}`)}
    </Tag>
  );
}

/** Member visibility; `isolated` stands out, `shared` (the default) stays quiet. */
export function VisibilityTag({ mode }: { mode: ResourceVisibility }) {
  const { t } = useTranslation();
  return (
    <Tag tone={mode === "isolated" ? "orange" : "gray"} testId={`v2-ws-visibility-tag-${mode}`}>
      {t(`v2.workspaces.visibility.${mode}`)}
    </Tag>
  );
}
