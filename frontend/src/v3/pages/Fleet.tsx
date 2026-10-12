import "./fleet.css";

import { ArrowRight, RefreshCw, Trash2 } from "lucide-react";
import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link, useNavigate, useSearchParams } from "react-router-dom";

import { api, errorMessage, type FleetRow, type SharedTemplateInfo } from "../../lib/api";
import { ago } from "../format";
import { useLoad, useToast } from "../hooks";
import { Btn, Chip, Confirm, Empty, Filters, Lamp, Notice, PageHead, Panel, type Signal, Skeleton, Stat } from "../ui";
import { findingSignal, rowProblems, rowSignal, TIER_SIGNAL } from "./fleet/signal";

type View = "fleet" | "health" | "market";
const VIEWS: View[] = ["fleet", "health", "market"];

/* ── fleet: every environment, worst first ──────────────────────────────── */

function FleetView() {
  const { t } = useTranslation();
  const report = useLoad(() => api.fleet(), "v3-fleet");
  const [state, setState] = useState<"all" | Signal>("all");
  const data = report.data;
  const rows = useMemo(() => {
    const order: Record<Signal, number> = { act: 0, wait: 1, info: 2, off: 3, ok: 4 };
    return [...(data?.workspaces ?? [])]
      .filter((row) => state === "all" || rowSignal(row) === state)
      .sort((a, b) => order[rowSignal(a)] - order[rowSignal(b)] || a.name.localeCompare(b.name));
  }, [data, state]);
  const count = (s: Signal) => (data?.workspaces ?? []).filter((row) => rowSignal(row) === s).length;
  const attention = data?.totals.needs_attention ?? 0;

  return (
    <div style={{ display: "grid", gap: 16 }}>
      <div className="v3-grid c4">
        <Panel>
          <Stat label={t("v3.fleet.workspaces")} value={data?.totals.workspaces ?? "—"}
            foot={data ? t("v3.fleet.readableFoot", { count: data.totals.readable }) : undefined} />
        </Panel>
        {/* red only when an environment is red; failed jobs or an unreadable one are amber */}
        <Panel signal={!data ? undefined : count("act") ? "act" : attention > 0 ? "wait" : "ok"}>
          <Stat label={t("v3.fleet.attention")} value={data ? attention : "—"}
            signal={count("act") ? "act" : attention > 0 ? "wait" : undefined}
            foot={t("v3.fleet.attentionFoot")} />
        </Panel>
        <Panel><Stat label={t("v3.fleet.agents")} value={data?.totals.agents_active ?? "—"} foot={t("v3.fleet.agentsFoot")} /></Panel>
        <Panel signal={(data?.totals.alerts_firing ?? 0) > 0 ? "act" : undefined}>
          <Stat label={t("v3.fleet.alerts")} value={data?.totals.alerts_firing ?? "—"}
            signal={(data?.totals.alerts_firing ?? 0) > 0 ? "act" : undefined}
            foot={t("v3.fleet.promotionsFoot", { count: data?.totals.promotions_pending ?? 0 })} />
        </Panel>
      </div>

      <div style={{ display: "flex", gap: 12, alignItems: "center", flexWrap: "wrap" }}>
        <Filters
          value={state}
          onChange={setState}
          options={[
            { value: "all", label: t("v3.fleet.all"), count: data?.workspaces.length ?? 0 },
            { value: "act", label: t("v3.fleet.sAct"), s: "act", count: count("act") },
            { value: "wait", label: t("v3.fleet.sWait"), s: "wait", count: count("wait") },
            { value: "ok", label: t("v3.fleet.sOk"), s: "ok", count: count("ok") },
          ]}
        />
        <span style={{ marginLeft: "auto", color: "var(--v3-text-3)", fontSize: 12 }} className="mono">
          {data ? t("v3.fleet.generated", { ago: ago(data.generated_at) }) : ""}
        </span>
        <Btn size="sm" kind="ghost" onClick={report.reload} disabled={report.loading}><RefreshCw size={13} /></Btn>
      </div>

      <Panel title={t("v3.fleet.tableTitle")} flush>
        {report.loading && !data ? (
          <div style={{ padding: 20 }}><Skeleton rows={4} /></div>
        ) : report.error ? (
          <div style={{ padding: 20 }}><Notice s="act">{report.error}</Notice></div>
        ) : rows.length === 0 ? (
          <Empty title={(data?.workspaces.length ?? 0) ? t("v3.fleet.none") : t("v2.fleet.empty")} />
        ) : (
          <table className="v3-table">
            <thead>
              <tr>
                <th />
                <th>{t("v3.fleet.colWorkspace")}</th>
                <th>{t("v3.fleet.colTier")}</th>
                <th className="num">{t("v3.fleet.colAgents")}</th>
                <th>{t("v3.fleet.colProblems")}</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row: FleetRow) => {
                const s = rowSignal(row);
                const problems = rowProblems(row);
                return (
                  <tr key={row.id}>
                    <td style={{ width: 30 }}><Lamp s={s} live={s === "ok"} /></td>
                    <td>
                      <div className="v3-name">
                        <div>
                          <b><Link to="/v2/workspaces">{row.name}</Link></b>
                          <small>{row.account_id} · {row.region}{row.cross_account ? ` · ${t("v3.fleet.crossAccount")}` : ""}</small>
                        </div>
                      </div>
                    </td>
                    <td><Chip s={TIER_SIGNAL[row.tier]}>{t(`v2.workspaces.tier.${row.tier}`, { defaultValue: row.tier })}</Chip></td>
                    {/* a blank, not a zero: this environment could not be read */}
                    <td className="num">{row.readable ? row.agents_active : "—"}</td>
                    <td>
                      {!row.readable ? (
                        <span className="v3-flt-tags">
                          <Chip s="wait">{t(`v2.workspaces.status.${row.bootstrap_status}`, { defaultValue: row.bootstrap_status })}</Chip>
                          <span style={{ color: "var(--v3-text-3)" }}>{t("v2.fleet.unreadable")}</span>
                        </span>
                      ) : problems.length === 0 ? (
                        <span style={{ color: "var(--v3-text-3)" }}>{t("v3.fleet.clear")}</span>
                      ) : (
                        <span className="v3-flt-tags">
                          {problems.map((p) => (
                            <Chip key={p.key} s={p.s}>{t(`v2.fleet.${p.key}`, { count: p.count })}</Chip>
                          ))}
                        </span>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </Panel>
    </div>
  );
}

/* ── governance health of the selected workspace ────────────────────────── */

function HealthView() {
  const { t } = useTranslation();
  const health = useLoad(() => api.governanceHealth(), "v3-gov-health");
  const data = health.data;
  const gradeSignal: Signal | undefined = data ? (data.grade === "good" ? "ok" : data.grade === "fair" ? "wait" : "act") : undefined;
  const findings = [...(data?.findings ?? [])].sort((a, b) => {
    const order = { action: 0, warn: 1, info: 2 };
    return order[a.severity] - order[b.severity] || b.count - a.count;
  });

  return (
    <div style={{ display: "grid", gap: 16 }}>
      {health.error && <Notice s="act">{health.error}</Notice>}
      <div className="v3-grid c3">
        <Panel signal={gradeSignal}>
          <Stat label={t("v3.fleet.score")} value={data ? data.score : "—"} signal={gradeSignal}
            foot={data ? t(`v2.fleet.grade.${data.grade}`) : undefined} />
        </Panel>
        <Panel><Stat label={t("v3.fleet.findings")} value={data?.findings.length ?? "—"} /></Panel>
        <Panel><Stat label={t("v3.fleet.considered")} value={data?.agents_considered ?? "—"} foot={data ? ago(data.generated_at) : undefined} /></Panel>
      </div>
      <Panel title={t("v3.fleet.findingsTitle")} flush end={<span style={{ color: "var(--v3-text-3)" }}>{t("v2.fleet.findingsSub")}</span>}>
        {health.loading && !data ? (
          <div style={{ padding: 20 }}><Skeleton rows={3} /></div>
        ) : findings.length === 0 ? (
          <Empty title={t("v2.fleet.noFindings")} />
        ) : (
          <table className="v3-table">
            <tbody>
              {findings.map((f) => (
                <tr key={f.key} data-testid={`v3-health-${f.key}`}>
                  <td style={{ width: 30 }}><Lamp s={findingSignal(f.severity)} /></td>
                  <td style={{ width: 1, whiteSpace: "nowrap" }}>
                    <Chip s={findingSignal(f.severity)}>{t(`v2.inbox.severity.${f.severity}`)}</Chip>
                  </td>
                  <td>
                    <div className="v3-name">
                      <div>
                        {/* the copy reads on from the count ("3 running agents …") */}
                        <b><span className="mono">{f.count}</span> {t(`v2.fleet.finding.${f.key}`, { count: f.count })}</b>
                        {f.sample.length > 0 && <small>{f.sample.join(" · ")}</small>}
                      </div>
                    </div>
                  </td>
                  <td style={{ width: 1, whiteSpace: "nowrap" }}>
                    <Link to={f.to} className="v3-btn sm">{t("v3.fleet.fix")} <ArrowRight size={13} /></Link>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Panel>
    </div>
  );
}

/* ── shared templates ───────────────────────────────────────────────────── */

function MarketView() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const toast = useToast();
  const [tick, setTick] = useState(0);
  const list = useLoad(() => api.sharedTemplates(), `v3-templates:${tick}`);
  const [withdrawing, setWithdrawing] = useState<SharedTemplateInfo | null>(null);
  const [busy, setBusy] = useState(false);
  const rows = list.data?.templates ?? [];

  const take = async (row: SharedTemplateInfo) => {
    try {
      const entry = await api.useSharedTemplate(row.id);
      // the template hands over defaults only: the consumer still goes through the wizard
      navigate(`/v2/agents?${new URLSearchParams({ view: "new", template: entry.id })}`);
    } catch (error) {
      toast("act", errorMessage(error));
    }
  };
  const withdraw = async () => {
    if (!withdrawing) return;
    setBusy(true);
    try {
      await api.unpublishTemplate(withdrawing.id);
      toast("ok", t("v2.fleet.withdrawn"));
      setTick((n) => n + 1);
    } catch (error) {
      toast("act", errorMessage(error));
    } finally {
      setBusy(false);
      setWithdrawing(null);
    }
  };

  return (
    <div style={{ display: "grid", gap: 16 }}>
      <Panel title={t("v3.fleet.marketTitle")} end={<span style={{ color: "var(--v3-text-3)" }}>{t("v2.fleet.marketSub")}</span>}>
        {list.loading && !list.data ? (
          <Skeleton rows={3} />
        ) : list.error ? (
          <Notice s="act">{list.error}</Notice>
        ) : rows.length === 0 ? (
          <Empty title={t("v2.fleet.marketEmpty")} />
        ) : (
          <div className="v3-flt-market">
            {rows.map((row) => (
              <div key={row.id} className="v3-flt-tpl" data-testid={`v3-template-${row.id}`}>
                <div className="top">
                  <b>{row.title}</b>
                  {row.own && <Chip s="info">{t("v2.fleet.ours")}</Chip>}
                  <Chip>{t(`v2.agents.wizard.method.${row.method}`, { defaultValue: row.method })}</Chip>
                </div>
                <p>{row.summary}</p>
                <div className="meta">
                  {t("v2.fleet.publishedBy", { who: row.published_by ?? "—", where: row.source_workspace_id, when: ago(row.updated_at) })}
                  {" · "}
                  {t("v2.fleet.uses", { count: row.uses })}
                </div>
                {row.requirements.length > 0 && (
                  <Notice s="wait">{t("v2.fleet.needs", { items: row.requirements.map((need) => need.label).join(", ") })}</Notice>
                )}
                <div className="actions">
                  <Btn size="sm" kind="primary" onClick={() => void take(row)}>{t("v2.fleet.use")}</Btn>
                  {row.own && (
                    <Btn size="sm" kind="ghost" onClick={() => setWithdrawing(row)} title={t("v3.fleet.withdraw")}>
                      <Trash2 size={13} /> {t("v3.fleet.withdraw")}
                    </Btn>
                  )}
                </div>
              </div>
            ))}
          </div>
        )}
      </Panel>
      {withdrawing && (
        <Confirm
          title={t("v3.fleet.withdrawTitle")}
          confirmLabel={t("v3.fleet.withdraw")}
          cancelLabel={t("v3.common.cancel")}
          danger
          busy={busy}
          onCancel={() => setWithdrawing(null)}
          onConfirm={() => void withdraw()}
        >
          {t("v3.fleet.withdrawBody", { title: withdrawing.title })}
        </Confirm>
      )}
    </div>
  );
}

/**
 * The cross-environment view, at three altitudes as in V2 (`?view=`): every
 * environment (where is anything wrong), governance health of the selected
 * workspace (what, and the link that fixes it), and the shared templates.
 */
export function V3Fleet() {
  const { t } = useTranslation();
  const [params, setParams] = useSearchParams();
  const raw = params.get("view");
  const view: View = (VIEWS as string[]).includes(raw ?? "") ? (raw as View) : "fleet";
  return (
    <div className="v3-reveal" style={{ display: "grid", gap: 16 }}>
      <PageHead
        eyebrow={t("v3.fleet.eyebrow")}
        title={t("v3.fleet.title")}
        sub={t("v3.fleet.sub")}
        end={
          <Filters
            value={view}
            onChange={(next) => setParams(next === "fleet" ? {} : { view: next })}
            options={VIEWS.map((v) => ({ value: v, label: t(`v2.fleet.tab.${v}`) }))}
          />
        }
      />
      {view === "fleet" && <FleetView />}
      {view === "health" && <HealthView />}
      {view === "market" && <MarketView />}
    </div>
  );
}
