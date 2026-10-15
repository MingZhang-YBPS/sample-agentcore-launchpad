import { type ReactNode, useState } from "react";
import { useTranslation } from "react-i18next";

import { ErrorCallout } from "../../../components/ErrorCallout";
import {
  checkMcpHealth,
  type McpHealthStatus,
  type McpScaffoldPlan,
  type McpServerOps,
  rollbackMcpServer,
  scaffoldMcpPolicies,
} from "../../../lib/api/mcpReviews";
import { useFailure } from "../../failure";
import { ago } from "../../format";
import { useToast } from "../../hooks";
import { Btn, Chip, Confirm, Notice, type Signal } from "../../ui";

const HEALTH_SIGNAL: Record<McpHealthStatus, Signal> = {
  healthy: "ok",
  drift: "wait",
  withheld: "wait",
  failing: "act",
};

type Mode = "" | "LOG_ONLY" | "ENFORCE";

/**
 * Health, endpoint, rollback and Cedar scaffold of one hosted MCP server (projects
 * §15.7, §15.8). The hourly `mcp.health` task fills the health row; "check now" runs the
 * same check. A rollback pins the named endpoint `live` at an earlier approved version
 * (the next deploy un-pins); the scaffold writes the per-target forbid / permit pair
 * through the governance service and can switch the Gateway mode in the same step.
 */
export function McpServerOpsSection({
  agentId,
  ops,
  version,
  onChanged,
}: {
  agentId: string;
  ops: McpServerOps;
  version: string | null;
  onChanged: () => void;
}) {
  const { t } = useTranslation();
  const toast = useToast();
  const failed = useFailure();
  const [busy, setBusy] = useState(false);
  const [rollbackTo, setRollbackTo] = useState<string | null>(null);
  const [plan, setPlan] = useState<McpScaffoldPlan | null>(null);
  const [mode, setMode] = useState<Mode>("");
  const [confirmName, setConfirmName] = useState("");
  const [reason, setReason] = useState("");

  const run = async (fn: () => Promise<unknown>, ok: string, failKey: string) => {
    setBusy(true);
    try {
      failed.clear();
      await fn();
      toast("ok", t(ok));
      onChanged();
      return true;
    } catch (err) {
      failed.fail(err, t(failKey));
      return false;
    } finally {
      setBusy(false);
    }
  };

  const preview = async () => {
    setBusy(true);
    try {
      failed.clear();
      setPlan((await scaffoldMcpPolicies(agentId, { dry_run: true, ...(mode ? { mode } : {}) })).plan);
    } catch (err) {
      failed.fail(err, t("mcpReview.ops.scaffoldFailed"));
    } finally {
      setBusy(false);
    }
  };

  const apply = () =>
    run(
      () =>
        scaffoldMcpPolicies(agentId, {
          ...(mode ? { mode } : {}),
          ...(mode === "ENFORCE" ? { confirmation_name: confirmName, override_reason: reason || undefined } : {}),
        }),
      "mcpReview.ops.scaffoldStarted",
      "mcpReview.ops.scaffoldFailed",
    ).then((done) => done && setPlan(null));

  return (
    <>
      <ErrorCallout error={failed.error} params={{ agent_id: agentId }} onDismiss={failed.clear} data-testid="v3-mcp-ops-error" />
    <McpServerOpsView
      ops={ops}
      version={version}
      busy={busy}
      plan={plan}
      mode={mode}
      confirmName={confirmName}
      reason={reason}
      onMode={setMode}
      onConfirmName={setConfirmName}
      onReason={setReason}
      onCheck={() => run(() => checkMcpHealth(agentId), "mcpReview.ops.checked", "mcpReview.ops.checkFailed")}
      onRollback={setRollbackTo}
      onPreview={preview}
      onApply={apply}
      confirm={
        rollbackTo !== null && (
          <Confirm
            title={t("mcpReview.ops.rollbackTitle", { version: rollbackTo })}
            confirmLabel={t("mcpReview.ops.rollback")}
            cancelLabel={t("mcpReview.ops.cancel")}
            danger
            busy={busy}
            onCancel={() => setRollbackTo(null)}
            onConfirm={() =>
              run(() => rollbackMcpServer(agentId, rollbackTo), "mcpReview.ops.rollbackStarted", "mcpReview.ops.rollbackFailed").then(() =>
                setRollbackTo(null),
              )
            }
          >
            {t("mcpReview.ops.rollbackBody", { version: rollbackTo })}
          </Confirm>
        )
      }
    />
    </>
  );
}

/** The section's markup from loaded data (split out so it renders without fetching). */
export function McpServerOpsView({
  ops,
  version,
  busy,
  plan,
  mode,
  confirmName,
  reason,
  onMode,
  onConfirmName,
  onReason,
  onCheck,
  onRollback,
  onPreview,
  onApply,
  confirm,
}: {
  ops: McpServerOps;
  version: string | null;
  busy: boolean;
  plan: McpScaffoldPlan | null;
  mode: Mode;
  confirmName: string;
  reason: string;
  onMode: (mode: Mode) => void;
  onConfirmName: (value: string) => void;
  onReason: (value: string) => void;
  onCheck: () => void;
  onRollback: (version: string) => void;
  onPreview: () => void;
  onApply: () => void;
  confirm?: ReactNode;
}) {
  const { t } = useTranslation();
  const health = ops.health ?? {};
  const pinned = ops.qualifier === "live";
  const serving = pinned ? ops.live_version : version;
  const targets = (ops.rollback_versions ?? []).filter((v) => v.version !== serving);
  const op = ops.operation ?? {};
  const changes = plan?.steps.filter((s) => s.action !== "keep") ?? [];

  return (
    <div style={{ display: "grid", gap: 10 }} data-testid="v3-mcp-ops">
      <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
        <strong>{t("mcpReview.ops.health")}</strong>
        <Chip s={health.status ? HEALTH_SIGNAL[health.status] : "off"}>
          {t(`mcpReview.ops.healthState.${health.status ?? "unknown"}`)}
        </Chip>
        {health.checked_at && <span className="v3-hint">{t("mcpReview.ops.checkedAgo", { ago: ago(health.checked_at) })}</span>}
        {health.tool_count != null && <span className="v3-hint">{t("mcpReview.ops.toolCount", { count: health.tool_count })}</span>}
        <span style={{ flex: 1 }} />
        <Btn size="sm" kind="ghost" disabled={busy} onClick={onCheck} title={t("mcpReview.ops.checkHint")}>
          {t("mcpReview.ops.check")}
        </Btn>
      </div>
      {health.alert && <Notice s="act">{t("mcpReview.ops.alert", { count: health.consecutive_failures ?? 0 })}</Notice>}
      {health.error && !health.alert && <Notice s="wait">{health.error}</Notice>}

      <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
        <strong>{t("mcpReview.ops.endpoint")}</strong>
        <Chip s={pinned ? "wait" : "ok"}>
          {pinned ? t("mcpReview.ops.pinned", { version: ops.live_version ?? "—" }) : t("mcpReview.ops.followsDefault")}
        </Chip>
        <span style={{ flex: 1 }} />
        {targets.slice(0, 3).map((v) => (
          <Btn key={v.review_id} size="sm" kind="ghost" busy={busy} disabled={op.status === "running"}
            disabledReason={t("story.reason.waitRunning")} onClick={() => onRollback(v.version)}>
            {t("mcpReview.ops.rollbackTo", { version: v.version })}
          </Btn>
        ))}
      </div>
      {targets.length === 0 && <div className="v3-hint">{t("mcpReview.ops.noRollback")}</div>}
      {pinned && <div className="v3-hint">{t("mcpReview.ops.pinnedHint")}</div>}

      {op.kind && (
        <Notice s={op.status === "failed" ? "act" : op.status === "running" ? "wait" : "ok"}>
          {t(`mcpReview.ops.operation.${op.kind}`, { version: op.version ?? "" })} · {t(`mcpReview.ops.opStatus.${op.status ?? "running"}`)}
          {op.error ? ` — ${op.error}` : ""}
        </Notice>
      )}

      <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
        <strong>{t("mcpReview.ops.cedar")}</strong>
        <select className="v3-input" value={mode} onChange={(e) => onMode(e.target.value as Mode)} aria-label={t("mcpReview.ops.mode")}>
          <option value="">{t("mcpReview.ops.modeKeep")}</option>
          <option value="LOG_ONLY">LOG_ONLY</option>
          <option value="ENFORCE">ENFORCE</option>
        </select>
        <span style={{ flex: 1 }} />
        <Btn size="sm" kind="ghost" disabled={busy} onClick={onPreview}>
          {t("mcpReview.ops.preview")}
        </Btn>
      </div>
      <div className="v3-hint">{t("mcpReview.ops.cedarHint")}</div>
      {mode === "ENFORCE" && (
        <div style={{ display: "grid", gap: 6 }}>
          <input
            className="v3-input"
            placeholder={t("mcpReview.ops.confirmName")}
            aria-label={t("mcpReview.ops.confirmName")}
            value={confirmName}
            onChange={(e) => onConfirmName(e.target.value)}
          />
          <input
            className="v3-input"
            placeholder={t("mcpReview.ops.overrideReason")}
            aria-label={t("mcpReview.ops.overrideReason")}
            value={reason}
            maxLength={1000}
            onChange={(e) => onReason(e.target.value)}
          />
        </div>
      )}
      {plan && (
        <div style={{ display: "grid", gap: 8 }}>
          <div className="v3-hint">{t("mcpReview.ops.planMeta", { gateway: plan.gateway_name ?? plan.gateway_id, mode: plan.mode ?? "—" })}</div>
          {plan.steps.map((step) => (
            <div key={step.name}>
              <Chip s={step.action === "keep" ? "off" : step.action === "delete" ? "act" : "wait"}>{t(`mcpReview.ops.action.${step.action}`)}</Chip>{" "}
              <span className="mono">{step.name}</span>
              {step.statement && (
                <pre className="mono" style={{ fontSize: 11, whiteSpace: "pre-wrap" }}>
                  {step.statement}
                </pre>
              )}
            </div>
          ))}
          <div>
            <Btn kind="primary" size="sm" busy={busy} disabled={changes.length === 0 && !mode} disabledReason={t("story.reason.noChanges")} onClick={onApply}>
              {t("mcpReview.ops.apply")}
            </Btn>
          </div>
        </div>
      )}
      {confirm}
    </div>
  );
}
