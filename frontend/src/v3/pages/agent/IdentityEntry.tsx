import { KeyRound } from "lucide-react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";

import { identityPath } from "../../../lib/agent-admin/edit";
import { Panel } from "../../ui";

/** The identity page entry (D-2: the page itself stays the V2 one, hosted in V3, for now). */
export function IdentityEntry({ agentId }: { agentId: string }) {
  const { t } = useTranslation();
  return (
    <Panel title={t("v3.agent.identityEntry.title")}>
      <div style={{ display: "flex", gap: 16, alignItems: "center", justifyContent: "space-between", flexWrap: "wrap" }}>
        <span className="v3-hint">{t("v3.agent.identityEntry.body")}</span>
        <Link to={identityPath(agentId)} className="v3-btn" data-testid="v3-agent-identity-entry">
          <KeyRound size={14} /> {t("v3.agent.identityEntry.open")}
        </Link>
      </div>
    </Panel>
  );
}
