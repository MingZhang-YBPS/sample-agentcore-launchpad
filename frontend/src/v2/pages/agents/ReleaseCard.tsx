import { useState } from "react";
import { useTranslation } from "react-i18next";
import { useNavigate } from "react-router-dom";

import { useAuth } from "../../../auth/auth-context";
import { api, errorMessage, type ReleaseBundleInfo, type Workspace } from "../../../lib/api";
import { fmtTime } from "../../format";
import { useLoad, useV2Toast } from "../../hooks";
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
  const [tick, setTick] = useState(0);
  const bundles = useLoad(
    () => api.agentReleaseBundles(agentId),
    `bundles:${agentId}:${tick}`,
  );
  const workspaces = useLoad(() => api.listWorkspaces(), "release-workspaces");
  const [asking, setAsking] = useState<ReleaseBundleInfo | null>(null);
  const [busy, setBusy] = useState(false);
  const [changeNote, setChangeNote] = useState("");
  const [rollbackNote, setRollbackNote] = useState("");
  const [target, setTarget] = useState("");

  if (systemManaged || agentStatus !== "active") return null;

  const rows = bundles.data?.bundles ?? [];
  const targets = (workspaces.data?.workspaces ?? []).filter(
    (row: Workspace) => row.id !== currentWorkspaceId,
  );

  const bundle = async () => {
    setBusy(true);
    try {
      const created = await api.createReleaseBundle(agentId);
      toast("success", t("v2.release.bundled", { digest: created.digest.slice(0, 12) }));
      setTick((n) => n + 1);
    } catch (error) {
      toast("error", errorMessage(error));
    } finally {
      setBusy(false);
    }
  };

  // T38: publishing shares the agent's shape with other workspaces. Environment ids and
  // secrets are stripped server-side, so the console does not have to decide what is safe.
  const publish = async () => {
    setBusy(true);
    try {
      await api.publishTemplate({ agent_id: agentId, title: agentTitle });
      toast("success", t("v2.release.published"));
    } catch (error) {
      toast("error", errorMessage(error));
    } finally {
      setBusy(false);
    }
  };

  const request = async () => {
    if (!asking) return;
    setBusy(true);
    try {
      const created = await api.createPromotion({
        bundle_id: asking.id,
        target_workspace_id: target,
        change_note: changeNote.trim(),
        rollback_note: rollbackNote.trim(),
      });
      toast("success", t("v2.release.requested"));
      setAsking(null);
      navigate(`/v2/promotions?view=detail&id=${encodeURIComponent(created.id)}`);
    } catch (error) {
      toast("error", errorMessage(error));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Card
      title={t("v2.release.title")}
      sub={t("v2.release.sub")}
      testId="v2-agent-release"
      end={
        <>
          <Button size="sm" disabled={busy} onClick={() => void publish()} testId="v2-agent-publish">
            {t("v2.release.publish")}
          </Button>
          {can("promotion.request") && (
            <Button size="sm" disabled={busy} onClick={() => void bundle()} testId="v2-agent-bundle">
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
              onClick={() => {
                setAsking(row);
                setTarget(targets[0].id);
                setChangeNote("");
                setRollbackNote("");
              }}
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
          onClose={() => setAsking(null)}
          footer={
            <>
              <Button onClick={() => setAsking(null)}>{t("common.cancel")}</Button>
              <Button
                kind="primary"
                disabled={busy || !changeNote.trim() || !rollbackNote.trim() || !target}
                onClick={() => void request()}
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
                onChange={setTarget}
                testId="v2-agent-promote-target"
              />
            </Field>
            <Field label={t("v2.release.changeNote")} required hint={t("v2.release.changeNoteHint")}>
              <textarea
                className="v2-textarea"
                rows={3}
                maxLength={4000}
                value={changeNote}
                onChange={(event) => setChangeNote(event.target.value)}
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
                onChange={(event) => setRollbackNote(event.target.value)}
                data-testid="v2-agent-promote-rollback"
              />
            </Field>
          </div>
        </Modal>
      )}
    </Card>
  );
}
