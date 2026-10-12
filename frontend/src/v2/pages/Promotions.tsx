import { ArrowRight, Check, X } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { useSearchParams } from "react-router-dom";

import { useAuth } from "../../auth/auth-context";
import {
  api,
  errorMessage,
  type PromotionInfo,
  type PromotionLogLine,
  type PromotionPlan,
  type PromotionStage,
  type SpecDiffRow,
} from "../../lib/api";
import { fmtTime } from "../format";
import { useLoad, useV2Toast } from "../hooks";
import {
  Alert,
  Button,
  Card,
  type Column,
  Confirm,
  Descriptions,
  Field,
  Kpi,
  LinkButton,
  Modal,
  PageHeader,
  Select,
  Spin,
  Table,
  Tag,
  type TagTone,
} from "../ui";

/**
 * T21 — the dev → ops hand-off surface.
 *
 * A member bundles a verified publish and asks for a release; whoever holds
 * `promotion.approve` (an operator, or a member an administrator granted it) reads the
 * change note, the gates and the diff against the target environment, then approves or
 * rejects. Execution into the target is P3 — until then an approved row is the record
 * that the release was accepted, which is the part an audit asks for.
 *
 * `?view=detail&id=` follows the console's sub-page convention.
 */

const STATUS_TONE: Record<PromotionInfo["status"], TagTone> = {
  pending: "orange",
  approved: "green",
  rejected: "red",
  cancelled: "gray",
  executing: "orange",
  succeeded: "green",
  failed: "red",
  rolled_back: "gray",
};

function DiffTable({ rows }: { rows: SpecDiffRow[] }) {
  const { t } = useTranslation();
  if (rows.length === 0) return <p className="v2-muted">{t("v2.promotions.noDiff")}</p>;
  return (
    <div className="v2-promo-diff">
      {rows.map((row) => (
        <div key={row.field} className="v2-promo-diff-row">
          <div className="v2-promo-diff-field mono">
            {row.field}
            <Tag tone={row.kind === "removed" ? "red" : row.kind === "added" ? "green" : undefined}>
              {t(`v2.promotions.diffKind.${row.kind}`)}
            </Tag>
            {row.redacted && <Tag tone="gray">{t("v2.promotions.redacted")}</Tag>}
          </div>
          <pre className="v2-pre v2-promo-diff-before">{row.before}</pre>
          <pre className="v2-pre v2-promo-diff-after">{row.after}</pre>
        </div>
      ))}
    </div>
  );
}

const STAGE_TONE: Record<PromotionStage["status"], TagTone> = {
  pending: "gray",
  running: "orange",
  succeeded: "green",
  skipped: "gray",
  failed: "red",
};

/**
 * T26 — what executing would create or change in the target, read-only, with the gates
 * that would refuse it. Execute lives here so nobody triggers a release without having
 * been shown the plan.
 */
function PlanCard({ promotion, onStarted }: { promotion: PromotionInfo; onStarted: () => void }) {
  const { t } = useTranslation();
  const { can } = useAuth();
  const toast = useV2Toast();
  const plan = useLoad<PromotionPlan>(() => api.promotionPlan(promotion.id), `plan:${promotion.id}`);
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const data = plan.data;

  const run = async () => {
    setBusy(true);
    try {
      await api.executePromotion(promotion.id);
      toast("success", t("v2.promotions.executeStarted"));
      setConfirming(false);
      onStarted();
    } catch (error) {
      toast("error", errorMessage(error));
      setConfirming(false);
      plan.reload();
    } finally {
      setBusy(false);
    }
  };

  return (
    <Card
      title={t("v2.promotions.planTitle")}
      sub={t("v2.promotions.planSub", { target: promotion.target_workspace_id })}
      end={
        can("promotion.approve") && (
          <Button
            kind="primary"
            disabled={!data?.can_execute}
            onClick={() => setConfirming(true)}
            testId="v2-promotion-execute"
          >
            {t("v2.promotions.execute")}
          </Button>
        )
      }
    >
      {plan.loading && !data && <Spin />}
      {plan.error && <Alert tone="error">{plan.error}</Alert>}
      {data && (
        <>
          {data.blocked_by.length > 0 && (
            <Alert tone="warn">
              {t("v2.promotions.blockedBy", { keys: data.blocked_by.join(", ") })}
              {data.gates.next_allowed_at &&
                ` ${t("v2.promotions.nextAllowed", { time: fmtTime(data.gates.next_allowed_at) })}`}
            </Alert>
          )}
          {data.items.map((item) => (
            <div key={item.key} className="v2-promo-gate" data-testid={`v2-plan-${item.key}`}>
              <Tag tone={item.action === "none" || item.action === "skip" ? "gray" : "green"}>
                {t(`v2.promotions.planAction.${item.action}`, { defaultValue: item.action })}
              </Tag>
              <span className="mono">{t(`v2.promotions.planItem.${item.key}`)}</span>
              <span className="v2-muted">{item.detail}</span>
            </div>
          ))}
          {data.observe_seconds > 0 && (
            <p className="v2-muted">
              {t("v2.promotions.observeNote", { seconds: data.observe_seconds })}
            </p>
          )}
        </>
      )}
      <Confirm
        open={confirming}
        title={t("v2.promotions.executeTitle")}
        body={t("v2.promotions.executeBody", { target: promotion.target_workspace_id })}
        confirmLabel={t("v2.promotions.executeConfirm")}
        busy={busy}
        onConfirm={() => void run()}
        onClose={() => setConfirming(false)}
      />
    </Card>
  );
}

/**
 * T27 — one card per stage, polled while the release runs. Rollback re-deploys the
 * previous bundle; it is offered only when the backend recorded one.
 */
function ExecutionCard({
  promotion,
  onChanged,
}: {
  promotion: PromotionInfo;
  onChanged: () => void;
}) {
  const { t } = useTranslation();
  const { can } = useAuth();
  const toast = useV2Toast();
  const [live, setLive] = useState<{ promotion: PromotionInfo; log: PromotionLogLine[] } | null>(
    null,
  );
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const current = live?.promotion ?? promotion;
  const running = current.status === "executing";
  // the parent re-renders with a fresh callback each time; a ref keeps `refresh` stable so
  // the polling effect is not torn down (or looped) by that
  const changed = useRef(onChanged);
  changed.current = onChanged;
  const previous = useRef(promotion.status);

  const refresh = useCallback(async () => {
    try {
      const next = await api.promotionExecution(promotion.id);
      setLive(next);
      if (previous.current === "executing" && next.promotion.status !== "executing") {
        previous.current = next.promotion.status;
        changed.current();
      }
    } catch {
      /* keep the last good view; the next tick retries */
    }
  }, [promotion.id]);

  useEffect(() => {
    void refresh();
  }, [refresh, promotion.status]);
  useEffect(() => {
    if (!running) return undefined;
    const timer = window.setInterval(() => void refresh(), 3000);
    return () => window.clearInterval(timer);
  }, [running, refresh]);

  const stages = current.stages ?? [];
  const log = live?.log ?? [];
  const canRollback =
    can("promotion.approve") && (current.status === "succeeded" || current.status === "failed");

  const rollback = async () => {
    setBusy(true);
    try {
      await api.rollbackPromotion(promotion.id);
      toast("success", t("v2.promotions.rollbackStarted"));
      setConfirming(false);
      previous.current = "executing";
      onChanged();
    } catch (error) {
      toast("error", errorMessage(error));
      setConfirming(false);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Card
      title={
        current.action === "rollback"
          ? t("v2.promotions.execTitleRollback")
          : t("v2.promotions.execTitle")
      }
      sub={t("v2.promotions.execSub")}
      end={
        canRollback && (
          <Button onClick={() => setConfirming(true)} testId="v2-promotion-rollback">
            {t("v2.promotions.rollback")}
          </Button>
        )
      }
    >
      {current.status === "failed" && current.error && (
        <Alert tone="error">
          {current.failed_stage
            ? `${t("v2.promotions.failedAt", { stage: t(`v2.promotions.stage.${current.failed_stage}`) })} — `
            : ""}
          {current.error}
        </Alert>
      )}
      {canRollback && !current.previous_bundle_id && (
        <p className="v2-muted">{t("v2.promotions.noPrevious")}</p>
      )}
      {stages.map((stage) => {
        const lines = log.filter((line) => line.stage === stage.name && line.level !== "debug");
        return (
          <div key={stage.name} className="v2-promo-gate" data-testid={`v2-stage-${stage.name}`}>
            <Tag tone={STAGE_TONE[stage.status]}>
              {running && stage.status === "running" && <Spin />}
              {t(`v2.promotions.stageStatus.${stage.status}`)}
            </Tag>
            <span className="mono">{t(`v2.promotions.stage.${stage.name}`)}</span>
            <span className="v2-muted">
              {stage.detail || lines[lines.length - 1]?.msg || ""}
            </span>
          </div>
        );
      })}
      <Confirm
        open={confirming}
        title={t("v2.promotions.rollbackTitle")}
        body={t("v2.promotions.rollbackBody", { target: promotion.target_workspace_id })}
        confirmLabel={t("v2.promotions.rollbackConfirm")}
        danger
        busy={busy}
        onConfirm={() => void rollback()}
        onClose={() => setConfirming(false)}
      />
    </Card>
  );
}

function ReviewModal({
  promotion,
  decision,
  onClose,
  onDone,
}: {
  promotion: PromotionInfo;
  decision: "approve" | "reject";
  onClose: () => void;
  onDone: () => void;
}) {
  const { t } = useTranslation();
  const toast = useV2Toast();
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const failures = promotion.gates.blocking_failures ?? [];

  const submit = async () => {
    setBusy(true);
    try {
      await api.reviewPromotion(promotion.id, { decision, note: note.trim() || undefined });
      toast("success", t(`v2.promotions.${decision}d`));
      onDone();
      onClose();
    } catch (error) {
      toast("error", errorMessage(error));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal
      title={t(`v2.promotions.${decision}Title`)}
      open
      onClose={onClose}
      footer={
        <>
          <Button onClick={onClose}>{t("common.cancel")}</Button>
          <Button kind="primary" disabled={busy} onClick={() => void submit()}>
            {busy ? <Spin /> : t(`v2.promotions.${decision}Confirm`)}
          </Button>
        </>
      }
    >
      {decision === "approve" && failures.length > 0 && (
        <Alert tone="warn">
          {t("v2.promotions.gatesFailing", { keys: failures.join(", ") })}
        </Alert>
      )}
      <Field label={t("v2.promotions.reviewNote")} hint={t("v2.promotions.reviewNoteHint")}>
        <textarea
          className="v2-textarea"
          rows={4}
          maxLength={4000}
          value={note}
          onChange={(event) => setNote(event.target.value)}
          data-testid="v2-promotion-review-note"
        />
      </Field>
    </Modal>
  );
}

function PromotionDetail({ id, onBack }: { id: string; onBack: () => void }) {
  const { t } = useTranslation();
  const { can } = useAuth();
  const [tick, setTick] = useState(0);
  const detail = useLoad(() => api.getPromotion(id), `promotion:${id}:${tick}`);
  const [review, setReview] = useState<"approve" | "reject" | null>(null);
  const promotion = detail.data;

  if (detail.loading) return <Spin />;
  if (!promotion) {
    return (
      <Card>
        <Alert tone="error">{detail.error ? errorMessage(detail.error) : t("v2.promotions.gone")}</Alert>
      </Card>
    );
  }
  const bundle = promotion.bundle;
  const pending = promotion.status === "pending";

  return (
    <>
      <PageHeader
        title={t("v2.promotions.detailTitle", {
          agent: bundle?.display_name || bundle?.agent_name || promotion.bundle_id,
        })}
        desc={t("v2.promotions.detailDesc", {
          from: promotion.source_workspace_id ?? "—",
          to: promotion.target_workspace_id,
        })}
        end={
          <>
            <Button onClick={onBack}>{t("v2.promotions.backToList")}</Button>
            {pending && can("promotion.approve") && (
              <>
                <Button onClick={() => setReview("reject")} testId="v2-promotion-reject">
                  <X size={13} aria-hidden="true" />
                  {t("v2.promotions.reject")}
                </Button>
                <Button kind="primary" onClick={() => setReview("approve")} testId="v2-promotion-approve">
                  <Check size={13} aria-hidden="true" />
                  {t("v2.promotions.approve")}
                </Button>
              </>
            )}
          </>
        }
      />
      <Card title={t("v2.promotions.request")}>
        <Descriptions
          items={[
            {
              label: t("v2.promotions.colStatus"),
              value: <Tag tone={STATUS_TONE[promotion.status]}>{t(`v2.promotions.status.${promotion.status}`)}</Tag>,
            },
            { label: t("v2.promotions.requestedBy"), value: promotion.requested_by ?? "—" },
            { label: t("v2.promotions.colCreated"), value: fmtTime(promotion.created_at) },
            {
              label: t("v2.promotions.reviewedBy"),
              value: promotion.reviewed_by
                ? `${promotion.reviewed_by} · ${fmtTime(promotion.reviewed_at)}`
                : "—",
            },
            { label: t("v2.promotions.changeNote"), value: promotion.change_note },
            { label: t("v2.promotions.rollbackNote"), value: promotion.rollback_note },
            ...(promotion.review_note
              ? [{ label: t("v2.promotions.reviewNote"), value: promotion.review_note }]
              : []),
          ]}
        />
      </Card>
      <Card title={t("v2.promotions.bundle")} sub={t("v2.promotions.bundleSub")}>
        <Descriptions
          items={[
            { label: t("v2.promotions.digest"), value: <span className="mono">{bundle?.digest}</span> },
            { label: t("v2.promotions.publish"), value: bundle?.snapshot_seq ?? "—" },
            { label: t("v2.promotions.method"), value: bundle?.method ?? "—" },
            {
              label: t("v2.promotions.evaluation"),
              value: (bundle?.evaluation as { run_id?: string })?.run_id ?? t("v2.promotions.noEval"),
            },
          ]}
        />
      </Card>
      <Card title={t("v2.promotions.gates")} sub={t("v2.promotions.gatesSub")}>
        {(promotion.gates.checks ?? []).map((gate) => (
          <div key={gate.key} className="v2-promo-gate">
            <Tag tone={gate.ok ? "green" : "orange"}>{gate.ok ? t("v2.promotions.gateOk") : t("v2.promotions.gateFail")}</Tag>
            <span className="mono">{t(`v2.promotions.gate.${gate.key}`)}</span>
            <span className="v2-muted">{gate.detail}</span>
          </div>
        ))}
      </Card>
      {(promotion.status === "approved" ||
        (promotion.status === "failed" && promotion.action !== "rollback")) && (
        <PlanCard promotion={promotion} onStarted={() => setTick((n) => n + 1)} />
      )}
      {(promotion.stages ?? []).length > 0 && (
        <ExecutionCard promotion={promotion} onChanged={() => setTick((n) => n + 1)} />
      )}
      <Card title={t("v2.promotions.diff")} sub={t("v2.promotions.diffSub")}>
        {promotion.target_visible === false ? (
          <Alert>{t("v2.promotions.targetHidden", { target: promotion.target_workspace_id })}</Alert>
        ) : (
          <DiffTable rows={promotion.diff ?? []} />
        )}
      </Card>
      {review && (
        <ReviewModal
          promotion={promotion}
          decision={review}
          onClose={() => setReview(null)}
          onDone={() => setTick((n) => n + 1)}
        />
      )}
    </>
  );
}

export function V2Promotions() {
  const { t } = useTranslation();
  const [params, setParams] = useSearchParams();
  const [status, setStatus] = useState("");
  const list = useLoad(() => api.listPromotions(status ? { status } : {}), `promotions:${status}`);
  const rows = list.data?.promotions ?? [];
  const id = params.get("id");

  if (params.get("view") === "detail" && id) {
    return (
      <PromotionDetail
        id={id}
        onBack={() => setParams(new URLSearchParams(), { replace: true })}
      />
    );
  }

  const open = (promotionId: string) =>
    setParams(new URLSearchParams({ view: "detail", id: promotionId }));

  const columns: Column<PromotionInfo>[] = [
    {
      key: "agent",
      title: t("v2.promotions.colAgent"),
      render: (row) => (
        <>
          <LinkButton onClick={() => open(row.id)}>
            {row.bundle?.display_name || row.bundle?.agent_name || row.bundle_id}
          </LinkButton>
          <span className="sub mono">{row.bundle?.digest.slice(0, 12)}</span>
        </>
      ),
    },
    {
      key: "route",
      title: t("v2.promotions.colRoute"),
      className: "nowrap",
      render: (row) => (
        <span className="mono">
          {row.source_workspace_id} <ArrowRight size={11} aria-hidden="true" /> {row.target_workspace_id}
        </span>
      ),
    },
    {
      key: "status",
      title: t("v2.promotions.colStatus"),
      render: (row) => (
        <Tag tone={STATUS_TONE[row.status]}>{t(`v2.promotions.status.${row.status}`)}</Tag>
      ),
    },
    { key: "by", title: t("v2.promotions.requestedBy"), render: (row) => row.requested_by ?? "—" },
    {
      key: "created",
      title: t("v2.promotions.colCreated"),
      className: "nowrap",
      render: (row) => fmtTime(row.created_at),
    },
  ];

  const pending = rows.filter((row) => row.status === "pending").length;

  return (
    <>
      <PageHeader title={t("v2.promotions.title")} desc={t("v2.promotions.desc")} />
      <div className="v2-kpis">
        <Kpi label={t("v2.promotions.kpiPending")} value={pending} tone={pending ? "bad" : undefined} />
        <Kpi
          label={t("v2.promotions.kpiApproved")}
          value={rows.filter((row) => row.status === "approved").length}
        />
        <Kpi label={t("v2.promotions.kpiTotal")} value={rows.length} />
      </div>
      <Card
        title={t("v2.promotions.listTitle")}
        flush
        end={
          <Select
            value={status}
            placeholder={t("v2.promotions.allStatuses")}
            options={(["pending", "approved", "rejected"] as const).map((value) => ({
              value,
              label: t(`v2.promotions.status.${value}`),
            }))}
            onChange={setStatus}
            testId="v2-promotions-status"
          />
        }
      >
        <div style={{ padding: "0 24px 16px" }}>
          <Table
            columns={columns}
            rows={rows}
            rowKey={(row) => row.id}
            loading={list.loading}
            error={list.error}
            onRetry={list.reload}
            empty={t("v2.promotions.empty")}
          />
        </div>
      </Card>
    </>
  );
}
