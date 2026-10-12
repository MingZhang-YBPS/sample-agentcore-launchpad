import { useState } from "react";
import { useTranslation } from "react-i18next";

import { api, errorMessage, type SnapshotDiff, type SpecChange, type SpecSnapshotInfo } from "../../../lib/api";
import { fmtTime } from "../../format";
import { useLoad, useV2Toast } from "../../hooks";
import { Alert, Button, Card, Confirm, LinkButton, Modal, Spin, Table } from "../../ui";

function show(value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (typeof value === "string") return value || "—";
  return JSON.stringify(value, null, 2);
}

function ChangeRow({ change }: { change: SpecChange }) {
  const { t } = useTranslation();
  const listy = change.added !== undefined && change.removed !== undefined;
  return (
    <div className="v2-diff-row" data-testid="v2-snapshot-change">
      <div className="v2-row" style={{ gap: 8 }}>
        <span className="mono">{change.field}</span>
        <span className="v2-muted">{t(`v2.agents.snapshots.group.${change.group}`)}</span>
        <span className="v2-muted">{t(`v2.agents.snapshots.kind.${change.kind}`)}</span>
      </div>
      {listy && (change.added!.length > 0 || change.removed!.length > 0) ? (
        <pre className="v2-pre">
          {[
            ...change.removed!.map((x) => `- ${show(x)}`),
            ...change.added!.map((x) => `+ ${show(x)}`),
          ].join("\n")}
        </pre>
      ) : (
        <div className="v2-grid-2">
          <pre className="v2-pre">{`- ${show(change.before)}`}</pre>
          <pre className="v2-pre">{`+ ${show(change.after)}`}</pre>
        </div>
      )}
    </div>
  );
}

function DiffModal({ agentId, pair, onClose }: { agentId: string; pair: [number, number]; onClose: () => void }) {
  const { t } = useTranslation();
  const [from, to] = pair;
  const { data, loading, error } = useLoad<SnapshotDiff>(
    () => api.snapshotDiff(agentId, from, to),
    `snapdiff:${agentId}:${from}:${to}`,
  );
  return (
    <Modal open wide title={t("v2.agents.snapshots.diffTitle", { from, to })} onClose={onClose} testId="v2-snapshot-diff">
      {loading ? (
        <Spin />
      ) : error ? (
        <Alert tone="error">{error}</Alert>
      ) : data && data.changes.length === 0 ? (
        <span className="v2-muted">{t("v2.agents.snapshots.noChanges")}</span>
      ) : (
        data?.changes.map((c) => <ChangeRow key={c.field} change={c} />)
      )}
    </Modal>
  );
}

/** Ledger snapshots per publish (T18): list, two-snapshot diff, rollback (confirmed). */
export function SnapshotsCard({
  agentId,
  refreshKey,
  canRollback,
  lockTitle,
  onRolledBack,
}: {
  agentId: string;
  /** changes whenever the agent (re)publishes, so the list follows */
  refreshKey: string;
  canRollback: boolean;
  lockTitle?: string;
  onRolledBack: () => void;
}) {
  const { t } = useTranslation();
  const toast = useV2Toast();
  const { data, loading, error } = useLoad(() => api.agentSnapshots(agentId), `snapshots:${agentId}:${refreshKey}`);
  const [picked, setPicked] = useState<number[]>([]);
  const [diff, setDiff] = useState<[number, number] | null>(null);
  const [rollback, setRollback] = useState<SpecSnapshotInfo | null>(null);
  const [busy, setBusy] = useState(false);

  const toggle = (seq: number) =>
    setPicked((prev) => (prev.includes(seq) ? prev.filter((s) => s !== seq) : [...prev, seq].slice(-2)));

  const run = async () => {
    if (!rollback) return;
    setBusy(true);
    try {
      await api.rollbackSnapshot(agentId, rollback.seq);
      toast("success", t("v2.agents.snapshots.rolledBack", { seq: rollback.seq }));
      setRollback(null);
      setPicked([]);
      onRolledBack();
    } catch (err) {
      toast("error", errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  const rows = data?.snapshots ?? [];
  return (
    <Card
      title={t("v2.agents.snapshots.title")}
      sub={t("v2.agents.snapshots.sub")}
      end={
        <Button
          size="sm"
          disabled={picked.length !== 2}
          onClick={() => setDiff([Math.min(...picked), Math.max(...picked)])}
          testId="v2-snapshot-compare"
        >
          {t("v2.agents.snapshots.compare")}
        </Button>
      }
      testId="v2-agent-snapshots"
    >
      {loading && !data ? (
        <Spin />
      ) : error ? (
        <Alert tone="warn">{error}</Alert>
      ) : (
        <Table
          columns={[
            {
              key: "pick",
              title: "",
              render: (r) => (
                <input
                  type="checkbox"
                  checked={picked.includes(r.seq)}
                  onChange={() => toggle(r.seq)}
                  aria-label={t("v2.agents.snapshots.pick", { seq: r.seq })}
                />
              ),
            },
            { key: "seq", title: "#", render: (r) => <span className="mono">{r.seq}</span> },
            { key: "v", title: t("v2.agents.colVersion"), render: (r) => <span className="mono">{r.aws_version ?? "—"}</span> },
            { key: "by", title: t("v2.agents.snapshots.by"), render: (r) => r.created_by ?? "—" },
            { key: "at", title: t("v2.common.createdAt"), className: "nowrap", render: (r) => fmtTime(r.created_at) },
            { key: "note", title: t("v2.agents.snapshots.note"), render: (r) => r.note ?? "—" },
            {
              key: "act",
              title: "",
              render: (r) =>
                r.seq === rows[0]?.seq ? (
                  <span className="v2-muted">{t("v2.agents.snapshots.current")}</span>
                ) : (
                  <LinkButton
                    disabled={!canRollback}
                    title={lockTitle}
                    onClick={() => setRollback(r)}
                    testId={`v2-snapshot-rollback-${r.seq}`}
                  >
                    {t("v2.agents.snapshots.rollback")}
                  </LinkButton>
                ),
            },
          ]}
          rows={rows}
          rowKey={(r) => String(r.seq)}
          density="dense"
        />
      )}
      {diff && <DiffModal agentId={agentId} pair={diff} onClose={() => setDiff(null)} />}
      <Confirm
        open={rollback !== null}
        title={t("v2.agents.snapshots.rollbackTitle", { seq: rollback?.seq ?? "" })}
        body={t("v2.agents.snapshots.rollbackBody", { seq: rollback?.seq ?? "" })}
        confirmLabel={t("v2.agents.snapshots.rollback")}
        danger
        busy={busy}
        onConfirm={() => void run()}
        onClose={() => setRollback(null)}
      />
    </Card>
  );
}
