import "./costTags.css";

import { RefreshCw, ShieldAlert, Zap } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";

import { useAuth } from "../../../auth/auth-context";
import { ApiErrorDetail } from "../../../components/ApiErrorDetail";
import { ErrorCallout } from "../../../components/ErrorCallout";
import { type ActivationStatus, activationSummary, costTagsApi, type CostTagsView } from "../../../lib/api/costTags";
import { useWorkspace } from "../../../workspace/workspace-context";
import { useFailure } from "../../failure";
import { ago } from "../../format";
import { useLoad, useToast } from "../../hooks";
import { Btn, Chip, Empty, Lamp, Notice, PageHead, Panel, type Signal, Skeleton, Track } from "../../ui";
import { activatable, stageOf, TAG_SIGNAL } from "./model";

/** The activation meter and the one action: activate here, or what to ask for. */
export function Activation({ status, onActivated }: { status: ActivationStatus; onActivated: () => void }) {
  const { t } = useTranslation();
  const toast = useToast();
  const failed = useFailure();
  const [busy, setBusy] = useState(false);
  const sum = activationSummary(status);
  const todo = activatable(status);
  const pct = (n: number) => `${sum.total ? (n / sum.total) * 100 : 0}%`;

  const activate = async () => {
    setBusy(true);
    failed.clear();
    try {
      const result = await costTagsApi.activate();
      toast("ok", t("v3.costTags.activated", { count: result.activated.length }));
      if (result.not_found.length) toast("ok", t("v3.costTags.notFoundYet", { count: result.not_found.length }));
      onActivated();
    } catch (err) {
      failed.fail(err);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Panel title={t("v3.costTags.activation")} signal={status.error ? "act" : sum.active === sum.total ? "ok" : "wait"}
      end={<Chip>{t(`v3.costTags.account.${status.account_type}`)}</Chip>}>
      <div className="v3-ctag-act" data-testid="cost-tags-activation">
        <ErrorCallout error={failed.error} onDismiss={failed.clear} data-testid="cost-tags-activate-error" />
        <div className="big">
          {status.error ? "—" : sum.active}
          <small>{t("v3.costTags.ofActive", { total: sum.total })}</small>
        </div>
        <div className="v3-ctag-meter" aria-hidden="true">
          <i data-s="ok" style={{ width: pct(sum.active) }} />
          <i data-s="wait" style={{ width: pct(sum.inactive) }} />
          <i data-s="off" style={{ width: pct(sum.notFound) }} />
        </div>
        <div className="v3-ctag-legend">
          <span><Lamp s="ok" />{t("v3.costTags.status.active")} {sum.active}</span>
          <span><Lamp s="wait" />{t("v3.costTags.status.inactive")} {sum.inactive}</span>
          <span><Lamp s="off" />{t("v3.costTags.status.not_found")} {sum.notFound}</span>
        </div>
        {status.error ? (
          <Notice s="act">
            {t("v3.costTags.unreadable", { code: status.error.code })}
            <div className="v3-hint" style={{ marginTop: 4 }}>{status.error.message}</div>
          </Notice>
        ) : status.account_type === "member" ? (
          <Notice s="info">
            <b>{t("v3.costTags.memberTitle")}</b>
            <ol className="v3-ctag-steps" style={{ marginTop: 6 }}>
              <li>{t("v3.costTags.memberStep1", { account: status.management_account_id ?? "—" })}</li>
              <li>{t("v3.costTags.memberStep2")}</li>
              <li>{t("v3.costTags.memberStep3", { keys: status.tags.filter((x) => x.status !== "active").map((x) => x.key).join(", ") || "—" })}</li>
            </ol>
          </Notice>
        ) : todo.length > 0 ? (
          <div style={{ display: "grid", gap: 6 }}>
            <Btn kind="primary" disabled={busy || !status.can_activate} onClick={() => void activate()}>
              <Zap size={14} /> {t("v3.costTags.activate", { count: todo.length })}
            </Btn>
            <small className="v3-hint">{t("v3.costTags.activateHint", { hours: status.activation_delay_hours })}</small>
          </div>
        ) : sum.notFound > 0 ? (
          <small className="v3-hint">{t("v3.costTags.waitingForBilling")}</small>
        ) : (
          <small className="v3-hint">{t("v3.costTags.allActive")}</small>
        )}
        <small className="v3-hint">{t("v3.costTags.checked", { when: ago(status.checked_at), account: status.account_id })}</small>
      </div>
    </Panel>
  );
}

export function Ledger({ view }: { view: CostTagsView }) {
  const { t } = useTranslation();
  const set = new Map(view.tag_set.map((e) => [e.key, e]));
  return (
    <Panel flush title={t("v3.costTags.ledger")} end={<span className="v3-hint">{t("v3.costTags.ledgerEnd", { shared: view.shared_value })}</span>}>
      <div className="v3-table-scroll">
        <table className="v3-table" data-testid="cost-tags-ledger">
          <thead>
            <tr>
              <th>{t("v3.costTags.col.key")}</th>
              <th>{t("v3.costTags.col.value")}</th>
              <th>{t("v3.costTags.col.role")}</th>
              <th>{t("v3.costTags.col.status")}</th>
              <th>{t("v3.costTags.col.lastUsed")}</th>
            </tr>
          </thead>
          <tbody>
            {view.activation.tags.map((tag) => {
              const entry = set.get(tag.key);
              const s: Signal = TAG_SIGNAL[tag.status];
              const role = tag.custom ? "custom" : entry?.role ?? "dimension";
              return (
                <tr key={tag.key} data-testid={`cost-tag-${tag.key}`}>
                  <td><span className="v3-ctag" data-s={s} data-primary={role === "primary"}>{tag.key}</span></td>
                  <td style={{ color: "var(--v3-text-2)" }}>{entry ? t(`v3.costTags.value.${tag.key.replace("launchpad:", "")}`, { defaultValue: entry.value }) : t("v3.costTags.value.custom")}</td>
                  <td><Chip s={role === "primary" ? "info" : undefined}>{t(`v3.costTags.role.${role}`)}</Chip></td>
                  <td><span style={{ display: "inline-flex", gap: 8, alignItems: "center" }}><Lamp s={s} />{t(`v3.costTags.status.${tag.status}`)}</span></td>
                  <td className="mono" style={{ color: "var(--v3-text-3)" }}>{tag.last_used ? ago(tag.last_used) : "—"}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </Panel>
  );
}

/**
 * Configure → Cost tags (cost-tags design §1.4, §1.6; administrators): the tag set the
 * platform writes on every resource, each key's cost allocation state in Billing and
 * the one-click activation (instructions instead for an organization member account),
 * how a billed project line is put together, and the backfill of existing resources.
 * Workspace-scoped: the activation belongs to the workspace's AWS account.
 */
export function V3CostTags() {
  const { t } = useTranslation();
  const { isAdmin } = useAuth();
  const { current } = useWorkspace();
  const [tick, setTick] = useState(0);
  const [force, setForce] = useState(false);
  const loaded = useLoad(async () => (isAdmin ? costTagsApi.get(force) : null), `v3-cost-tags:${isAdmin}:${current?.id ?? ""}:${tick}`);
  const view = loaded.data;
  const refresh = (hard: boolean) => {
    setForce(hard);
    setTick((n) => n + 1);
  };

  if (!isAdmin) {
    return (
      <div className="v3-reveal" style={{ display: "grid", gap: 16 }}>
        <PageHead eyebrow={t("v3.costTags.eyebrow")} title={t("v3.costTags.title")} sub={t("auth.adminRequired.meta")} />
        <Panel>
          <Empty title={<span style={{ display: "inline-flex", gap: 8, alignItems: "center" }}><ShieldAlert size={16} /> {t("auth.adminRequired.title")}</span>}>
            {t("usersPage.forbiddenBody")}
          </Empty>
        </Panel>
      </div>
    );
  }

  const stage = view ? stageOf(view.activation) : null;
  const trackSignal = (lit: boolean, here: boolean): Signal => (lit ? "ok" : here ? "wait" : "off");

  return (
    <div className="v3-reveal" style={{ display: "grid", gap: 16 }}>
      <PageHead
        eyebrow={t("v3.costTags.eyebrow")}
        title={t("v3.costTags.title")}
        sub={t("v3.costTags.sub", { workspace: current?.name ?? current?.id ?? "—" })}
        end={
          <>
            <Btn kind="ghost" disabled={loaded.loading} onClick={() => refresh(true)}><RefreshCw size={14} /> {t("v3.costTags.recheck")}</Btn>
            <Link className="v3-btn" to="/v3/costs?basis=billing">{t("v3.costTags.openBilling")}</Link>
          </>
        }
      />
      {loaded.loading && !view ? (
        <Panel><Skeleton rows={5} /></Panel>
      ) : !view ? (
        <Notice s="act"><ApiErrorDetail error={loaded.failure ?? loaded.error} /></Notice>
      ) : (
        <>
          <div className="v3-ctag-hero">
            <Panel title={t("v3.costTags.path")}>
              <div style={{ display: "grid", gap: 14 }}>
                <Track nodes={[
                  { key: "tagged", s: "ok", label: t("v3.costTags.stage.tagged"), detail: t("v3.costTags.stage.taggedDetail") },
                  { key: "active", s: stage === "unreadable" ? "act" : trackSignal(stage !== "inactive", stage === "inactive"),
                    here: stage === "inactive" || stage === "unreadable", label: t("v3.costTags.stage.active"),
                    detail: t(`v3.costTags.stage.activeDetail.${stage}`) },
                  { key: "settling", s: trackSignal(stage === "billing", stage === "settling"), here: stage === "settling",
                    label: t("v3.costTags.stage.settling"), detail: t("v3.costTags.stage.settlingDetail", { hours: view.activation.activation_delay_hours }) },
                  { key: "billed", s: stage === "billing" ? "ok" : "off", here: stage === "billing",
                    label: t("v3.costTags.stage.billed"), detail: t("v3.costTags.stage.billedDetail") },
                ]} />
                <small className="v3-hint">{t("v3.costTags.pathHint")}</small>
              </div>
            </Panel>
            <Activation status={view.activation} onActivated={() => refresh(false)} />
          </div>

          <Ledger view={view} />

          <Panel flush title={t("v3.costTags.sumTitle")}>
            <div className="v3-ctag-sum">
              <div>
                <span className="op">{t("v3.costTags.sum.taggedOp")}</span>
                <b>{t("v3.costTags.sum.tagged")}</b>
                <p>{t("v3.costTags.sum.taggedBody")}</p>
              </div>
              <div>
                <span className="op">{t("v3.costTags.sum.sharedOp")}</span>
                <b>{t("v3.costTags.sum.shared")}</b>
                <p>{t("v3.costTags.sum.sharedBody", { shared: view.shared_value })}</p>
              </div>
              <div>
                <span className="op">{t("v3.costTags.sum.untaggedOp")}</span>
                <b>{t("v3.costTags.sum.untagged")}</b>
                <p>{t("v3.costTags.sum.untaggedBody")}</p>
              </div>
            </div>
          </Panel>

          <Panel title={t("v3.costTags.backfill")}>
            <div className="v3-ctag-slot" data-testid="cost-tags-backfill">
              <b>{t("v3.costTags.backfillPending")}</b>
              <span>{t("v3.costTags.backfillBody")}</span>
            </div>
          </Panel>
        </>
      )}
    </div>
  );
}
