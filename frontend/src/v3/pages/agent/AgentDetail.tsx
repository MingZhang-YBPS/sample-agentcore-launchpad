import { ArrowLeft } from "lucide-react";
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link, useNavigate, useSearchParams } from "react-router-dom";

import { isImported, useAgentActions } from "../../../lib/agent-admin/actions";
import { useAgentDetail } from "../../../lib/agent-admin/deploy";
import { useAgentPermissions } from "../../../lib/agent-admin/permissions";
import { api, dlcApi } from "../../../lib/api";
import { useWorkspace } from "../../../workspace/workspace-context";
import { useLoad, useToast } from "../../hooks";
import { JourneyStrip } from "../../story/JourneyStrip";
import { agentSignal } from "../../signals";
import { Chip, Filters, Lamp, Notice, PageHead, Skeleton } from "../../ui";
import { AccessTab } from "./AccessTab";
import { ActionDialog } from "./ActionDialog";
import { DeployTab } from "./DeployTab";
import { HeaderActions } from "./HeaderActions";
import { OverviewTab } from "./OverviewTab";
import { ReleaseTab } from "./ReleaseTab";
import { AGENT_TABS, type AgentTab, detailSections, parseTab, tabSearch } from "./tabs";
import "./agent.css";

/**
 * The V3 agent detail page (v3-agent-management §2): the header with its
 * Edit ▾ / ⋯ More menus, then four tabs — overview, deploy & versions, access &
 * integration, release — kept in the URL (`?id=…&tab=access`). Only assembly
 * lives here; each card is its own file in this folder.
 */
export function AgentDetail({ id }: { id: string }) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const toast = useToast();
  const { current: workspace } = useWorkspace();
  const [params, setParams] = useSearchParams();
  const tab = parseTab(params.get("tab"));
  const listParams = new URLSearchParams(params);
  listParams.delete("id");
  listParams.delete("tab");
  const listTo = `/v3/agents${listParams.size ? `?${listParams}` : ""}`;

  const detail = useAgentDetail(id, 4000);
  const release = useLoad(() => dlcApi.release(id), `v3-agent-rel:${id}`);
  const versions = useLoad(() => api.agentVersions(id), `v3-agent-ver:${id}`);
  const perms = useAgentPermissions();
  const [focusSnapshots, setFocusSnapshots] = useState(false);
  const actions = useAgentActions({
    onDeleted: (agent) => {
      const name = agent.display_name || agent.name;
      toast("ok", t(isImported(agent) ? "v3.agent.action.removed" : "v3.agent.action.deleted", { name }));
      navigate(listTo);
    },
    onConverted: (agent) => {
      toast("ok", t("v3.agent.action.convertStarted", { name: agent.name }));
      navigate(`/v3/agents?id=${encodeURIComponent(agent.id)}`);
    },
  });

  const a = detail.agent;
  // when a deploy settles, its versions and release state moved too
  const deploying = detail.deploying;
  const reloadVersions = versions.reload;
  const reloadRelease = release.reload;
  useEffect(() => {
    if (!deploying) return;
    return () => {
      reloadVersions();
      reloadRelease();
    };
  }, [deploying, reloadVersions, reloadRelease]);

  const refresh = () => {
    detail.reload();
    release.reload();
    versions.reload();
  };
  const openTab = (next: AgentTab) => {
    if (next !== "deploy") setFocusSnapshots(false);
    setParams(tabSearch(params, next), { replace: true });
  };

  if (!a && !detail.error) return <Skeleton rows={6} />;
  if (!a)
    return (
      <Notice s="act">
        {detail.error ?? t("v3.agent.notFound")}
        <button type="button" className="v3-btn sm" onClick={detail.reload}>{t("v3.ux.retry")}</button>
        <Link to={listTo} className="v3-inline-link">{t("v3.agents.title")}</Link>
      </Notice>
    );

  const name = a.display_name || a.name;
  const spec = a.spec as { system_prompt?: string };
  const sections = detailSections(a);

  return (
    <div className="v3-reveal" style={{ display: "grid", gap: 16 }}>
      <div>
        <button type="button" className="v3-btn ghost sm" onClick={() => navigate(listTo)}>
          <ArrowLeft size={14} /> {t("v3.agents.title")}
        </button>
      </div>
      <PageHead
        eyebrow={`${a.method} · ${a.id.slice(0, 8)}`}
        title={
          <span style={{ display: "inline-flex", alignItems: "center", gap: 14 }}>
            <Lamp s={agentSignal(a)} live={a.status === "active"} />
            {name}
          </span>
        }
        sub={a.status === "failed" ? a.error ?? t("v3.agent.failed") : spec.system_prompt?.slice(0, 180)}
        end={
          <HeaderActions
            agent={a}
            perms={perms}
            actions={actions}
            refreshing={detail.loading || release.loading || versions.loading}
            onRefresh={refresh}
            onRollback={() => {
              setFocusSnapshots(true);
              openTab("deploy");
            }}
          />
        }
      />
      <JourneyStrip key={a.id} agentId={a.id} status={a.status} version={a.version} />
      {detail.error && (
        <Notice s="act">
          {detail.error} <button type="button" className="v3-btn sm" onClick={detail.reload}>{t("v3.ux.retry")}</button>
        </Notice>
      )}
      {perms.locked && <Notice s="wait">{perms.lockTitle}</Notice>}
      <div className="v3-agent-status">
        <Chip s={agentSignal(a)}>{t(`v3.agents.filter.${agentSignal(a)}`)}</Chip>
        <span className="v3-hint">{t("v3.ux.runtimeStatus")}</span>
      </div>

      <nav aria-label={t("v3.agent.tabs.label")}>
        <Filters value={tab} onChange={openTab} options={AGENT_TABS.map((key) => ({ value: key, label: t(`v3.agent.tabs.${key}`) }))} />
      </nav>

      {tab === "overview" && <OverviewTab agent={a} sections={sections} release={release} />}
      {tab === "deploy" && (
        <DeployTab
          agent={a}
          job={detail.job}
          sections={sections}
          versions={versions}
          perms={perms}
          focusSnapshots={focusSnapshots}
          onChanged={detail.reload}
        />
      )}
      {tab === "access" && <AccessTab agent={a} sections={sections} perms={perms} onChanged={detail.reload} />}
      {tab === "release" && (
        <ReleaseTab agent={a} sections={sections} release={release.data} currentWorkspaceId={workspace?.id ?? null} />
      )}

      <ActionDialog key={actions.pending ? `${actions.pending.kind}:${actions.pending.agent.id}` : "none"} actions={actions} />
    </div>
  );
}
