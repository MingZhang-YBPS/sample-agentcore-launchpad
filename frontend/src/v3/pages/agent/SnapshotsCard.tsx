import { GitCompare, RotateCcw } from "lucide-react";
import { useEffect, useRef } from "react";
import { useTranslation } from "react-i18next";

import { ApiErrorDetail } from "../../../components/ApiErrorDetail";
import { showValue, useSnapshotDiff, useSnapshots } from "../../../lib/agent-admin/snapshots";
import type { SpecChange } from "../../../lib/api";
import { ago } from "../../format";
import { useToast } from "../../hooks";
import { Btn, Chip, Confirm, Dialog, Empty, Notice, Panel, Skeleton } from "../../ui";

function ChangeRow({ change }: { change: SpecChange }) {
  const { t } = useTranslation();
  const listy = change.added !== undefined && change.removed !== undefined;
  return (
    <div style={{ display: "grid", gap: 6 }} data-testid="v3-snapshot-change">
      <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
        <span className="mono">{change.field}</span>
        <Chip>{t(`v3.agent.snapshots.group.${change.group}`)}</Chip>
        <Chip s={change.kind === "removed" ? "act" : change.kind === "added" ? "ok" : "wait"}>{t(`v3.agent.snapshots.kind.${change.kind}`)}</Chip>
      </div>
      {listy && (change.added!.length > 0 || change.removed!.length > 0) ? (
        <pre className="v3-pre">
          {[...change.removed!.map((x) => `- ${showValue(x)}`), ...change.added!.map((x) => `+ ${showValue(x)}`)].join("\n")}
        </pre>
      ) : (
        <div className="pair">
          <pre className="v3-pre">{`- ${showValue(change.before)}`}</pre>
          <pre className="v3-pre">{`+ ${showValue(change.after)}`}</pre>
        </div>
      )}
    </div>
  );
}

function DiffDialog({ agentId, pair, onClose }: { agentId: string; pair: [number, number]; onClose: () => void }) {
  const { t } = useTranslation();
  const { data, loading, error } = useSnapshotDiff(agentId, pair);
  return (
    <Dialog
      wide
      title={t("v3.agent.snapshots.diffTitle", { from: pair[0], to: pair[1] })}
      onClose={onClose}
      foot={<Btn kind="ghost" onClick={onClose}>{t("v3.agent.snapshots.close")}</Btn>}
    >
      <div className="v3-agent-diff" data-testid="v3-snapshot-diff">
        {loading && !data ? (
          <Skeleton rows={3} />
        ) : error ? (
          <Notice s="act">{error}</Notice>
        ) : data && data.changes.length === 0 ? (
          <span className="v3-hint">{t("v3.agent.snapshots.noChanges")}</span>
        ) : (
          data?.changes.map((c) => <ChangeRow key={c.field} change={c} />)
        )}
      </div>
    </Dialog>
  );
}

/**
 * Ledger snapshots per publish: pick two to compare, or roll back to an older
 * one (a re-publish, confirmed). `focus` scrolls it into view — the header's
 * "Roll back to a snapshot…" lands here.
 */
export function SnapshotsCard({
  agentId,
  refreshKey,
  canRollback,
  blockedTitle,
  focus,
  onRolledBack,
}: {
  agentId: string;
  refreshKey: string;
  canRollback: boolean;
  /** why rollback is disabled (prod lock, deploying, no permission) */
  blockedTitle?: string;
  focus?: boolean;
  onRolledBack: () => void;
}) {
  const { t } = useTranslation();
  const toast = useToast();
  const ref = useRef<HTMLDivElement>(null);
  const snaps = useSnapshots(agentId, refreshKey, {
    onRolledBack: (seq) => {
      toast("ok", t("v3.agent.snapshots.rolledBack", { seq }));
      onRolledBack();
    },
  });
  useEffect(() => {
    if (focus) ref.current?.scrollIntoView({ behavior: "smooth", block: "start" });
  }, [focus]);

  return (
    <div ref={ref} data-testid="v3-agent-snapshots">
      <Panel
        title={t("v3.agent.snapshots.title")}
        flush
        end={
          <Btn size="sm" disabled={!snaps.canCompare} disabledReason={t("story.reason.twoSnapshots")} onClick={snaps.openDiff}>
            <GitCompare size={13} /> {t("v3.agent.snapshots.compare")}
          </Btn>
        }
      >
        <p className="v3-hint" style={{ margin: "0 20px 12px" }}>{t("v3.agent.snapshots.sub")}</p>
        {snaps.loading && !snaps.data ? (
          <div style={{ padding: 20 }}><Skeleton rows={3} /></div>
        ) : snaps.error ? (
          <div style={{ padding: 20 }}>
            <Notice s="wait">
              {snaps.error} <button type="button" className="v3-btn sm" onClick={snaps.reload}>{t("v3.ux.retry")}</button>
            </Notice>
          </div>
        ) : snaps.rows.length === 0 ? (
          <Empty title={t("v3.agent.snapshots.empty")} />
        ) : (
          <table className="v3-table">
            <thead>
              <tr>
                <th style={{ width: 36 }} />
                <th>#</th>
                <th>{t("v3.agents.version")}</th>
                <th>{t("v3.agent.snapshots.by")}</th>
                <th>{t("v3.agent.snapshots.at")}</th>
                <th>{t("v3.agent.snapshots.note")}</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {snaps.rows.map((r) => (
                <tr key={r.seq}>
                  <td>
                    <input
                      type="checkbox"
                      checked={snaps.picked.includes(r.seq)}
                      onChange={() => snaps.toggle(r.seq)}
                      aria-label={t("v3.agent.snapshots.pick", { seq: r.seq })}
                    />
                  </td>
                  <td className="mono">{r.seq}</td>
                  <td className="mono">{r.aws_version ?? "—"}</td>
                  <td>{r.created_by ?? "—"}</td>
                  <td style={{ color: "var(--v3-text-3)" }}>{ago(r.created_at)}</td>
                  <td style={{ color: "var(--v3-text-2)" }}>{r.note ?? "—"}</td>
                  <td className="num">
                    {r.seq === snaps.currentSeq ? (
                      <Chip s="ok">{t("v3.agent.snapshots.current")}</Chip>
                    ) : (
                      <Btn size="sm" kind="ghost" disabled={!canRollback} disabledReason={blockedTitle ?? t("story.reason.unavailable")} onClick={() => snaps.askRollback(r)}>
                        <RotateCcw size={13} /> {t("v3.agent.snapshots.rollback")}
                      </Btn>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Panel>
      {snaps.diff && <DiffDialog agentId={agentId} pair={snaps.diff} onClose={snaps.closeDiff} />}
      {snaps.rollback && (
        <Confirm
          title={t("v3.agent.snapshots.rollbackTitle", { seq: snaps.rollback.seq })}
          confirmLabel={t("v3.agent.snapshots.rollback")}
          cancelLabel={t("v3.agent.action.cancel")}
          danger
          busy={snaps.busy}
          onConfirm={() => void snaps.confirmRollback()}
          onCancel={snaps.cancelRollback}
        >
          <p style={{ marginTop: 0 }}>{t("v3.agent.snapshots.rollbackBody", { seq: snaps.rollback.seq })}</p>
          {snaps.actionError != null && (
            <Notice s="act"><ApiErrorDetail error={snaps.actionError} data-testid="v3-snapshot-rollback-error" /></Notice>
          )}
        </Confirm>
      )}
    </div>
  );
}
