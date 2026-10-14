import { useTranslation } from "react-i18next";
import { useNavigate } from "react-router-dom";

import { useAuth } from "../../../auth/auth-context";
import { ApiErrorDetail } from "../../../components/ApiErrorDetail";
import { useRelease } from "../../../lib/agent-admin/release";
import type { AgentInfo } from "../../../lib/api";
import { ago } from "../../format";
import { useToast } from "../../hooks";
import { Btn, Chip, Dialog, Empty, Notice, Panel, Skeleton } from "../../ui";

/**
 * T20/T21 — freeze this publish as a bundle (idempotent), share it as a template,
 * and ask for it to be released into another workspace (reviewed by a second person).
 */
export function PromotionCard({ agent, currentWorkspaceId }: { agent: AgentInfo; currentWorkspaceId: string | null }) {
  const { t } = useTranslation();
  const { can } = useAuth();
  const toast = useToast();
  const navigate = useNavigate();
  const rel = useRelease(
    { agentId: agent.id, agentTitle: agent.display_name || agent.name, currentWorkspaceId },
    {
      onBundled: (b) => toast("ok", t("v3.agent.promotion.bundled", { digest: b.digest.slice(0, 12) })),
      onPublished: () => toast("ok", t("v3.agent.promotion.published")),
      onRequested: (id) => {
        toast("ok", t("v3.agent.promotion.requested"));
        navigate(`/v3/releases?id=${encodeURIComponent(id)}`);
      },
    },
  );
  const mayRequest = can("promotion.request");
  return (
    <Panel
      title={t("v3.agent.promotion.title")}
      end={
        <>
          <Btn size="sm" kind="ghost" disabled={rel.busy} onClick={() => void rel.publish()}>{t("v3.agent.promotion.publish")}</Btn>
          {mayRequest && <Btn size="sm" disabled={rel.busy} onClick={() => void rel.bundle()}>{t("v3.agent.promotion.bundle")}</Btn>}
        </>
      }
    >
      <div style={{ display: "grid", gap: 12 }} data-testid="v3-agent-promotion">
        <span className="v3-hint">{t("v3.agent.promotion.sub")}</span>
        {rel.error != null && !rel.asking && <Notice s="act"><ApiErrorDetail error={rel.error} data-testid="v3-agent-promotion-error" /></Notice>}
        {rel.bundles.loading && !rel.bundles.data ? (
          <Skeleton rows={2} />
        ) : rel.bundles.error ? (
          <Notice s="wait">{rel.bundles.error}</Notice>
        ) : rel.rows.length === 0 ? (
          <Empty title={t("v3.agent.promotion.empty")} />
        ) : (
          <table className="v3-table">
            <tbody>
              {rel.rows.map((row) => (
                <tr key={row.id}>
                  <td className="mono">{row.digest.slice(0, 12)}</td>
                  <td><Chip>{row.snapshot_seq ? t("v3.agent.promotion.publishN", { n: row.snapshot_seq }) : t("v3.agent.promotion.live")}</Chip></td>
                  <td style={{ color: "var(--v3-text-3)" }}>{ago(row.created_at)}</td>
                  <td className="num">
                    {mayRequest && rel.targets.length > 0 && (
                      <Btn size="sm" onClick={() => rel.ask(row)}>{t("v3.agent.promotion.request")}</Btn>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        {rel.targets.length === 0 && rel.rows.length > 0 && <Notice s="info">{t("v3.agent.promotion.noTargets")}</Notice>}
      </div>
      {rel.asking && (
        <Dialog
          title={t("v3.agent.promotion.requestTitle")}
          onClose={() => !rel.busy && rel.cancelAsk()}
          foot={
            <>
              <Btn kind="ghost" onClick={rel.cancelAsk} disabled={rel.busy}>{t("v3.agent.action.cancel")}</Btn>
              <Btn kind="primary" disabled={!rel.canRequest} onClick={() => void rel.request()}>{t("v3.agent.promotion.requestConfirm")}</Btn>
            </>
          }
        >
          <div style={{ display: "grid", gap: 12 }}>
            <label className="v3-field">
              <span>{t("v3.agent.promotion.target")}</span>
              <select className="v3-input" value={rel.target} onChange={(e) => rel.setTarget(e.target.value)}>
                {rel.targets.map((w) => (
                  <option key={w.id} value={w.id}>{`${w.name} · ${w.id}${w.tier && w.tier !== "dev" ? ` (${w.tier})` : ""}`}</option>
                ))}
              </select>
            </label>
            <label className="v3-field">
              <span>{t("v3.agent.promotion.changeNote")}</span>
              <textarea className="v3-input" rows={3} maxLength={4000} value={rel.changeNote} onChange={(e) => rel.setChangeNote(e.target.value)} />
            </label>
            <label className="v3-field">
              <span>{t("v3.agent.promotion.rollbackNote")}</span>
              <textarea className="v3-input" rows={3} maxLength={4000} value={rel.rollbackNote} onChange={(e) => rel.setRollbackNote(e.target.value)} />
            </label>
            {rel.error != null && <Notice s="act"><ApiErrorDetail error={rel.error} data-testid="v3-agent-promotion-error" /></Notice>}
          </div>
        </Dialog>
      )}
    </Panel>
  );
}
