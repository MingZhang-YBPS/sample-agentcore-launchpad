import { useTranslation } from "react-i18next";
import { useNavigate } from "react-router-dom";

import { useAuth } from "../../../auth/auth-context";
import { releasable, useRelease } from "../../../lib/agent-admin/release";
import { errorMessage } from "../../../lib/api";
import { fmtTime } from "../../format";
import { useV2Toast } from "../../hooks";
import { Alert, Button, Card, Field, Modal, Select, Spin, Tag } from "../../ui";

/**
 * T20/T21 — bundle this agent's publish and ask for a release.
 *
 * Two steps, deliberately separate: bundling freezes what was tested (idempotent, so
 * pressing it twice returns the same digest), and a promotion is the request a second
 * person reviews. The card only appears for an active agent — there is nothing to
 * release otherwise — and the target list excludes this workspace.
 */
export function ReleaseCard({
  agentId,
  agentTitle,
  agentStatus,
  systemManaged,
  currentWorkspaceId,
}: {
  agentId: string;
  agentTitle: string;
  agentStatus: string;
  systemManaged: boolean;
  currentWorkspaceId: string | null;
}) {
  const { t } = useTranslation();
  const { can } = useAuth();
  const toast = useV2Toast();
  const navigate = useNavigate();
  const rel = useRelease(
    { agentId, agentTitle, currentWorkspaceId },
    {
      onBundled: (created) => toast("success", t("v2.release.bundled", { digest: created.digest.slice(0, 12) })),
      onPublished: () => toast("success", t("v2.release.published")),
      onRequested: (id) => {
        toast("success", t("v2.release.requested"));
        navigate(`/v2/promotions?view=detail&id=${encodeURIComponent(id)}`);
      },
      onError: (error) => toast("error", errorMessage(error)),
    },
  );
  const { bundles, rows, targets, busy, asking, target, changeNote, rollbackNote } = rel;

  if (!releasable({ status: agentStatus, system: systemManaged })) return null;

  return (
    <Card
      title={t("v2.release.title")}
      sub={t("v2.release.sub")}
      testId="v2-agent-release"
      end={
        <>
          <Button size="sm" disabled={busy} onClick={() => void rel.publish()} testId="v2-agent-publish">
            {t("v2.release.publish")}
          </Button>
          {can("promotion.request") && (
            <Button size="sm" disabled={busy} onClick={() => void rel.bundle()} testId="v2-agent-bundle">
              {busy ? <Spin /> : t("v2.release.bundle")}
            </Button>
          )}
        </>
      }
    >
      {bundles.loading && <Spin />}
      {!bundles.loading && rows.length === 0 && (
        <p className="v2-muted">{t("v2.release.empty")}</p>
      )}
      {rows.map((row) => (
        <div key={row.id} className="v2-promo-gate">
          <span className="mono">{row.digest.slice(0, 12)}</span>
          <Tag tone="gray">
            {row.snapshot_seq ? t("v2.release.publishN", { n: row.snapshot_seq }) : t("v2.release.live")}
          </Tag>
          <span className="v2-muted">{fmtTime(row.created_at)}</span>
          {can("promotion.request") && targets.length > 0 && (
            <span style={{ marginLeft: "auto" }}>
            <Button
              size="sm"
              onClick={() => rel.ask(row)}
              testId={`v2-agent-promote-${row.id}`}
            >
              {t("v2.release.request")}
            </Button>
            </span>
          )}
        </div>
      ))}
      {targets.length === 0 && rows.length > 0 && (
        <Alert>{t("v2.release.noTargets")}</Alert>
      )}
      {asking && (
        <Modal
          title={t("v2.release.requestTitle")}
          open
          onClose={() => rel.cancelAsk()}
          footer={
            <>
              <Button onClick={() => rel.cancelAsk()}>{t("common.cancel")}</Button>
              <Button
                kind="primary"
                disabled={!rel.canRequest}
                onClick={() => void rel.request()}
                testId="v2-agent-promote-submit"
              >
                {busy ? <Spin /> : t("v2.release.requestConfirm")}
              </Button>
            </>
          }
        >
          <div className="v2-form">
            <Field label={t("v2.release.target")} hint={t("v2.release.targetHint")}>
              <Select
                value={target}
                options={targets.map((row) => ({
                  value: row.id,
                  label: `${row.name} · ${row.id}${row.tier && row.tier !== "dev" ? ` (${row.tier})` : ""}`,
                }))}
                onChange={rel.setTarget}
                testId="v2-agent-promote-target"
              />
            </Field>
            <Field label={t("v2.release.changeNote")} required hint={t("v2.release.changeNoteHint")}>
              <textarea
                className="v2-textarea"
                rows={3}
                maxLength={4000}
                value={changeNote}
                onChange={(event) => rel.setChangeNote(event.target.value)}
                data-testid="v2-agent-promote-change"
              />
            </Field>
            <Field
              label={t("v2.release.rollbackNote")}
              required
              hint={t("v2.release.rollbackNoteHint")}
            >
              <textarea
                className="v2-textarea"
                rows={3}
                maxLength={4000}
                value={rollbackNote}
                onChange={(event) => rel.setRollbackNote(event.target.value)}
                data-testid="v2-agent-promote-rollback"
              />
            </Field>
          </div>
        </Modal>
      )}
    </Card>
  );
}
