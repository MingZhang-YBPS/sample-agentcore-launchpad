import { useTranslation } from "react-i18next";

import type { AgentPermissions } from "../../../lib/agent-admin/permissions";
import type { AgentInfo } from "../../../lib/api";
import { Notice, Panel } from "../../ui";
import { IdentityEntry } from "./IdentityEntry";
import { InboundCard } from "./InboundCard";
import { IntegrationCard } from "./IntegrationCard";
import type { DetailSections } from "./tabs";

/** 访问与集成: inbound auth (and its switch), the /v1 integration snippets, the identity page entry. */
export function AccessTab({
  agent,
  sections,
  perms,
  onChanged,
}: {
  agent: AgentInfo;
  sections: DetailSections;
  perms: AgentPermissions;
  onChanged: () => void;
}) {
  const { t } = useTranslation();
  return (
    <>
      {sections.inbound && <InboundCard agent={agent} perms={perms} onSwitched={onChanged} />}
      {sections.integration ? (
        <IntegrationCard agentId={agent.id} />
      ) : (
        agent.invoke_capability?.reason && <Notice s="info">{t("v3.agent.integration.unavailable")} {agent.invoke_capability.reason}</Notice>
      )}
      {sections.identity ? (
        <IdentityEntry agentId={agent.id} />
      ) : (
        <Panel title={t("v3.agent.identityEntry.title")}><span className="v3-hint">{t("v3.agent.identityEntry.imported")}</span></Panel>
      )}
    </>
  );
}
