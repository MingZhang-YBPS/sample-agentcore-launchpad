import { ArrowDown, ArrowUp, Pencil, Plus, Power, Trash2 } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";

import { type AnswerRuleInfo, errorMessage, ruleApi } from "../../../lib/api";
import { fmtTime } from "../../format";
import { useLoad, useV2Toast } from "../../hooks";
import { Alert, Button, Card, Confirm, Field, FilterSelect, Select, Modal, Table, Tag } from "../../ui";
import "./selfservice.css";
import { useAgents } from "./useAgents";

/**
 * T35 — curated answers. An ordered list per agent: the first enabled rule that matches
 * the question answers it, with no model call. A curated answer is always labelled in the
 * chat, the history and the API response -- it is never passed off as the model's.
 */
export function AnswerRules() {
  const { t } = useTranslation();
  const toast = useV2Toast();
  const { agents, label } = useAgents();
  const [picked, setPicked] = useState("");
  const agentId = picked || agents[0]?.id || "";
  const [tick, setTick] = useState(0);
  const list = useLoad(
    () => (agentId ? ruleApi.list(agentId) : Promise.resolve(null)),
    `rules:${agentId}:${tick}`,
  );
  const data = list.data;
  const [editing, setEditing] = useState<AnswerRuleInfo | "new" | null>(null);
  const [removing, setRemoving] = useState<AnswerRuleInfo | null>(null);
  const [probe, setProbe] = useState("");
  const [probeResult, setProbeResult] = useState<string | null>(null);
  const reload = () => setTick((n) => n + 1);

  const act = async (fn: () => Promise<unknown>) => {
    try {
      await fn();
      reload();
    } catch (err) {
      toast("error", errorMessage(err));
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

  return (
    <>
      <Alert tone="info">{t("selfService.answers.explain")}</Alert>
      <Card
        title={t("selfService.answers.title")}
        end={
          <>
            <FilterSelect
              label={t("selfService.common.agent")}
              value={agentId}
              options={agents.map((a) => ({ value: a.id, label: label(a.id) }))}
              onChange={setPicked}
            />
            <Button
              disabled={!data}
              onClick={() => void act(() => ruleApi.setEnabled(agentId, !data?.enabled))}
              testId="v2-rules-agent-switch"
            >
              <Power size={14} aria-hidden="true" />{" "}
              {data?.enabled === false ? t("selfService.answers.agentOff") : t("selfService.answers.agentOn")}
            </Button>
            <Button kind="primary" disabled={!agentId} onClick={() => setEditing("new")} testId="v2-rules-add">
              <Plus size={14} aria-hidden="true" /> {t("selfService.answers.add")}
            </Button>
          </>
        }
      >
        {data?.enabled === false && <Alert tone="warn">{t("selfService.answers.agentDisabled")}</Alert>}
        <Table<AnswerRuleInfo>
          rows={data?.rules ?? []}
          rowKey={(r) => r.id}
          loading={list.loading}
          error={list.error}
          onRetry={reload}
          empty={t("selfService.answers.empty")}
          testId="v2-rules-table"
          columns={[
            { key: "pos", title: "#", className: "nowrap", render: (r) => r.position + 1 },
            {
              key: "on",
              title: t("selfService.answers.enabled"),
              render: (r) => (
                <input
                  type="checkbox"
                  checked={r.enabled}
                  aria-label={t("selfService.answers.enabled")}
                  onChange={() => void act(() => ruleApi.update(agentId, r.id, { enabled: !r.enabled }))}
                  data-testid="v2-rule-toggle"
                />
              ),
            },
            {
              key: "q",
              title: t("selfService.answers.pattern"),
              render: (r) => (
                <span className={r.enabled ? "" : "rule-row-off"}>
                  <Tag tone="outline">{t(`selfService.answers.match_${r.match}`)}</Tag> {r.pattern}
                </span>
              ),
            },
            { key: "a", title: t("selfService.answers.answer"), render: (r) => <span className="rule-answer clip" title={r.answer}>{r.answer}</span> },
            {
              key: "hits",
              title: t("selfService.answers.hits"),
              className: "nowrap",
              render: (r) => (
                <span title={r.last_hit_at ? fmtTime(r.last_hit_at) : undefined}>{r.hit_count}</span>
              ),
            },
            {
              key: "act",
              title: t("v2.common.actions"),
              className: "right nowrap",
              render: (r) => {
                const index = (data?.rules ?? []).findIndex((x) => x.id === r.id);
                return (
                  <>
                    <Button size="sm" disabled={index <= 0} title={t("selfService.answers.up")} onClick={() => move(index, -1)}>
                      <ArrowUp size={13} aria-hidden="true" />
                    </Button>{" "}
                    <Button size="sm" disabled={index >= (data?.rules.length ?? 0) - 1} title={t("selfService.answers.down")} onClick={() => move(index, 1)}>
                      <ArrowDown size={13} aria-hidden="true" />
                    </Button>{" "}
                    <Button size="sm" title={t("v2.common.edit")} onClick={() => setEditing(r)}>
                      <Pencil size={13} aria-hidden="true" />
                    </Button>{" "}
                    <Button size="sm" kind="danger" title={t("v2.common.delete")} onClick={() => setRemoving(r)}>
                      <Trash2 size={13} aria-hidden="true" />
                    </Button>
                  </>
                );
              },
            },
          ]}
        />
      </Card>

      <Card title={t("selfService.answers.testTitle")} sub={t("selfService.answers.testDesc")}>
        <div className="v2-row-actions">
          <input
            className="v2-input"
            style={{ flex: 1 }}
            value={probe}
            maxLength={500}
            placeholder={t("selfService.answers.testPlaceholder")}
            onChange={(e) => setProbe(e.target.value)}
          />
          <Button
            disabled={!probe.trim() || !agentId}
            onClick={async () => {
              try {
                const res = await ruleApi.test(agentId, probe);
                setProbeResult(
                  !res.agent_enabled
                    ? t("selfService.answers.testOff")
                    : res.rule
                      ? t("selfService.answers.testHit", { position: res.rule.position + 1, answer: res.rule.answer })
                      : t("selfService.answers.testMiss"),
                );
              } catch (err) {
                setProbeResult(errorMessage(err));
              }
            }}
            testId="v2-rules-test"
          >
            {t("selfService.answers.test")}
          </Button>
        </div>
        {probeResult && <p data-testid="v2-rules-test-result">{probeResult}</p>}
      </Card>

      {editing && (
        <RuleModal
          agentId={agentId}
          rule={editing === "new" ? null : editing}
          onClose={() => setEditing(null)}
          onSaved={() => {
            setEditing(null);
            reload();
          }}
        />
      )}
      <Confirm
        open={removing !== null}
        title={t("selfService.answers.deleteTitle")}
        body={t("selfService.answers.deleteBody", { pattern: removing?.pattern ?? "" })}
        confirmLabel={t("v2.common.delete")}
        danger
        onClose={() => setRemoving(null)}
        onConfirm={() => {
          const target = removing;
          setRemoving(null);
          if (target) void act(() => ruleApi.remove(agentId, target.id));
        }}
      />
    </>
  );
}

function RuleModal({
  agentId,
  rule,
  onClose,
  onSaved,
}: {
  agentId: string;
  rule: AnswerRuleInfo | null;
  onClose: () => void;
  onSaved: () => void;
}) {
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
    <Modal
      open
      title={rule ? t("selfService.answers.edit") : t("selfService.answers.add")}
      onClose={onClose}
      testId="v2-rule-modal"
      footer={
        <>
          <Button onClick={onClose}>{t("v2.common.cancel")}</Button>
          <Button kind="primary" disabled={busy || !pattern.trim() || !answer.trim()} onClick={() => void save()} testId="v2-rule-save">
            {t("v2.common.save")}
          </Button>
        </>
      }
    >
      <div className="v2-stack">
        <Field label={t("selfService.answers.name")}>
          <input className="v2-input" maxLength={64} value={name} onChange={(e) => setName(e.target.value)} />
        </Field>
        <Field label={t("selfService.answers.match")} hint={t(`selfService.answers.hint_${match}`)}>
          <Select
            value={match}
            options={[
              { value: "exact", label: t("selfService.answers.matchExact") },
              { value: "contains", label: t("selfService.answers.matchContains") },
            ]}
            onChange={(v) => setMatch(v as "exact" | "contains")}
          />
        </Field>
        <Field label={t("selfService.answers.pattern")} hint={t("selfService.answers.patternHint")} required>
          <input className="v2-input" maxLength={500} value={pattern} onChange={(e) => setPattern(e.target.value)} data-testid="v2-rule-pattern" />
        </Field>
        <Field label={t("selfService.answers.answer")} required>
          <textarea className="v2-textarea" rows={5} maxLength={8000} value={answer} onChange={(e) => setAnswer(e.target.value)} data-testid="v2-rule-answer" />
        </Field>
        {error && <Alert tone="error">{error}</Alert>}
      </div>
    </Modal>
  );
}
