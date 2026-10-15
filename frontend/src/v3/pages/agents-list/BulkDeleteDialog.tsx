import { useState } from "react";
import { useTranslation } from "react-i18next";

import { ApiErrorDetail } from "../../../components/ApiErrorDetail";
import { isImported } from "../../../lib/agent-admin/actions";
import { api, type AgentInfo } from "../../../lib/api";
import { Confirm, Notice } from "../../ui";
import { type BulkResult, deleteMany } from "./rows";

/**
 * The bulk delete confirmation (§3): it lists every agent by name — an imported
 * runtime is marked as only losing its platform record — and deletes them one by
 * one. When some fail the dialog stays open on those, with each error; the ones
 * that went are reported through `onDone` either way so the list reloads.
 */
export function BulkDeleteDialog({
  agents,
  onDone,
  onCancel,
  del = api.deleteAgent,
}: {
  agents: AgentInfo[];
  onDone: (result: BulkResult) => void;
  onCancel: () => void;
  /** the delete call (tests pass a stub) */
  del?: (id: string) => Promise<unknown>;
}) {
  const { t } = useTranslation();
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState<BulkResult["failed"]>([]);
  // after a partial failure only the agents still there are retried
  const left = failed.length ? failed.map((f) => f.agent) : agents;

  const run = async () => {
    setBusy(true);
    const result = await deleteMany(left, del);
    setBusy(false);
    setFailed(result.failed);
    onDone(result);
  };

  return (
    <Confirm
      title={t("v3.agents.bulk.title", { count: left.length })}
      confirmLabel={failed.length ? t("v3.agents.bulk.retry") : t("v3.agents.bulk.confirm", { count: left.length })}
      cancelLabel={t("v3.agent.action.cancel")}
      danger
      busy={busy}
      confirmDisabled={left.length === 0}
      confirmReason={t("story.reason.nothingSelected")}
      onConfirm={() => void run()}
      onCancel={onCancel}
    >
      <div style={{ display: "grid", gap: 12 }} data-testid="v3-agents-bulk-dialog">
        <p style={{ margin: 0 }}>{t(failed.length ? "v3.agents.bulk.failedBody" : "v3.agents.bulk.body", { count: left.length })}</p>
        <ul className="v3-agents-bulk-list">
          {left.map((a) => {
            const error = failed.find((f) => f.agent.id === a.id)?.error;
            return (
              <li key={a.id}>
                <b>{a.display_name || a.name}</b> <span className="mono">{a.name}</span>
                {isImported(a) && <> <small>{t("v3.agents.bulk.recordOnly")}</small></>}
                {error != null && (
                  <Notice s="act">
                    <ApiErrorDetail error={error} />
                  </Notice>
                )}
              </li>
            );
          })}
        </ul>
      </div>
    </Confirm>
  );
}
