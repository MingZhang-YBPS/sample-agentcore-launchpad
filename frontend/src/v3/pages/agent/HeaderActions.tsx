import { Activity, ChevronDown, Ellipsis, KeyRound, MessagesSquare, RefreshCw, Scale, Settings2 } from "lucide-react";
import { useTranslation } from "react-i18next";
import { Link, useNavigate } from "react-router-dom";

import { classicDetailPath, EDIT_HINT_KEY, EDIT_LABEL_KEY, editKind, editPath, identityPath } from "../../../lib/agent-admin/edit";
import type { AgentPermissions } from "../../../lib/agent-admin/permissions";
import type { useAgentActions } from "../../../lib/agent-admin/actions";
import type { AgentInfo } from "../../../lib/api";
import { Menu, type MenuItem } from "./Menu";
import { moreEntries, type MoreEntry } from "./menus";
import { isMcpServer } from "./tabs";

/**
 * The detail page's header row: refresh, chat, gate, observability, identity,
 * then "Edit configuration ▾" (only the editor for the agent's method — wizard,
 * canvas or preset) and "⋯ More" (convert, roll back, the classic detail page,
 * delete). Without the permission a control is hidden; on a locked prod
 * workspace, for a preset when not an admin, or while a deploy runs it is shown
 * disabled with the reason as its title (§2.4).
 */
export function HeaderActions({
  agent,
  perms,
  actions,
  refreshing,
  onRefresh,
  onRollback,
}: {
  agent: AgentInfo;
  perms: AgentPermissions;
  actions: ReturnType<typeof useAgentActions>;
  refreshing: boolean;
  onRefresh: () => void;
  /** open the snapshots (deploy tab) to pick the one to roll back to */
  onRollback: () => void;
}) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const mcp = isMcpServer(agent);
  const imported = agent.method === "discovered_runtime";

  const edit = perms.editState(agent);
  const editTitle =
    edit.blocked === "locked" ? perms.lockTitle : edit.blocked ? t(`v3.agent.editBlocked.${edit.blocked}`) : t(EDIT_HINT_KEY);
  const editItems: MenuItem[] = edit.visible
    ? [{
        key: "editor",
        label: t(`v3.agent.editMenu.${editKind(agent)}`),
        onSelect: () => navigate(editPath(agent)),
        disabled: edit.blocked !== null,
        title: editTitle,
      }]
    : [];

  const run: Record<MoreEntry["key"], () => void> = {
    convert: () => actions.askConvert(agent),
    rollback: onRollback,
    classic: () => navigate(classicDetailPath(agent.id)),
    delete: () => actions.askDelete(agent),
    remove: () => actions.askDelete(agent),
  };
  const moreItems: MenuItem[] = moreEntries(agent, perms).map((entry) => ({
    key: entry.key,
    label: t(entry.key === "classic" ? "v3.agent.editMenu.classic" : `v3.agent.${entry.key}`),
    onSelect: run[entry.key],
    disabled: entry.disabled,
    title: entry.why === "lock" ? perms.lockTitle : undefined,
    danger: entry.danger,
    separated: entry.danger,
  }));

  return (
    <>
      <button type="button" className="v3-btn" disabled={refreshing} onClick={onRefresh}>
        <RefreshCw size={14} /> {t("v3.ux.refresh")}
      </button>
      {!mcp && <Link to={`/v3/chat?agent=${agent.id}`} className="v3-btn"><MessagesSquare size={14} /> {t("v3.agent.chat")}</Link>}
      {!mcp && <Link to={`/v3/gate?agent=${agent.id}`} className="v3-btn"><Scale size={14} /> {t("v3.agent.gate")}</Link>}
      <Link to="/v3/observability" className="v3-btn ghost"><Activity size={14} /> {t("v3.agent.observability")}</Link>
      {!imported && (
        <Link to={identityPath(agent.id)} className="v3-btn ghost" data-testid="v3-agent-identity-open">
          <KeyRound size={14} /> {t("v3.agent.identity")}
        </Link>
      )}
      <Menu
        kind="primary"
        label={<><Settings2 size={14} /> {t(EDIT_LABEL_KEY)} <ChevronDown size={13} /></>}
        title={t(EDIT_HINT_KEY)}
        items={editItems}
        testId="v3-agent-edit"
      />
      <Menu
        kind="ghost"
        label={<><Ellipsis size={14} /> {t("v3.agent.more")}</>}
        items={moreItems}
        testId="v3-agent-more"
      />
    </>
  );
}
