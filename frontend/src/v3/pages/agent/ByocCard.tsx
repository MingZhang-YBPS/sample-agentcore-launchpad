import { useTranslation } from "react-i18next";

import type { AgentInfo } from "../../../lib/api";
import { Panel } from "../../ui";

interface ByocSummary {
  artifact_kind?: string;
  image_uri?: string | null;
  entrypoint?: string | null;
  allowed_models?: string[];
}

/** Bring-your-own-code: what was uploaded and which models it may call. */
export function ByocCard({ agent }: { agent: AgentInfo }) {
  const { t } = useTranslation();
  const byoc = ((agent.spec ?? {}) as { byoc?: ByocSummary }).byoc;
  if (!byoc) return null;
  const rows: [string, string][] = [
    [t("v3.agent.byoc.kind"), byoc.artifact_kind ?? "—"],
    [t("v3.agent.byoc.image"), byoc.image_uri ?? "—"],
    [t("v3.agent.byoc.entrypoint"), byoc.entrypoint ?? "—"],
    [t("v3.agent.byoc.models"), byoc.allowed_models?.length ? byoc.allowed_models.join(", ") : "—"],
  ];
  return (
    <Panel title={t("v3.agent.byoc.title")}>
      <dl className="v3-kv">
        {rows.map(([label, value]) => (
          <div key={label} style={{ display: "contents" }}>
            <dt>{label}</dt>
            <dd className="mono">{value}</dd>
          </div>
        ))}
      </dl>
    </Panel>
  );
}
