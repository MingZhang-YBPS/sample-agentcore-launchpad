/**
 * Admission: what becomes part of the standard, decided by a person. The queue
 * leads with candidates the evaluators scored as a pass (a missing case and a
 * calibration sample at once). Each shows the redaction preview — a candidate that
 * cannot be redacted cannot be admitted — and the nearest existing item. Admitting
 * needs a human expected answer; an agent's own output is not a standard.
 */
import { RefreshCw } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";

import { useAuth } from "../../../auth/auth-context";
import { dlcApi, errorMessage } from "../../../lib/api";
import { type AdmissionCandidate, type AdmissionQueue, CASE_TIERS, type CaseTier, type Redaction, type WritableSplit } from "../../../lib/dlc";
import { useLoad, useToast } from "../../hooks";
import { Btn, Chip, Dialog, Empty, Filters, Lamp, Notice, Panel, type Signal, Skeleton, Stat } from "../../ui";

const STATUSES = ["new", "admitted", "rejected", "duplicate"] as const;

function redactionSignal(r: Redaction | Record<string, never>): Signal | undefined {
  const status = (r as Redaction).status;
  return status === "blocked" ? "act" : status === "clean" ? "ok" : status === "redacted" ? "info" : status ? "wait" : undefined;
}

function RedactionChip({ redaction }: { redaction: Redaction | Record<string, never> }) {
  const { t } = useTranslation();
  const status = (redaction as Redaction).status;
  return <Chip s={redactionSignal(redaction)}>{t(`v2.dlc.admission.redaction.${status ?? "pending"}`)}</Chip>;
}

function AdmitDialog({ candidate, criteriaKeys, onClose, onDone }: { candidate: AdmissionCandidate; criteriaKeys: string[]; onClose: () => void; onDone: () => void }) {
  const { t } = useTranslation();
  const toast = useToast();
  const detail = useLoad(() => dlcApi.candidate(candidate.id), `v3-std-cand:${candidate.id}`);
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
      .admit(candidate.id, { split, expected_response: expected, expected_source: "annotator", criteria_ids: keys, case_tier: caseTier })
      .then(() => {
        toast("ok", t("v2.dlc.admission.admitted"));
        onDone();
        onClose();
      })
      .catch((err: unknown) => toast("act", errorMessage(err)))
      .finally(() => setBusy(false));
  };

  return (
    <Dialog
      wide
      title={t("v2.dlc.admission.admitTitle")}
      onClose={onClose}
      foot={
        <>
          <Btn kind="ghost" onClick={onClose}>{t("v3.common.cancel")}</Btn>
          <Btn kind="primary" disabled={busy || blocked || expected.trim() === ""} onClick={submit}>{t("v2.dlc.admission.admit")}</Btn>
        </>
      }
    >
      <div style={{ display: "grid", gap: 12, maxHeight: "60vh", overflowY: "auto" }}>
        {detail.loading && <Skeleton rows={2} />}
        {blocked && <Notice s="act">{t("v2.dlc.admission.blockedHint")}</Notice>}
        <div className="v3-grid c2">
          <div className="v3-field"><span>{t("v2.dlc.admission.question")}</span><pre className="v3-pre">{redaction?.question ?? row.question}</pre></div>
          <div className="v3-field"><span>{t("v2.dlc.admission.agentAnswer")}</span><pre className="v3-pre">{redaction?.answer ?? row.answer}</pre></div>
        </div>
        <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
          <RedactionChip redaction={row.redaction} />
          <Chip>{t("v2.dlc.admission.source")}: {row.source}</Chip>
          <Chip>{t("v2.dlc.admission.impact")}: {row.affected_sessions ?? 1}</Chip>
          <Chip s={row.judged_wrong ? "act" : undefined}>{row.judged_wrong ? t("v2.dlc.admission.judgedWrong") : t("v2.dlc.admission.judgedOk")}</Chip>
        </div>
        {redaction?.entities?.length > 0 && <Notice s="wait">{t("v2.dlc.admission.entities", { list: redaction.entities.join(", ") })}</Notice>}
        {(row.nearest ?? []).length > 0 && (
          <Notice s="wait">{t("v2.dlc.admission.duplicateHint", { list: (row.nearest ?? []).map((n) => `${n.scenario_id} (${n.split})`).join(", ") })}</Notice>
        )}
        <label className="v3-field">
          <span>{t("v2.dlc.admission.expected")}</span>
          <textarea className="v3-input" rows={4} value={expected} onChange={(e) => setExpected(e.target.value)} />
          <small className="v3-hint">{t("v2.dlc.admission.expectedHint")}</small>
        </label>
        <div className="v3-grid c3">
          <label className="v3-field">
            <span>{t("v2.dlc.admission.split")}</span>
            {/* the holdout is never offered: it is written only by seed */}
            <select className="v3-select" value={split} onChange={(e) => setSplit(e.target.value as WritableSplit)}>
              {(["dev", "regression"] as WritableSplit[]).map((s) => <option key={s} value={s}>{t(`v2.dlc.split.${s}`)}</option>)}
            </select>
          </label>
          <label className="v3-field">
            <span>{t("v2.dlc.golden.caseTier")}</span>
            <select className="v3-select" value={caseTier} onChange={(e) => setCaseTier(e.target.value as CaseTier)}>
              {CASE_TIERS.map((c) => <option key={c} value={c}>{t(`v2.dlc.caseTier.${c}`)}</option>)}
            </select>
          </label>
          <div className="v3-field">
            <span>{t("v2.dlc.admission.criteria")}</span>
            <div className="v3-std-keys">
              {criteriaKeys.map((key) => (
                <label key={key}>
                  <input type="checkbox" checked={keys.includes(key)}
                    onChange={(e) => setKeys((prev) => (e.target.checked ? [...prev, key] : prev.filter((k) => k !== key)))} />
                  <span className="mono">{key}</span>
                </label>
              ))}
              {criteriaKeys.length === 0 && <span className="v3-std-muted">{t("v2.dlc.admission.noCriteria")}</span>}
            </div>
          </div>
        </div>
      </div>
    </Dialog>
  );
}

export function Admission({ agentId, criteriaKeys }: { agentId: string | null; criteriaKeys: string[] }) {
  const { t } = useTranslation();
  const { can } = useAuth();
  const toast = useToast();
  const [status, setStatus] = useState<(typeof STATUSES)[number]>("new");
  const [nonce, setNonce] = useState(0);
  const [admitting, setAdmitting] = useState<AdmissionCandidate | null>(null);
  const [rejecting, setRejecting] = useState<AdmissionCandidate | null>(null);
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const queue = useLoad<AdmissionQueue>(
    () => dlcApi.admissionQueue({ agentId: agentId ?? undefined, status }),
    `v3-std-adm:${agentId ?? ""}:${status}:${nonce}`,
  );
  const mayAdmit = can("golden.admit");
  const data = queue.data;
  const order = new Map((data?.priority ?? []).map((id, index) => [id, index]));
  const rows = [...(data?.candidates ?? [])].sort((a, b) => (order.get(a.id) ?? 999) - (order.get(b.id) ?? 999));

  const collect = () => {
    if (!agentId) return;
    setBusy(true);
    dlcApi
      .admissionQueue({ agentId, status, refresh: true })
      .then(() => setNonce((n) => n + 1))
      .catch((err: unknown) => toast("act", errorMessage(err)))
      .finally(() => setBusy(false));
  };

  return (
    <div style={{ display: "grid", gap: 16 }}>
      <div className="v3-grid c4">
        {STATUSES.map((key) => (
          <Panel key={key} signal={key === "new" && (data?.counts?.new ?? 0) > 0 ? "wait" : undefined}>
            <Stat label={t(`v2.dlc.admission.status.${key}`)} value={data?.counts?.[key] ?? 0} />
          </Panel>
        ))}
      </div>

      <div style={{ display: "flex", gap: 12, alignItems: "center", flexWrap: "wrap" }}>
        <Filters value={status} onChange={setStatus} options={STATUSES.map((s) => ({ value: s, label: t(`v2.dlc.admission.status.${s}`) }))} />
        <span style={{ marginLeft: "auto" }} />
        <Btn size="sm" disabled={busy || !agentId} onClick={collect}><RefreshCw size={13} /> {t("v2.dlc.admission.collect")}</Btn>
      </div>
      <p className="v3-std-muted" style={{ margin: 0 }}>{t("v2.dlc.admission.sub")}</p>

      <Panel title={t("v2.dlc.admission.queue")} flush signal={status === "new" && rows.length > 0 ? "wait" : undefined} end={<span className="mono">{rows.length}</span>}>
        {queue.loading && !data ? (
          <div style={{ padding: 20 }}><Skeleton rows={4} /></div>
        ) : queue.error ? (
          <div style={{ padding: 20 }}><Notice s="act">{queue.error}</Notice></div>
        ) : rows.length === 0 ? (
          <Empty title={t("v2.dlc.admission.empty")} />
        ) : (
          <table className="v3-table">
            <thead>
              <tr>
                <th />
                <th>{t("v2.dlc.admission.question")}</th>
                <th>{t("v2.dlc.admission.source")}</th>
                <th className="num">{t("v2.dlc.admission.impact")}</th>
                <th>{t("v2.dlc.admission.redactionCol")}</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.id}>
                  <td style={{ width: 30 }}><Lamp s={row.judged_wrong ? "act" : row.status === "new" ? "wait" : "off"} /></td>
                  <td>
                    <div className="v3-name"><div><b>{row.question || t("v2.dlc.admission.noQuestion")}</b>
                      <small>{row.judged_wrong ? t("v2.dlc.admission.judgedWrong") : row.cluster_name ?? ""}</small></div></div>
                  </td>
                  <td>{row.source}</td>
                  <td className="num">{row.affected_sessions ?? 1}</td>
                  <td><RedactionChip redaction={row.redaction} /></td>
                  <td style={{ width: 1, whiteSpace: "nowrap" }}>
                    {row.status === "new" && mayAdmit ? (
                      <span style={{ display: "inline-flex", gap: 6 }}>
                        <Btn size="sm" kind="primary" onClick={() => setAdmitting(row)}>{t("v2.dlc.admission.admit")}</Btn>
                        <Btn size="sm" kind="ghost" onClick={() => { setNote(""); setRejecting(row); }}>{t("v2.dlc.admission.reject")}</Btn>
                      </span>
                    ) : (
                      <span className="v3-std-muted">{t(`v2.dlc.admission.status.${row.status}`)}</span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Panel>

      {(data?.clusters ?? []).length > 0 && (
        <Panel title={t("v2.dlc.admission.cluster")} flush>
          <table className="v3-table">
            <thead><tr><th>{t("v2.dlc.admission.cluster")}</th><th className="num">{t("v2.dlc.admission.impact")}</th><th className="num">{t("v2.dlc.admission.candidates")}</th></tr></thead>
            <tbody>
              {(data?.clusters ?? []).map((c) => (
                <tr key={c.cluster_id}><td>{c.name ?? c.cluster_id}</td><td className="num">{c.affected_sessions ?? "—"}</td><td className="num">{c.candidates}</td></tr>
              ))}
            </tbody>
          </table>
        </Panel>
      )}

      {admitting && <AdmitDialog candidate={admitting} criteriaKeys={criteriaKeys} onClose={() => setAdmitting(null)} onDone={() => setNonce((n) => n + 1)} />}
      {rejecting && (
        <Dialog
          title={t("v2.dlc.admission.rejectTitle")}
          onClose={() => setRejecting(null)}
          foot={
            <>
              <Btn kind="ghost" onClick={() => setRejecting(null)}>{t("v3.common.cancel")}</Btn>
              <Btn kind="danger" disabled={busy || note.trim() === ""}
                onClick={() => {
                  const target = rejecting;
                  setRejecting(null);
                  setBusy(true);
                  dlcApi
                    .rejectCandidate(target.id, note)
                    .then(() => { toast("ok", t("v2.dlc.admission.rejected")); setNonce((n) => n + 1); })
                    .catch((err: unknown) => toast("act", errorMessage(err)))
                    .finally(() => setBusy(false));
                }}>
                {t("v2.dlc.admission.reject")}
              </Btn>
            </>
          }
        >
          <label className="v3-field">
            <span>{t("v2.dlc.admission.rejectReason")}</span>
            <textarea className="v3-input" rows={3} value={note} onChange={(e) => setNote(e.target.value)} />
            <small className="v3-hint">{t("v2.dlc.admission.rejectHint")}</small>
          </label>
        </Dialog>
      )}
    </div>
  );
}
