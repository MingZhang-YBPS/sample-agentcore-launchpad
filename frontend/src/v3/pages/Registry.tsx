import { ArrowLeft, Copy, Eye, EyeOff, Plus, RefreshCw, Search } from "lucide-react";
import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link, useNavigate, useSearchParams } from "react-router-dom";

import { useAuth } from "../../auth/auth-context";
import { api, errorMessage, type RegistryRecordOut } from "../../lib/api";
import { descriptorExcerpt, parseAgentCard, parseMcpUrl, parseSkillDefinition, skillPath } from "../../lib/registry";
import { isRegistryUnavailable } from "../../v2/pages/registry/common";
import { useWorkspace } from "../../workspace/workspace-context";
import { ago } from "../format";
import { useLoad, useToast } from "../hooks";
import { registrySignal } from "../signals";
import {
  Btn,
  Chip,
  Confirm,
  Empty,
  Filters,
  Lamp,
  Notice,
  PageHead,
  Panel,
  type Signal,
  Skeleton,
  Stat,
  Track,
  type TrackNode,
} from "../ui";
import { Term } from "../onboarding/Glossary";

type RecordType = RegistryRecordOut["type"];
type Lifecycle = "submit" | "approve" | "reject" | "disable";

function useStatusLabel() {
  const { t } = useTranslation();
  return (status: string) => t(`v2.registry.status.${status}`, { defaultValue: status });
}

function useTypeLabel() {
  const { t } = useTranslation();
  return (type: string) => t(`v2.registry.type.${type}`, { defaultValue: type });
}

/** Lifecycle actions, run from the list's approval queue and from a record. */
function useLifecycle(onDone: () => void) {
  const { t } = useTranslation();
  const toast = useToast();
  const statusLabel = useStatusLabel();
  const [busy, setBusy] = useState<string | null>(null);
  const run = async (record: RegistryRecordOut, action: Lifecycle) => {
    setBusy(record.record_id);
    try {
      const updated = await api.registryAction(record.record_id, action);
      toast("ok", t("v2.registry.actionDone", { name: record.name, status: statusLabel(updated.status) }));
      onDone();
    } catch (err) {
      toast("act", t("common.actionFailed", { msg: errorMessage(err) }));
    } finally {
      setBusy(null);
    }
  };
  return { busy, run };
}

/* ── list ────────────────────────────────────────────────────────────────── */

function RecordList() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const { isAdmin } = useAuth();
  const { current } = useWorkspace();
  const statusLabel = useStatusLabel();
  const typeLabel = useTypeLabel();
  const [tick, setTick] = useState(0);
  // 503 registry.unavailable: this account has no Registry — a page state, not an error
  const records = useLoad(
    () =>
      api.registryRecords().then(
        (body) => ({ ...body, unavailable: false }),
        (err: unknown) => {
          if (isRegistryUnavailable(err)) return { records: [] as RegistryRecordOut[], unavailable: true };
          throw err;
        },
      ),
    `v3-registry:${current?.id ?? ""}:${tick}`,
  );
  // which records a consumer can find: null when the consumer view is unreadable
  const discoverable = useLoad(
    () => api.registryDiscoverable().then((r) => new Set(r.records.map((x) => x.record_id))).catch(() => null),
    `v3-registry-disc:${current?.id ?? ""}:${tick}`,
  );
  const lifecycle = useLifecycle(() => setTick((n) => n + 1));
  const [type, setType] = useState<"all" | RecordType>("all");
  const [state, setState] = useState<"all" | Signal>("all");
  const [q, setQ] = useState("");

  const all = useMemo(() => records.data?.records ?? [], [records.data]);
  const rows = useMemo(() => {
    const needle = q.trim().toLowerCase();
    return all
      .filter((r) => type === "all" || r.type === type)
      .filter((r) => state === "all" || registrySignal(r.status) === state)
      .filter((r) => !needle || `${r.name} ${r.record_id} ${r.description}`.toLowerCase().includes(needle))
      .sort((a, b) => (b.updated_at ?? "").localeCompare(a.updated_at ?? ""));
  }, [all, type, state, q]);
  // a member cannot move a system record; the server enforces it, the queue just hides it
  const pending = all.filter((r) => r.status === "PENDING_APPROVAL" && (!r.system || isAdmin));
  const countType = (k: RecordType) => all.filter((r) => r.type === k).length;
  const countState = (s: Signal) => all.filter((r) => registrySignal(r.status) === s).length;

  if (records.data?.unavailable) {
    return <Notice s="wait">{t("v3.registry.unavailable")}</Notice>;
  }

  return (
    <div className="v3-reveal" style={{ display: "grid", gap: 16 }}>
      <PageHead
        eyebrow={t("v3.registry.eyebrow")}
        title={t("v3.registry.title")}
        sub={t("v3.registry.sub")}
        end={
          <>
            <Link to="/v2/registry?view=discoverable" className="v3-btn ghost">{t("v3.registry.consumer")}</Link>
            <Link to="/v2/registry?view=a2a-demo" className="v3-btn ghost">{t("v3.registry.a2aDemo")}</Link>
            <Link to="/v2/governance" className="v3-btn" title={t("v2.registry.importGatewayHint")}>{t("v3.registry.importGateway")}</Link>
            <Link to="/v2/registry?view=register" className="v3-btn primary"><Plus size={14} /> {t("v3.registry.register")}</Link>
          </>
        }
      />

      <div className="v3-grid c4">
        <Panel><Stat label={<Term term="a2a">{typeLabel("A2A")}</Term>} value={countType("A2A")} foot={t("v3.registry.footA2A")} /></Panel>
        <Panel><Stat label={<Term term="mcp">{typeLabel("MCP")}</Term>} value={countType("MCP")} foot={t("v3.registry.footMcp")} /></Panel>
        <Panel><Stat label={<Term term="skill">{typeLabel("AGENT_SKILLS")}</Term>} value={countType("AGENT_SKILLS")} foot={t("v3.registry.footSkills")} /></Panel>
        <Panel signal={pending.length ? "wait" : undefined}>
          <Stat label={t("v3.registry.pending")} value={pending.length} signal={pending.length ? "wait" : undefined}
            foot={discoverable.data ? t("v3.registry.hiddenFoot", { count: all.filter((r) => !discoverable.data!.has(r.record_id)).length }) : undefined} />
        </Panel>
      </div>

      {pending.length > 0 && (
        <Panel title={t("v3.registry.queue")} signal="wait" flush end={<span className="mono">{pending.length}</span>}>
          <table className="v3-table">
            <tbody>
              {pending.map((r) => (
                <tr key={r.record_id}>
                  <td style={{ width: 30 }}><Lamp s="wait" live /></td>
                  <td>
                    <div className="v3-name"><div><b>{r.name}</b><small>{(r.description || r.record_id).slice(0, 90)}</small></div></div>
                  </td>
                  <td><Chip>{typeLabel(r.type)}</Chip></td>
                  <td className="mono" style={{ color: "var(--v3-text-2)", whiteSpace: "nowrap" }}>{r.version ?? "—"}</td>
                  <td className="num" style={{ color: "var(--v3-text-3)" }}>{ago(r.updated_at)}</td>
                  <td style={{ width: 1, whiteSpace: "nowrap" }}>
                    <div style={{ display: "flex", gap: 6, justifyContent: "flex-end" }}>
                      <Btn size="sm" kind="primary" disabled={lifecycle.busy === r.record_id} onClick={() => void lifecycle.run(r, "approve")}>
                        {t("v2.registry.action.approve")}
                      </Btn>
                      <Btn size="sm" disabled={lifecycle.busy === r.record_id} onClick={() => void lifecycle.run(r, "reject")}>
                        {t("v2.registry.action.reject")}
                      </Btn>
                      <Btn size="sm" kind="ghost" onClick={() => navigate(`/v3/registry?id=${encodeURIComponent(r.record_id)}`)}>
                        {t("v3.registry.open")}
                      </Btn>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </Panel>
      )}

      <div style={{ display: "flex", gap: 12, alignItems: "center", flexWrap: "wrap" }}>
        <Filters
          value={type}
          onChange={setType}
          options={[
            { value: "all", label: t("v3.registry.allTypes"), count: all.length },
            ...(["A2A", "MCP", "AGENT_SKILLS"] as const).map((k) => ({ value: k, label: typeLabel(k), count: countType(k) })),
          ]}
        />
        <span style={{ width: 1, height: 20, background: "var(--v3-line)" }} aria-hidden="true" />
        <Filters
          value={state}
          onChange={setState}
          options={[
            { value: "all", label: t("v3.registry.allStates") },
            { value: "ok", label: statusLabel("APPROVED"), s: "ok", count: countState("ok") },
            { value: "wait", label: statusLabel("PENDING_APPROVAL"), s: "wait", count: countState("wait") },
            { value: "act", label: statusLabel("REJECTED"), s: "act", count: countState("act") },
            { value: "off", label: t("v3.registry.draftOrRetired"), s: "off", count: countState("off") },
          ]}
        />
        <div style={{ marginLeft: "auto", position: "relative", flex: "1 1 200px", maxWidth: 300 }}>
          <Search size={14} style={{ position: "absolute", left: 12, top: 11, color: "var(--v3-text-3)" }} aria-hidden="true" />
          <input className="v3-input" style={{ paddingLeft: 34 }} value={q} onChange={(e) => setQ(e.target.value)}
            placeholder={t("v3.registry.search")} aria-label={t("v3.registry.search")} />
        </div>
      </div>

      <Panel flush>
        {records.loading && !records.data ? (
          <div style={{ padding: 20 }}><Skeleton rows={6} /></div>
        ) : records.error ? (
          <div style={{ padding: 20 }}><Notice s="act">{records.error}</Notice></div>
        ) : rows.length === 0 ? (
          <Empty title={all.length ? t("v3.registry.none") : t("v3.registry.empty")} />
        ) : (
          <table className="v3-table">
            <thead>
              <tr>
                <th />
                <th>{t("v3.registry.name")}</th>
                <th>{t("v3.registry.type")}</th>
                <th>{t("v3.registry.version")}</th>
                <th>{t("v3.registry.status")}</th>
                <th>{t("v3.registry.consumers")}</th>
                <th className="num">{t("v3.registry.updated")}</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => {
                const s = registrySignal(r.status);
                const hidden = discoverable.data ? !discoverable.data.has(r.record_id) : null;
                return (
                  <tr key={r.record_id} className="click" onClick={() => navigate(`/v3/registry?id=${encodeURIComponent(r.record_id)}`)}>
                    <td style={{ width: 30 }}><Lamp s={s} live={s === "ok"} /></td>
                    <td>
                      <div className="v3-name">
                        <div>
                          <b>{r.name}</b>
                          <small>{r.description ? r.description.slice(0, 90) : r.record_id}</small>
                        </div>
                      </div>
                    </td>
                    <td><Chip>{typeLabel(r.type)}</Chip>{r.system && <> <Chip s="info">{t("v2.registry.system")}</Chip></>}</td>
                    <td className="mono" style={{ color: "var(--v3-text-2)", whiteSpace: "nowrap" }}>{r.version ?? "—"}</td>
                    <td><Chip s={s === "off" ? undefined : s}>{statusLabel(r.status)}</Chip></td>
                    <td style={{ color: hidden ? "var(--v3-text-3)" : "var(--v3-text-2)" }}>
                      {hidden == null ? "—" : hidden ? (
                        <span style={{ display: "inline-flex", gap: 6, alignItems: "center" }}><EyeOff size={14} /> {t("v3.registry.hidden")}</span>
                      ) : (
                        <span style={{ display: "inline-flex", gap: 6, alignItems: "center" }}><Eye size={14} /> {t("v3.registry.visible")}</span>
                      )}
                    </td>
                    <td className="num" style={{ color: "var(--v3-text-3)" }}>{ago(r.updated_at)}</td>
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

/* ── one record ──────────────────────────────────────────────────────────── */

function lifecycleNodes(status: string, statusLabel: (s: string) => string): TrackNode[] {
  const steps = ["DRAFT", "PENDING_APPROVAL", "APPROVED"];
  // where the record is on the main line; rejected and retired leave it after review
  const at = status === "REJECTED" ? 2 : status === "DEPRECATED" ? 3 : steps.indexOf(status);
  const nodes: TrackNode[] = steps.map((step, i) => ({
    key: step,
    label: statusLabel(step),
    s: i < at ? "ok" : i === at ? (step === "DRAFT" ? "info" : registrySignal(step)) : "off",
    here: i === at,
  }));
  if (status === "REJECTED") nodes[2] = { key: "REJECTED", label: statusLabel("REJECTED"), s: "act", here: true };
  if (status === "DEPRECATED") nodes.push({ key: "DEPRECATED", label: statusLabel("DEPRECATED"), s: "off", here: true });
  return nodes;
}

function RecordDetail({ id }: { id: string }) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const toast = useToast();
  const { isAdmin } = useAuth();
  const statusLabel = useStatusLabel();
  const typeLabel = useTypeLabel();
  const [tick, setTick] = useState(0);
  const detail = useLoad(() => api.registryRecord(id), `v3-registry-rec:${id}:${tick}`);
  const discoverable = useLoad(
    () => api.registryDiscoverable().then((r) => new Set(r.records.map((x) => x.record_id))).catch(() => null),
    `v3-registry-disc:${tick}`,
  );
  const lifecycle = useLifecycle(() => setTick((n) => n + 1));
  const [confirm, setConfirm] = useState<"disable" | "delete" | null>(null);
  const [busy, setBusy] = useState(false);
  const r = detail.data;

  if (detail.loading && !r) return <Skeleton rows={6} />;
  if (!r) return <Notice s="act">{detail.error ?? t("v3.registry.notFound")}</Notice>;

  const s = registrySignal(r.status);
  const lifecycleAllowed = !r.system || isAdmin;
  const approved = r.status === "APPROVED";
  const card = parseAgentCard(r);
  const skill = parseSkillDefinition(r);
  const mcpUrl = r.type === "MCP" ? parseMcpUrl(r) : "";
  const hidden = discoverable.data ? !discoverable.data.has(r.record_id) : null;
  const canEdit = r.type !== "A2A" && r.status !== "DEPRECATED" && !r.system;
  const canReimport = r.type === "AGENT_SKILLS" && !r.system && r.status !== "DEPRECATED"
    && (skill?.source?.kind === "git" || skill?.source?.kind === "url");
  const busyHere = lifecycle.busy === r.record_id || busy;

  const useInAgent = () => {
    if (r.type === "MCP") navigate(`/v2/agents?view=new&gateway=${encodeURIComponent(r.name)}`);
    else navigate(`/v2/agents?view=new&skill=${encodeURIComponent(skillPath(r))}`);
  };
  const remove = async () => {
    setBusy(true);
    try {
      await api.registryDelete(r.record_id);
      toast("ok", t("registry.deleted", { name: r.name }));
      navigate("/v3/registry");
    } catch (err) {
      toast("act", t("common.actionFailed", { msg: errorMessage(err) }));
      setBusy(false);
      setConfirm(null);
    }
  };
  const reimport = async () => {
    setBusy(true);
    try {
      const updated = await api.registryReimport(r.record_id);
      toast("ok", t("registry.drawer.reimportOk", { name: updated.name }));
      setTick((n) => n + 1);
    } catch (err) {
      toast("act", t("registry.drawer.reimportFailed", { msg: errorMessage(err) }));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="v3-reveal" style={{ display: "grid", gap: 16 }}>
      <div>
        <button type="button" className="v3-btn ghost sm" onClick={() => navigate("/v3/registry")}>
          <ArrowLeft size={14} /> {t("v3.registry.title")}
        </button>
      </div>
      <PageHead
        eyebrow={`${typeLabel(r.type)} · ${r.record_id}`}
        title={
          <span style={{ display: "inline-flex", alignItems: "center", gap: 14 }}>
            <Lamp s={s} live={s === "ok"} />
            {r.name}
          </span>
        }
        sub={r.description || undefined}
        end={
          <>
            {r.type !== "A2A" && (
              <Btn kind="primary" disabled={!approved} title={approved ? undefined : t("registry.drawer.useNeedsApproved")} onClick={useInAgent}>
                {t("v2.registry.useInAgent")}
              </Btn>
            )}
            {canEdit && <Link to={`/v2/registry?view=edit&id=${encodeURIComponent(r.record_id)}`} className="v3-btn">{t("v3.registry.edit")}</Link>}
            <Link to={`/v2/registry?view=detail&id=${encodeURIComponent(r.record_id)}&full=1`} className="v3-btn ghost">{t("v3.registry.full")}</Link>
            {!r.system && <Btn kind="danger" disabled={busyHere} onClick={() => setConfirm("delete")}>{t("v3.registry.delete")}</Btn>}
          </>
        }
      />

      {r.status_reason && <Notice s={r.status === "REJECTED" ? "act" : "info"}>{r.status_reason}</Notice>}
      {r.system && (
        <Notice>
          {t(isAdmin ? "registry.drawer.system.noteAdmin" : "registry.drawer.system.noteMember")}{" "}
          <span className="mono">{r.system.label}{r.system.skill_version ? ` · ${r.system.skill_version}` : ""}</span>
        </Notice>
      )}

      <Panel
        title={t("v3.registry.lifecycle")}
        signal={s === "off" ? undefined : s}
        end={
          lifecycleAllowed ? (
            <span style={{ display: "inline-flex", gap: 8 }}>
              {r.status === "DRAFT" && <Btn size="sm" kind="primary" disabled={busyHere} onClick={() => void lifecycle.run(r, "submit")}>{t("v2.registry.action.submit")}</Btn>}
              {(r.status === "PENDING_APPROVAL" || r.status === "REJECTED") && (
                <Btn size="sm" kind="primary" disabled={busyHere} onClick={() => void lifecycle.run(r, "approve")}>{t("v2.registry.action.approve")}</Btn>
              )}
              {r.status === "PENDING_APPROVAL" && <Btn size="sm" disabled={busyHere} onClick={() => void lifecycle.run(r, "reject")}>{t("v2.registry.action.reject")}</Btn>}
              {approved && <Btn size="sm" disabled={busyHere} onClick={() => setConfirm("disable")}>{t("v2.registry.action.disable")}</Btn>}
            </span>
          ) : (
            <span style={{ color: "var(--v3-text-3)" }}>{t("registry.drawer.system.memberReadOnly")}</span>
          )
        }
      >
        <Track nodes={lifecycleNodes(r.status, statusLabel)} />
        <p style={{ margin: "14px 0 0", color: "var(--v3-text-3)", fontSize: 13 }}>
          {t(r.type === "A2A" ? "v2.registry.lifecycleNoteA2A" : "registry.drawer.useNeedsApproved")}
        </p>
      </Panel>

      <div className="v3-grid c4">
        <Panel><Stat label={t("v3.registry.version")} value={<span className="mono" style={{ fontSize: 22 }}>{r.version ?? "—"}</span>} /></Panel>
        <Panel signal={hidden ? "wait" : hidden === false ? "ok" : undefined}>
          <Stat label={t("v3.registry.consumers")} value={hidden == null ? "—" : hidden ? t("v3.registry.hidden") : t("v3.registry.visible")}
            foot={hidden ? t("registry.consumer.notDiscoverableHint") : undefined} />
        </Panel>
        <Panel><Stat label={t("v3.registry.created")} value={ago(r.created_at)} /></Panel>
        <Panel><Stat label={t("v3.registry.updated")} value={ago(r.updated_at)} /></Panel>
      </div>

      {card && (
        <Panel title={t("v2.registry.agentCard")} flush={(card.skills ?? []).length > 0}>
          <div style={(card.skills ?? []).length > 0 ? { padding: "0 20px 16px" } : undefined}>
            <dl className="v3-kv">
              <dt>{t("v2.registry.cardTransport")}</dt>
              <dd><Chip s={card.metadata?.["launchpad.transport"] === "a2a-jsonrpc" ? "ok" : undefined}>{card.metadata?.["launchpad.transport"] ?? "—"}</Chip></dd>
              <dt>{t("v2.registry.cardStreaming")}</dt>
              <dd>{card.capabilities?.streaming ? t("v2.common.yes") : t("v2.common.no")}</dd>
              <dt>{t("v2.registry.cardUrl")}</dt>
              <dd className="mono" style={{ display: "flex", gap: 8, alignItems: "center" }}>
                {card.url ?? "—"}
                {card.url && (
                  <button type="button" className="v3-btn ghost sm" aria-label={t("registry.drawer.cardUrlCopy")}
                    onClick={() => {
                      void navigator.clipboard?.writeText(card.url ?? "").catch(() => undefined);
                      toast("ok", t("registry.drawer.cardUrlCopied"));
                    }}>
                    <Copy size={13} />
                  </button>
                )}
              </dd>
            </dl>
          </div>
          {(card.skills ?? []).length > 0 && (
            <table className="v3-table">
              <thead><tr><th>{t("v3.registry.skill")}</th><th>{t("v3.registry.description")}</th><th>{t("v3.registry.tags")}</th></tr></thead>
              <tbody>
                {(card.skills ?? []).map((sk) => (
                  <tr key={sk.id ?? sk.name}>
                    <td><b>{sk.name ?? sk.id}</b></td>
                    <td style={{ color: "var(--v3-text-2)" }}>{sk.description || "—"}</td>
                    <td><span style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>{(sk.tags ?? []).map((tg) => <Chip key={tg}>{tg}</Chip>)}</span></td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Panel>
      )}

      {r.type === "MCP" && (
        <Panel title={t("v3.registry.endpoint")}>
          <dl className="v3-kv"><dt>URL</dt><dd className="mono">{mcpUrl || "—"}</dd></dl>
        </Panel>
      )}

      {skill && (
        <Panel
          title={t("v3.registry.skillSource")}
          end={
            <span style={{ display: "inline-flex", gap: 8 }}>
              {canReimport && <Btn size="sm" disabled={busyHere} onClick={() => void reimport()}><RefreshCw size={13} /> {t("v3.registry.reimport")}</Btn>}
              {approved && (
                <Link className="v3-btn sm ghost" to={`/v2/skill-lab?tab=eval&view=new&record=${encodeURIComponent(r.record_id)}`}>
                  {t("v3.registry.evaluateSkill")}
                </Link>
              )}
            </span>
          }
        >
          <dl className="v3-kv">
            <dt>{t("v3.registry.source")}</dt>
            <dd>{skill.source ? <Chip>{t(`v2.registry.source.${skill.source.kind}`, { defaultValue: skill.source.kind })}</Chip> : "—"}</dd>
            {skill.source?.url && <><dt>URL</dt><dd className="mono">{skill.source.url}</dd></>}
            {skill.source?.ref && <><dt>Ref</dt><dd className="mono">{skill.source.ref}</dd></>}
            {skill.source?.commit && <><dt>Commit</dt><dd className="mono">{skill.source.commit.slice(0, 12)}</dd></>}
            {skill.source?.imported_at && <><dt>{t("v3.registry.imported")}</dt><dd>{ago(skill.source.imported_at)}</dd></>}
            <dt>{t("v3.registry.files")}</dt>
            <dd className="mono" style={{ color: "var(--v3-text-2)" }}>{skill.files.length ? skill.files.join("  ·  ") : "—"}</dd>
          </dl>
        </Panel>
      )}

      <Panel title={t("v3.registry.descriptor")}>
        <pre className="v3-pre">{descriptorExcerpt(r) || "—"}</pre>
      </Panel>

      {confirm && (
        <Confirm
          title={confirm === "delete" ? t("v3.registry.deleteTitle") : t("v3.registry.disableTitle")}
          confirmLabel={confirm === "delete" ? t("v3.registry.delete") : t("v2.registry.action.disable")}
          cancelLabel={t("v3.common.cancel")}
          danger
          busy={busyHere}
          onCancel={() => setConfirm(null)}
          onConfirm={() => {
            if (confirm === "delete") void remove();
            else void lifecycle.run(r, "disable").then(() => setConfirm(null));
          }}
        >
          {confirm === "delete" ? t("v3.registry.deleteBody", { name: r.name }) : t("v3.registry.disableBody", { name: r.name })}
        </Confirm>
      )}
    </div>
  );
}

export function V3Registry() {
  const [params] = useSearchParams();
  const id = params.get("id");
  return id ? <RecordDetail key={id} id={id} /> : <RecordList />;
}
