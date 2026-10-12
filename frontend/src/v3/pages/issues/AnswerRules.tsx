import { ArrowDown, ArrowUp, FlaskConical, Pencil, Plus, Power, Trash2 } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";

import { type AnswerRuleInfo, errorMessage, ruleApi } from "../../../lib/api";
import { useWorkspace } from "../../../workspace/workspace-context";
import { ago } from "../../format";
import { useLoad, useToast } from "../../hooks";
import { Btn, Chip, Confirm, Dialog, Empty, Lamp, Notice, Panel, Skeleton } from "../../ui";
import { useAgents } from "./common";

/**
 * Curated answers — an ordered list per agent: the first enabled rule that
 * matches the question answers it, with no model call. A curated answer is always
 * labelled in chat, history and the API response; never passed off as the model's.
 */
export function AnswerRules() {
  const { t } = useTranslation();
  const toast = useToast();
  const { current } = useWorkspace();
  const { agents, label } = useAgents(current?.id ?? "");
  const [picked, setPicked] = useState("");
  const agentId = picked || agents[0]?.id || "";
  const [tick, setTick] = useState(0);
  const list = useLoad(() => (agentId ? ruleApi.list(agentId) : Promise.resolve(null)), `v3-rules:${agentId}:${tick}`);
  const data = list.data;
  const [editing, setEditing] = useState<AnswerRuleInfo | "new" | null>(null);
  const [removing, setRemoving] = useState<AnswerRuleInfo | null>(null);
  const [probe, setProbe] = useState("");
  const [probeResult, setProbeResult] = useState<{ s: "ok" | "off" | "wait"; text: string } | null>(null);
  const reload = () => setTick((n) => n + 1);

  const act = async (fn: () => Promise<unknown>) => {
    try {
      await fn();
      reload();
    } catch (err) {
      toast("act", errorMessage(err));
    }
  };
  const move = (index: number, delta: number) => {
    if (!data) return;
    const ids = data.rules.map((r) => r.id);
    const target = index + delta;
    if (target < 0 || target >= ids.length) return;
    [ids[index], ids[target]] = [ids[target], ids[index]];
    void act(() => ruleApi.reorder(agentId, ids));
  };
  const rules = data?.rules ?? [];

  return (
    <div style={{ display: "grid", gap: 16 }}>
      <div style={{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap" }}>
        <select className="v3-select v3-iss-select" value={agentId} onChange={(e) => setPicked(e.target.value)} aria-label={t("selfService.common.agent")}>
          {agents.map((a) => <option key={a.id} value={a.id}>{label(a.id)}</option>)}
        </select>
        {data && (
          <Btn size="sm" kind={data.enabled ? undefined : "danger"} onClick={() => void act(() => ruleApi.setEnabled(agentId, !data.enabled))}>
            <Power size={13} /> {data.enabled === false ? t("selfService.answers.agentOff") : t("selfService.answers.agentOn")}
          </Btn>
        )}
        <span style={{ color: "var(--v3-text-3)", fontSize: 13 }}>{t("selfService.answers.explain")}</span>
        <span style={{ marginLeft: "auto" }}>
          <Btn kind="primary" disabled={!agentId} onClick={() => setEditing("new")}><Plus size={14} /> {t("selfService.answers.add")}</Btn>
        </span>
      </div>
      {data?.enabled === false && <Notice s="wait">{t("selfService.answers.agentDisabled")}</Notice>}

      <Panel flush title={t("selfService.answers.title")} end={data ? <span className="mono">{rules.length} / {data.limit}</span> : undefined}>
        {!agentId && !list.loading ? (
          <Empty title={t("v3.issues.noAgents")} />
        ) : list.loading && !data ? (
          <div style={{ padding: 20 }}><Skeleton rows={4} /></div>
        ) : list.error ? (
          <div style={{ padding: 20 }}><Notice s="act">{list.error}</Notice></div>
        ) : rules.length === 0 ? (
          <Empty title={t("selfService.answers.empty")} />
        ) : (
          <table className="v3-table">
            <thead>
              <tr>
                <th className="num">#</th>
                <th />
                <th>{t("selfService.answers.pattern")}</th>
                <th>{t("selfService.answers.answer")}</th>
                <th className="num">{t("selfService.answers.hits")}</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {rules.map((r, index) => (
                <tr key={r.id} className={r.enabled ? undefined : "v3-iss-off"}>
                  <td className="num" style={{ width: 40 }}>{r.position + 1}</td>
                  <td style={{ width: 44 }}>
                    <button type="button" className="v3-btn ghost sm" aria-pressed={r.enabled} aria-label={t("selfService.answers.enabled")}
                      onClick={() => void act(() => ruleApi.update(agentId, r.id, { enabled: !r.enabled }))}>
                      <Lamp s={r.enabled && data?.enabled !== false ? "ok" : "off"} />
                    </button>
                  </td>
                  <td>
                    <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
                      <Chip>{t(`selfService.answers.match_${r.match}`)}</Chip>
                      <span>{r.pattern}</span>
                    </div>
                    {r.name && <small style={{ color: "var(--v3-text-3)" }}>{r.name}</small>}
                  </td>
                  <td><span className="v3-iss-answer" title={r.answer}>{r.answer}</span></td>
                  <td className="num" title={r.last_hit_at ? ago(r.last_hit_at) : undefined}>{r.hit_count}</td>
                  <td style={{ width: 1, whiteSpace: "nowrap" }}>
                    <div style={{ display: "flex", gap: 2, justifyContent: "flex-end" }}>
                      <Btn size="sm" kind="ghost" disabled={index <= 0} title={t("selfService.answers.up")} onClick={() => move(index, -1)}><ArrowUp size={13} /></Btn>
                      <Btn size="sm" kind="ghost" disabled={index >= rules.length - 1} title={t("selfService.answers.down")} onClick={() => move(index, 1)}><ArrowDown size={13} /></Btn>
                      <Btn size="sm" kind="ghost" title={t("v3.issues.edit")} onClick={() => setEditing(r)}><Pencil size={13} /></Btn>
                      <Btn size="sm" kind="ghost" title={t("v3.issues.delete")} onClick={() => setRemoving(r)}><Trash2 size={13} /></Btn>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Panel>

      <Panel title={t("selfService.answers.testTitle")}>
        <p style={{ margin: "0 0 12px", color: "var(--v3-text-3)", fontSize: 13 }}>{t("selfService.answers.testDesc")}</p>
        <form
          style={{ display: "flex", gap: 8 }}
          onSubmit={async (e) => {
            e.preventDefault();
            if (!probe.trim() || !agentId) return;
            try {
              const res = await ruleApi.test(agentId, probe);
              setProbeResult(
                !res.agent_enabled
                  ? { s: "off", text: t("selfService.answers.testOff") }
                  : res.rule
                    ? { s: "ok", text: t("selfService.answers.testHit", { position: res.rule.position + 1, answer: res.rule.answer }) }
                    : { s: "wait", text: t("selfService.answers.testMiss") },
              );
            } catch (err) {
              setProbeResult({ s: "wait", text: errorMessage(err) });
            }
          }}
        >
          <input className="v3-input" value={probe} maxLength={500} placeholder={t("selfService.answers.testPlaceholder")}
            aria-label={t("selfService.answers.testTitle")} onChange={(e) => setProbe(e.target.value)} />
          <Btn type="submit" disabled={!probe.trim() || !agentId}><FlaskConical size={14} /> {t("selfService.answers.test")}</Btn>
        </form>
        {probeResult && <div style={{ marginTop: 12 }}><Notice s={probeResult.s === "off" ? "wait" : probeResult.s === "ok" ? "ok" : "info"}>{probeResult.text}</Notice></div>}
      </Panel>

      {editing && (
        <RuleDialog agentId={agentId} rule={editing === "new" ? null : editing} onClose={() => setEditing(null)}
          onSaved={() => {
            setEditing(null);
            reload();
          }} />
      )}
      {removing && (
        <Confirm
          title={t("selfService.answers.deleteTitle")}
          confirmLabel={t("v3.issues.delete")}
          cancelLabel={t("v3.common.cancel")}
          danger
          onCancel={() => setRemoving(null)}
          onConfirm={() => {
            const target = removing;
            setRemoving(null);
            void act(() => ruleApi.remove(agentId, target.id));
          }}
        >
          {t("selfService.answers.deleteBody", { pattern: removing.pattern })}
        </Confirm>
      )}
    </div>
  );
}

function RuleDialog({ agentId, rule, onClose, onSaved }: { agentId: string; rule: AnswerRuleInfo | null; onClose: () => void; onSaved: () => void }) {
  const { t } = useTranslation();
  const [pattern, setPattern] = useState(rule?.pattern ?? "");
  const [match, setMatch] = useState<"exact" | "contains">(rule?.match ?? "exact");
  const [answer, setAnswer] = useState(rule?.answer ?? "");
  const [name, setName] = useState(rule?.name ?? "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const save = async () => {
    setBusy(true);
    setError(null);
    try {
      if (rule) await ruleApi.update(agentId, rule.id, { name, match, pattern, answer });
      else await ruleApi.create(agentId, { name, match, pattern, answer });
      onSaved();
    } catch (err) {
      setError(errorMessage(err));
      setBusy(false);
    }
  };
  return (
    <Dialog
      wide
      title={rule ? t("selfService.answers.edit") : t("selfService.answers.add")}
      onClose={onClose}
      foot={
        <>
          <Btn kind="ghost" onClick={onClose} disabled={busy}>{t("v3.common.cancel")}</Btn>
          <Btn kind="primary" disabled={busy || !pattern.trim() || !answer.trim()} onClick={() => void save()}>{t("v3.issues.save")}</Btn>
        </>
      }
    >
      <div style={{ display: "grid", gap: 12 }}>
        <label className="v3-field">
          <span>{t("selfService.answers.name")}</span>
          <input className="v3-input" maxLength={64} value={name} onChange={(e) => setName(e.target.value)} />
        </label>
        <label className="v3-field">
          <span>{t("selfService.answers.match")}</span>
          <select className="v3-select" value={match} onChange={(e) => setMatch(e.target.value as "exact" | "contains")}>
            <option value="exact">{t("selfService.answers.matchExact")}</option>
            <option value="contains">{t("selfService.answers.matchContains")}</option>
          </select>
          <small className="v3-hint">{t(`selfService.answers.hint_${match}`)}</small>
        </label>
        <label className="v3-field">
          <span>{t("selfService.answers.pattern")} *</span>
          <input className="v3-input" maxLength={500} value={pattern} onChange={(e) => setPattern(e.target.value)} />
          <small className="v3-hint">{t("selfService.answers.patternHint")}</small>
        </label>
        <label className="v3-field">
          <span>{t("selfService.answers.answer")} *</span>
          <textarea className="v3-input" rows={5} maxLength={8000} value={answer} onChange={(e) => setAnswer(e.target.value)} />
        </label>
        {error && <Notice s="act">{error}</Notice>}
      </div>
    </Dialog>
  );
}
