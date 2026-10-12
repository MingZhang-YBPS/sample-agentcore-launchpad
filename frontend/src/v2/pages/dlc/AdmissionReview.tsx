/**
 * Admission review — §7.6: what becomes part of the standard, decided by a person.
 *
 * The queue is ordered the way the decision actually wants it: a candidate the
 * current evaluators scored as a *pass* comes first, because it is both a missing
 * case and a calibration sample. Each candidate is shown with the evidence the
 * decision needs — the redaction preview (a candidate that cannot be redacted
 * cannot be admitted), the cluster impact, and the nearest existing golden item so
 * duplicates do not quietly inflate the set.
 *
 * Admitting requires a human expected answer. `agent_observed` is not offered: an
 * agent's own output is not a standard.
 */
import { RefreshCw } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";

import { useAuth } from "../../../auth/auth-context";
import { dlcApi, errorMessage } from "../../../lib/api";
import {
  CASE_TIERS,
  type AdmissionCandidate,
  type AdmissionQueue,
  type CaseTier,
  type Redaction,
  type WritableSplit,
} from "../../../lib/dlc";
import { useLoad, useV2Toast } from "../../hooks";
import { Alert, Button, Card, Field, Kpi, LinkButton, Modal, Select, Spin, Table, Tag } from "../../ui";

function RedactionBadge({ redaction }: { redaction: Redaction | Record<string, never> }) {
  const { t } = useTranslation();
  const status = (redaction as Redaction).status;
  if (!status) return <Tag tone="gray">{t("v2.dlc.admission.redaction.pending")}</Tag>;
  const tone = status === "blocked" ? "red" : status === "clean" ? "green" : status === "redacted" ? "blue" : "orange";
  return <Tag tone={tone}>{t(`v2.dlc.admission.redaction.${status}`)}</Tag>;
}

function AdmitModal({
  candidate,
  criteriaKeys,
  onClose,
  onDone,
}: {
  candidate: AdmissionCandidate;
  criteriaKeys: string[];
  onClose: () => void;
  onDone: () => void;
}) {
  const { t } = useTranslation();
  const toast = useV2Toast();
  const detail = useLoad(() => dlcApi.candidate(candidate.id), `candidate:${candidate.id}`);
  const [expected, setExpected] = useState("");
  const [split, setSplit] = useState<WritableSplit>("regression");
  const [caseTier, setCaseTier] = useState<CaseTier>("known_bad");
  const [keys, setKeys] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const row = detail.data ?? candidate;
  const redaction = row.redaction as Redaction;
  const blocked = redaction?.status === "blocked";

  const submit = () => {
    setBusy(true);
    dlcApi
      .admit(candidate.id, {
        split,
        expected_response: expected,
        expected_source: "annotator",
        criteria_ids: keys,
        case_tier: caseTier,
      })
      .then(() => {
        toast("success", t("v2.dlc.admission.admitted"));
        onDone();
        onClose();
      })
      .catch((error: unknown) => toast("error", errorMessage(error)))
      .finally(() => setBusy(false));
  };

  return (
    <Modal
      open
      wide
      tall
      title={t("v2.dlc.admission.admitTitle")}
      onClose={onClose}
      footer={
        <>
          <Button onClick={onClose}>{t("v2.common.cancel")}</Button>
          <Button kind="primary" disabled={busy || blocked || expected.trim() === ""} onClick={submit}>
            {t("v2.dlc.admission.admit")}
          </Button>
        </>
      }
    >
      {detail.loading && <Spin />}
      {blocked && <Alert tone="error">{t("v2.dlc.admission.blockedHint")}</Alert>}
      <div className="v2-dlc-candidate">
        <div>
          <h4>{t("v2.dlc.admission.question")}</h4>
          <pre className="v2-pre">{redaction?.question ?? row.question}</pre>
        </div>
        <div>
          <h4>{t("v2.dlc.admission.agentAnswer")}</h4>
          <pre className="v2-pre">{redaction?.answer ?? row.answer}</pre>
        </div>
      </div>
      <div className="v2-dlc-kpis">
        <Kpi label={t("v2.dlc.admission.source")} value={row.source} />
        <Kpi label={t("v2.dlc.admission.impact")} value={String(row.affected_sessions ?? 1)} />
        <Kpi
          label={t("v2.dlc.admission.judged")}
          value={row.judged_wrong ? t("v2.dlc.admission.judgedWrong") : t("v2.dlc.admission.judgedOk")}
          tone={row.judged_wrong ? "bad" : undefined}
        />
      </div>
      {redaction?.entities?.length > 0 && (
        <Alert tone="warn">{t("v2.dlc.admission.entities", { list: redaction.entities.join(", ") })}</Alert>
      )}
      {(row.nearest ?? []).length > 0 && (
        <Alert tone="warn">
          {t("v2.dlc.admission.duplicateHint", {
            list: (row.nearest ?? []).map((n) => `${n.scenario_id} (${n.split})`).join(", "),
          })}
        </Alert>
      )}
      <Field label={t("v2.dlc.admission.expected")} required hint={t("v2.dlc.admission.expectedHint")}>
        <textarea className="v2-input" rows={4} value={expected} onChange={(e) => setExpected(e.target.value)} />
      </Field>
      <div className="v2-dlc-grid3">
        <Field label={t("v2.dlc.admission.split")}>
          <Select
            value={split}
            onChange={(v) => setSplit(v as WritableSplit)}
            options={(["dev", "regression"] as WritableSplit[]).map((s) => ({
              value: s,
              label: t(`v2.dlc.split.${s}`),
            }))}
          />
        </Field>
        <Field label={t("v2.dlc.golden.caseTier")}>
          <Select
            value={caseTier}
            onChange={(v) => setCaseTier(v as CaseTier)}
            options={CASE_TIERS.map((c) => ({ value: c, label: t(`v2.dlc.caseTier.${c}`) }))}
          />
        </Field>
        <Field label={t("v2.dlc.admission.criteria")} hint={t("v2.dlc.admission.criteriaHint")}>
          <div className="v2-dlc-keys">
            {criteriaKeys.map((key) => (
              <label key={key}>
                <input
                  type="checkbox"
                  checked={keys.includes(key)}
                  onChange={(e) =>
                    setKeys((prev) => (e.target.checked ? [...prev, key] : prev.filter((k) => k !== key)))
                  }
                />
                <span className="mono">{key}</span>
              </label>
            ))}
            {criteriaKeys.length === 0 && <span className="v2-muted">{t("v2.dlc.admission.noCriteria")}</span>}
          </div>
        </Field>
      </div>
    </Modal>
  );
}

export function AdmissionReview({
  agentId,
  criteriaKeys,
}: {
  agentId: string | null;
  criteriaKeys: string[];
}) {
  const { t } = useTranslation();
  const { can } = useAuth();
  const toast = useV2Toast();
  const [status, setStatus] = useState("new");
  const [nonce, setNonce] = useState(0);
  const [admitting, setAdmitting] = useState<AdmissionCandidate | null>(null);
  const [rejecting, setRejecting] = useState<AdmissionCandidate | null>(null);
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const queue = useLoad<AdmissionQueue>(
    () => dlcApi.admissionQueue({ agentId: agentId ?? undefined, status }),
    `admission:${agentId ?? ""}:${status}:${nonce}`,
  );
  const mayAdmit = can("golden.admit");
  const data = queue.data;
  const order = new Map((data?.priority ?? []).map((id, index) => [id, index]));
  const rows = [...(data?.candidates ?? [])].sort(
    (a, b) => (order.get(a.id) ?? 999) - (order.get(b.id) ?? 999),
  );

  const refresh = () => {
    if (!agentId) return;
    setBusy(true);
    dlcApi
      .admissionQueue({ agentId, status, refresh: true })
      .then(() => setNonce((n) => n + 1))
      .catch((error: unknown) => toast("error", errorMessage(error)))
      .finally(() => setBusy(false));
  };

  return (
    <>
      <Card
        title={t("v2.dlc.admission.title")}
        sub={t("v2.dlc.admission.sub")}
        end={
          <>
            <Select
              value={status}
              onChange={setStatus}
              options={["new", "admitted", "rejected", "duplicate"].map((s) => ({
                value: s,
                label: t(`v2.dlc.admission.status.${s}`),
              }))}
            />
            <Button size="sm" disabled={busy || !agentId} onClick={refresh}>
              <RefreshCw size={13} /> {t("v2.dlc.admission.collect")}
            </Button>
          </>
        }
      >
        <div className="v2-dlc-kpis">
          {["new", "admitted", "rejected", "duplicate"].map((key) => (
            <Kpi key={key} label={t(`v2.dlc.admission.status.${key}`)} value={String(data?.counts?.[key] ?? 0)} />
          ))}
        </div>
        {(data?.clusters ?? []).length > 0 && (
          <table className="v2-table dense">
            <thead>
              <tr>
                <th>{t("v2.dlc.admission.cluster")}</th>
                <th>{t("v2.dlc.admission.impact")}</th>
                <th>{t("v2.dlc.admission.candidates")}</th>
              </tr>
            </thead>
            <tbody>
              {(data?.clusters ?? []).map((c) => (
                <tr key={c.cluster_id}>
                  <td>{c.name ?? c.cluster_id}</td>
                  <td>{c.affected_sessions ?? "—"}</td>
                  <td>{c.candidates}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>

      <Card title={t("v2.dlc.admission.queue")} flush>
        <Table
          rows={rows}
          rowKey={(row) => row.id}
          loading={queue.loading}
          error={queue.error}
          onRetry={queue.reload}
          empty={t("v2.dlc.admission.empty")}
          columns={[
            {
              key: "q",
              title: t("v2.dlc.admission.question"),
              render: (row) => (
                <div className="v2-dlc-qcell">
                  <span>{row.question || t("v2.dlc.admission.noQuestion")}</span>
                  {row.judged_wrong && <Tag tone="red">{t("v2.dlc.admission.judgedWrong")}</Tag>}
                </div>
              ),
            },
            { key: "source", title: t("v2.dlc.admission.source"), render: (row) => row.source },
            {
              key: "impact",
              title: t("v2.dlc.admission.impact"),
              render: (row) => String(row.affected_sessions ?? 1),
            },
            {
              key: "redaction",
              title: t("v2.dlc.admission.redactionCol"),
              render: (row) => <RedactionBadge redaction={row.redaction} />,
            },
            {
              key: "act",
              title: "",
              render: (row) =>
                row.status === "new" && mayAdmit ? (
                  <>
                    <LinkButton onClick={() => setAdmitting(row)}>{t("v2.dlc.admission.admit")}</LinkButton>
                    <LinkButton
                      danger
                      onClick={() => {
                        setNote("");
                        setRejecting(row);
                      }}
                    >
                      {t("v2.dlc.admission.reject")}
                    </LinkButton>
                  </>
                ) : (
                  <span className="v2-muted">{t(`v2.dlc.admission.status.${row.status}`)}</span>
                ),
            },
          ]}
        />
      </Card>

      {admitting && (
        <AdmitModal
          candidate={admitting}
          criteriaKeys={criteriaKeys}
          onClose={() => setAdmitting(null)}
          onDone={() => setNonce((n) => n + 1)}
        />
      )}

      <Modal
        open={rejecting !== null}
        title={t("v2.dlc.admission.rejectTitle")}
        onClose={() => setRejecting(null)}
        footer={
          <>
            <Button onClick={() => setRejecting(null)}>{t("v2.common.cancel")}</Button>
            <Button
              kind="danger"
              disabled={busy || note.trim() === ""}
              onClick={() => {
                const target = rejecting;
                setRejecting(null);
                if (!target) return;
                setBusy(true);
                dlcApi
                  .rejectCandidate(target.id, note)
                  .then(() => {
                    toast("success", t("v2.dlc.admission.rejected"));
                    setNonce((n) => n + 1);
                  })
                  .catch((error: unknown) => toast("error", errorMessage(error)))
                  .finally(() => setBusy(false));
              }}
            >
              {t("v2.dlc.admission.reject")}
            </Button>
          </>
        }
      >
        <Field label={t("v2.dlc.admission.rejectReason")} required hint={t("v2.dlc.admission.rejectHint")}>
          <textarea className="v2-input" rows={3} value={note} onChange={(e) => setNote(e.target.value)} />
        </Field>
      </Modal>
    </>
  );
}
