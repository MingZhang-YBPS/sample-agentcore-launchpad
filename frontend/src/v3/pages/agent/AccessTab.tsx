import { useTranslation } from "react-i18next";

import type { AgentPermissions } from "../../../lib/agent-admin/permissions";
import type { AgentInfo } from "../../../lib/api";
import { Notice, Panel } from "../../ui";
import { ApiKeysCard } from "./ApiKeysCard";
import { BoundaryEntry } from "./BoundaryEntry";
import { IdentityEntry } from "./IdentityEntry";
import { InboundCard } from "./InboundCard";
import { IntegrationCard } from "./IntegrationCard";
import { InvokeAccessCard } from "./InvokeAccessCard";
import type { DetailSections } from "./tabs";

/** 访问与集成: inbound auth (and its switch), calling it from your app (ARNs + IAM policy, IA-1), the /v1 integration snippets and its API keys (IA-2), the identity page entry, the project boundary (§10.14). */
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
      {agent.arn && agent.status !== "deleted" && <InvokeAccessCard agentId={agent.id} />}
      {sections.integration ? (
        <>
          <IntegrationCard agentId={agent.id} />
          <ApiKeysCard agentId={agent.id} agentName={agent.name} />
        </>
      ) : (
        agent.invoke_capability?.reason && <Notice s="info">{t("v3.agent.integration.unavailable")} {agent.invoke_capability.reason}</Notice>
      )}
      {sections.identity ? (
        <IdentityEntry agentId={agent.id} />
      ) : (
        <Panel title={t("v3.agent.identityEntry.title")}><span className="v3-hint">{t("v3.agent.identityEntry.imported")}</span></Panel>
      )}
      <BoundaryEntry agentId={agent.id} />
    </>
  );
}
