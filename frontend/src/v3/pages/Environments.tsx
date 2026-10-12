import "./environments.css";

import { Radar, Search } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link, useSearchParams } from "react-router-dom";

import {
  api,
  type DriftAgent,
  type DriftReport,
  type DriftState,
  type EnvironmentComparison,
  type EnvironmentRow,
  environmentApi,
  errorMessage,
} from "../../lib/api";
import { useWorkspace } from "../../workspace/workspace-context";
import { ago } from "../format";
import { useLoad, useToast } from "../hooks";
import { Btn, Chip, Empty, Filters, Lamp, Notice, PageHead, Panel, type Signal, Skeleton, Stat } from "../ui";

/** How one environment's copy compares with the reference (the highest tier running it). */
const VS_SIGNAL: Record<EnvironmentRow["vs_reference"], Signal> = {
  reference: "info",
  same: "ok",
  differs: "wait",
  absent: "off",
};

/** Ledger vs AWS. "Unknown" is never folded into in sync: green means AWS was read and agreed. */
const DRIFT_SIGNAL: Record<DriftState, Signal> = { in_sync: "ok", drift: "act", unknown: "wait" };

const TIER_ORDER: Record<string, number> = { dev: 0, staging: 1, prod: 2 };
const TIER_SIGNAL: Record<string, Signal | undefined> = { prod: "act", staging: "wait" };
// compare reads the ledger only; a few at a time keeps a large fleet from flooding the hub
const PARALLEL = 4;
const MAX_ROWS = 40;

type Compared = Record<string, EnvironmentComparison | { error: string }>;

function isError(v: EnvironmentComparison | { error: string } | undefined): v is { error: string } {
  return !!v && "error" in v;
}

function findingText(t: (k: string, o?: Record<string, unknown>) => string, a: DriftAgent): string {
  if (a.findings.length) {
    return a.findings.map((f) => t(`v2.environments.finding.${f.code}`, { expected: f.expected, observed: f.observed })).join("; ");
  }
  return a.reason ? t(`v2.environments.reason.${a.reason}`) : "";
}

/**
 * Environments — every agent of this workspace across every environment the caller
 * can see, as one matrix: is the same spec running everywhere, and does AWS still
 * agree with what Launchpad recorded here. The comparison reads the ledger and loads
 * on open; drift reads AWS, so it runs only when asked.
 */
export function V3Environments() {
  const { t } = useTranslation();
  const toast = useToast();
  const { current } = useWorkspace();
  const [params, setParams] = useSearchParams();
  const ws = current?.id ?? "";
  const agents = useLoad(() => api.listAgents(), `v3-env-agents:${ws}`);
  const [compared, setCompared] = useState<Compared>({});
  const [drift, setDrift] = useState<DriftReport | null>(null);
  const [checking, setChecking] = useState(false);
  const [filter, setFilter] = useState<"all" | "differs" | "aligned" | "drift">("all");
  const [lookup, setLookup] = useState(params.get("agent") ?? "");
  const [extra, setExtra] = useState<EnvironmentComparison | null>(null);
  const [looking, setLooking] = useState(false);

  const names = useMemo(
    () =>
      (agents.data?.agents ?? [])
        .filter((a) => a.status !== "deleted")
        .map((a) => a.name)
        .sort()
        .slice(0, MAX_ROWS),
    [agents.data],
  );
  const total = (agents.data?.agents ?? []).filter((a) => a.status !== "deleted").length;

  // a workspace switch is a new matrix and a new drift question
  useEffect(() => {
    setCompared({});
    setDrift(null);
  }, [ws]);

  useEffect(() => {
    let cancelled = false;
    const queue = names.filter((n) => !(n in compared));
    if (!queue.length) return;
    const worker = async () => {
      while (queue.length && !cancelled) {
        const name = queue.shift()!;
        let value: EnvironmentComparison | { error: string };
        try {
          value = await environmentApi.compare(name);
        } catch (err) {
          value = { error: errorMessage(err) };
        }
        if (!cancelled) setCompared((prev) => ({ ...prev, [name]: value }));
      }
    };
    void Promise.all(Array.from({ length: Math.min(PARALLEL, queue.length) }, worker));
    return () => {
      cancelled = true;
    };
    // `compared` is read only to skip what is already loaded
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [names]);

  // columns: every environment any comparison reported, lowest tier first, current marked
  const columns = useMemo(() => {
    const seen = new Map<string, EnvironmentRow["workspace"] & { current: boolean }>();
    for (const v of Object.values(compared)) {
      if (isError(v)) continue;
      for (const row of v.environments) {
        if (!seen.has(row.workspace.id)) seen.set(row.workspace.id, { ...row.workspace, current: row.current });
      }
    }
    return [...seen.values()].sort(
      (a, b) => (TIER_ORDER[a.tier] ?? 9) - (TIER_ORDER[b.tier] ?? 9) || a.name.localeCompare(b.name),
    );
  }, [compared]);

  const driftBy = useMemo(() => new Map((drift?.agents ?? []).map((a) => [a.name, a])), [drift]);
  const loaded = names.filter((n) => n in compared).length;
  const aligned = names.filter((n) => {
    const v = compared[n];
    return v && !isError(v) && v.summary.aligned;
  });
  const differs = names.filter((n) => {
    const v = compared[n];
    return v && !isError(v) && !v.summary.aligned;
  });
  const drifted = (drift?.agents ?? []).filter((a) => a.state === "drift");
  const rows = names.filter((n) => {
    if (filter === "differs") return differs.includes(n);
    if (filter === "aligned") return aligned.includes(n);
    if (filter === "drift") return driftBy.get(n)?.state === "drift" || driftBy.get(n)?.state === "unknown";
    return true;
  });
  const selected = params.get("agent");
  const selectedCmp = selected ? (compared[selected] && !isError(compared[selected]) ? (compared[selected] as EnvironmentComparison) : extra?.agent === selected ? extra : null) : null;

  const check = async () => {
    setChecking(true);
    try {
      setDrift(await environmentApi.drift());
    } catch (err) {
      toast("act", errorMessage(err));
    } finally {
      setChecking(false);
    }
  };
  const select = (name: string | null) => {
    const next = new URLSearchParams(params);
    if (name) next.set("agent", name);
    else next.delete("agent");
    setParams(next, { replace: true });
  };
  // an agent this workspace does not run can still be looked up across environments
  const look = async () => {
    const name = lookup.trim();
    if (!name) return;
    if (names.includes(name)) return select(name);
    setLooking(true);
    try {
      setExtra(await environmentApi.compare(name));
      select(name);
    } catch (err) {
      toast("act", errorMessage(err));
    } finally {
      setLooking(false);
    }
  };
  useEffect(() => {
    // a deep link (`?agent=`) to an agent outside this workspace
    if (selected && !names.includes(selected) && agents.data && extra?.agent !== selected) {
      environmentApi.compare(selected).then(setExtra, () => undefined);
    }
  }, [selected, names, agents.data, extra?.agent]);

  return (
    <div className="v3-reveal" style={{ display: "grid", gap: 16 }}>
      <PageHead
        eyebrow={t("v3.environments.eyebrow", { ws: current?.name ?? "" })}
        title={t("v3.environments.title")}
        sub={t("v3.environments.sub")}
        end={
          <Btn kind="primary" disabled={checking || !ws} onClick={() => void check()}>
            <Radar size={14} /> {checking ? t("v2.environments.checking") : t("v2.environments.check")}
          </Btn>
        }
      />

      <div className="v3-grid c4">
        <Panel>
          <Stat label={t("v3.environments.agents")} value={agents.data ? total : "—"}
            foot={agents.data && loaded < names.length ? t("v3.environments.comparing", { done: loaded, total: names.length }) : t("v3.environments.envCount", { count: columns.length })} />
        </Panel>
        <Panel signal={aligned.length ? "ok" : undefined}>
          <Stat label={t("v3.environments.aligned")} value={agents.data ? aligned.length : "—"} foot={t("v3.environments.alignedFoot")} />
        </Panel>
        <Panel signal={differs.length ? "wait" : undefined}>
          <Stat label={t("v3.environments.differs")} value={agents.data ? differs.length : "—"} signal={differs.length ? "wait" : undefined} foot={t("v3.environments.differsFoot")} />
        </Panel>
        <Panel signal={drift ? (drifted.length ? "act" : drift.counts.unknown ? "wait" : "ok") : undefined}>
          <Stat
            label={t("v3.environments.drift")}
            value={drift ? drifted.length : "—"}
            signal={drift && drifted.length ? "act" : undefined}
            foot={drift ? t("v3.environments.driftFoot", { checked: drift.checked, unknown: drift.counts.unknown }) : t("v2.environments.notChecked")}
          />
        </Panel>
      </div>

      {agents.error && <Notice s="act">{agents.error}</Notice>}
      {total > MAX_ROWS && <Notice s="wait">{t("v3.environments.capped", { max: MAX_ROWS, total })}</Notice>}
      {drift?.truncated && <Notice s="wait">{t("v2.environments.truncated")}</Notice>}
      {drift && drift.counts.unknown > 0 && <Notice>{t("v2.environments.unknownNote")}</Notice>}

      <div style={{ display: "flex", gap: 12, alignItems: "center", flexWrap: "wrap" }}>
        <Filters
          value={filter}
          onChange={setFilter}
          options={[
            { value: "all", label: t("v3.environments.all"), count: names.length },
            { value: "differs", label: t("v3.environments.differs"), s: "wait", count: differs.length },
            { value: "aligned", label: t("v3.environments.aligned"), s: "ok", count: aligned.length },
            ...(drift ? [{ value: "drift" as const, label: t("v3.environments.driftOrUnknown"), s: "act" as Signal, count: drift.counts.drift + drift.counts.unknown }] : []),
          ]}
        />
        <form
          onSubmit={(e) => {
            e.preventDefault();
            void look();
          }}
          style={{ marginLeft: "auto", display: "flex", gap: 8, flex: "1 1 260px", maxWidth: 380 }}
        >
          <div style={{ position: "relative", flex: 1 }}>
            <Search size={14} style={{ position: "absolute", left: 12, top: 11, color: "var(--v3-text-3)" }} aria-hidden="true" />
            <input className="v3-input" style={{ paddingLeft: 34 }} value={lookup} maxLength={64} onChange={(e) => setLookup(e.target.value)}
              placeholder={t("v2.environments.agentPlaceholder")} aria-label={t("v2.environments.agentLabel")} />
          </div>
          <Btn type="submit" disabled={looking || !lookup.trim()}>{t("v2.environments.compare")}</Btn>
        </form>
      </div>

      <Panel flush title={t("v3.environments.matrix")} end={drift ? <span>{t("v3.environments.checkedAt", { ws: current?.name ?? "" })}</span> : undefined}>
        {agents.loading && !agents.data ? (
          <div style={{ padding: 20 }}><Skeleton rows={5} /></div>
        ) : names.length === 0 ? (
          <Empty title={t("v3.environments.noAgents")} />
        ) : rows.length === 0 ? (
          <Empty title={t("v3.environments.none")} />
        ) : (
          <div style={{ overflowX: "auto" }}>
            <table className="v3-table v3-env-matrix">
              <thead>
                <tr>
                  <th>{t("v2.environments.colAgent")}</th>
                  {columns.map((c) => (
                    <th key={c.id} className={c.current ? "cur" : undefined}>
                      <span className="env">
                        <span>{c.name}</span>
                        <Chip s={TIER_SIGNAL[c.tier]}>{t(`v2.workspaces.tier.${c.tier}`, { defaultValue: c.tier })}</Chip>
                      </span>
                      <small>{c.region}{c.current ? ` · ${t("v2.environments.current")}` : ""}</small>
                    </th>
                  ))}
                  <th className="drift">{t("v3.environments.awsHere")}</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((name) => {
                  const v = compared[name];
                  const byWs = new Map(!v || isError(v) ? [] : v.environments.map((r) => [r.workspace.id, r]));
                  const d = driftBy.get(name);
                  return (
                    <tr key={name} className={selected === name ? "click on" : "click"} onClick={() => select(selected === name ? null : name)}>
                      <td>
                        <div className="v3-name">
                          <div>
                            <b>{name}</b>
                            <small>
                              {!v ? t("v3.environments.loadingRow") : isError(v) ? v.error.slice(0, 80) : v.summary.aligned ? t("v2.environments.aligned") : t("v3.environments.specs", { count: v.summary.distinct_spec_digests })}
                            </small>
                          </div>
                        </div>
                      </td>
                      {columns.map((c) => {
                        const row = byWs.get(c.id);
                        if (!v) return <td key={c.id} className="cell"><span className="v3-skel" style={{ width: 56, height: 10, display: "inline-block" }} /></td>;
                        if (!row || !row.agent) return <td key={c.id} className="cell off"><Lamp s="off" /> <span>—</span></td>;
                        const s = VS_SIGNAL[row.vs_reference];
                        return (
                          <td key={c.id} className={`cell ${c.current ? "cur" : ""}`} title={t(`v2.environments.vs.${row.vs_reference}`)}>
                            <span className="v">
                              <Lamp s={s} live={row.agent.status === "deploying"} />
                              <span className="mono">{row.agent.version ? `v${row.agent.version}` : "—"}</span>
                            </span>
                            <small>{t(`v2.environments.vs.${row.vs_reference}`)}</small>
                          </td>
                        );
                      })}
                      <td className="cell drift">
                        {!drift ? (
                          <span style={{ color: "var(--v3-text-3)" }}>—</span>
                        ) : d ? (
                          <span className="v" title={findingText(t, d)}>
                            <Lamp s={DRIFT_SIGNAL[d.state]} />
                            <span>{t(`v2.environments.state.${d.state}`)}</span>
                          </span>
                        ) : (
                          <span style={{ color: "var(--v3-text-3)" }}>{t("v3.environments.notChecked")}</span>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </Panel>

      {selected && (
        <Panel
          title={t("v2.environments.compareTitle", { agent: selected })}
          signal={selectedCmp ? (selectedCmp.summary.aligned ? "ok" : "wait") : undefined}
          flush
          end={<button type="button" className="v3-btn ghost sm" onClick={() => select(null)}>{t("v3.environments.close")}</button>}
        >
          {!selectedCmp ? (
            <div style={{ padding: 20 }}><Skeleton rows={3} /></div>
          ) : (
            <>
              <div style={{ padding: "0 20px 12px", color: "var(--v3-text-3)", fontSize: 13 }}>
                {selectedCmp.summary.present === 0
                  ? t("v2.environments.empty")
                  : selectedCmp.summary.aligned
                    ? t("v2.environments.aligned")
                    : t("v2.environments.notAligned")}{" "}
                {t("v2.environments.compareSub")}
              </div>
              <table className="v3-table">
                <thead>
                  <tr>
                    <th />
                    <th>{t("v2.environments.colEnv")}</th>
                    <th>{t("v2.environments.colStatus")}</th>
                    <th>{t("v2.environments.colVersion")}</th>
                    <th>{t("v2.environments.colSpec")}</th>
                    <th>{t("v2.environments.colDeploy")}</th>
                    <th>{t("v2.environments.colVs")}</th>
                  </tr>
                </thead>
                <tbody>
                  {selectedCmp.environments.map((row) => (
                    <tr key={row.workspace.id}>
                      <td style={{ width: 30 }}><Lamp s={row.agent ? VS_SIGNAL[row.vs_reference] : "off"} /></td>
                      <td>
                        <div className="v3-name">
                          <div>
                            <b>{row.workspace.name}{row.current ? ` · ${t("v2.environments.current")}` : ""}</b>
                            <small className="mono">{row.workspace.id} · {row.workspace.region}</small>
                          </div>
                        </div>
                      </td>
                      <td style={{ color: "var(--v3-text-2)" }}>
                        {row.agent ? (
                          row.agent.id && row.current ? <Link to={`/v3/agents?id=${encodeURIComponent(row.agent.id)}`} style={{ textDecoration: "underline" }}>{row.agent.status}</Link> : row.agent.status
                        ) : "—"}
                      </td>
                      <td className="mono">{row.agent?.version ? `v${row.agent.version}` : "—"}</td>
                      <td className="mono" style={{ color: "var(--v3-text-2)" }}>{row.agent ? row.agent.spec_digest.slice(0, 12) : "—"}</td>
                      <td style={{ color: "var(--v3-text-3)" }}>
                        {row.agent?.last_deploy ? `${row.agent.last_deploy.status} · ${ago(row.agent.last_deploy.started_at)}` : t("v2.environments.notDeployed")}
                      </td>
                      <td><Chip s={VS_SIGNAL[row.vs_reference] === "off" ? undefined : VS_SIGNAL[row.vs_reference]}>{t(`v2.environments.vs.${row.vs_reference}`)}</Chip></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </>
          )}
        </Panel>
      )}

      {drift && (drift.counts.drift > 0 || drift.counts.unknown > 0) && (
        <Panel title={t("v3.environments.findings")} signal={drift.counts.drift ? "act" : "wait"} flush>
          <table className="v3-table">
            <tbody>
              {drift.agents
                .filter((a) => a.state !== "in_sync")
                .map((a) => (
                  <tr key={a.agent_id}>
                    <td style={{ width: 30 }}><Lamp s={DRIFT_SIGNAL[a.state]} /></td>
                    <td><b>{a.name}</b></td>
                    <td><Chip s={DRIFT_SIGNAL[a.state]}>{t(`v2.environments.state.${a.state}`)}</Chip></td>
                    <td style={{ color: "var(--v3-text-2)" }}>{findingText(t, a) || "—"}</td>
                  </tr>
                ))}
            </tbody>
          </table>
        </Panel>
      )}
    </div>
  );
}
