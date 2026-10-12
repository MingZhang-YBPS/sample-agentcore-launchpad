import { useState } from "react";
import { useTranslation } from "react-i18next";

import { api, ApiError, errorMessage, type V2FromSessionsResult, type V2Range, type V2SkippedSession } from "../../../lib/api";
import { useLoad, useToast } from "../../hooks";
import { Btn, Dialog, Filters, Notice } from "../../ui";

/**
 * Turn observed sessions (the trajectories behind picked traces) into dataset
 * items — appended to an existing local dataset or as a new one. Same call and
 * the same refusal reporting as V2's `AddToDatasetModal`.
 */
export function AddToDataset({
  sessionIds,
  range,
  onClose,
  onDone,
}: {
  sessionIds: string[];
  range: V2Range;
  onClose: () => void;
  onDone?: (result: V2FromSessionsResult) => void;
}) {
  const { t } = useTranslation();
  const toast = useToast();
  const datasets = useLoad(() => api.v2Datasets(), "v3-data-datasets-pick");
  const receivable = (datasets.data?.datasets ?? []).filter((d) => d.kind !== "simulated");
  const [mode, setMode] = useState<"new" | "existing">("new");
  const [datasetId, setDatasetId] = useState("");
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [firstTurnOnly, setFirstTurnOnly] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [refused, setRefused] = useState<V2SkippedSession[]>([]);
  const [result, setResult] = useState<V2FromSessionsResult | null>(null);

  const submit = async () => {
    if (mode === "existing" && !datasetId) return setError(t("v2.addToDataset.errPick"));
    if (mode === "new" && !name.trim()) return setError(t("v2.addToDataset.errName"));
    setBusy(true);
    setError(null);
    try {
      const res = await api.v2DatasetFromSessions({
        session_ids: sessionIds,
        range,
        first_turn_only: firstTurnOnly,
        ...(mode === "existing" ? { dataset_id: datasetId } : { name: name.trim(), description }),
      });
      setResult(res);
      toast("ok", t("v2.addToDataset.done", { count: res.added, name: res.dataset.name }));
      onDone?.(res);
    } catch (err) {
      setError(errorMessage(err));
      // nothing usable: the backend says why per session
      const detail = err instanceof ApiError ? (err.detail as { skipped?: V2SkippedSession[] } | null) : null;
      setRefused((detail?.skipped ?? []).filter((s) => s.session_id));
    } finally {
      setBusy(false);
    }
  };

  const reasons = (list: V2SkippedSession[]) => (
    <ul style={{ margin: "6px 0 0 16px", padding: 0 }}>
      {list.slice(0, 6).map((s) => (
        <li key={s.session_id}>
          <span className="mono">{s.session_id}</span> · {t(`v2.skipReason.${s.reason}`, { defaultValue: s.reason })}
        </li>
      ))}
    </ul>
  );
  const skipped = (result?.skipped ?? []).filter((s) => s.session_id);
  const duplicates = (result?.skipped ?? []).filter((s) => !s.session_id).length;

  return (
    <Dialog
      title={t("v2.addToDataset.title")}
      onClose={onClose}
      foot={
        result ? (
          <Btn kind="primary" onClick={onClose}>{t("v3.data.done")}</Btn>
        ) : (
          <>
            <Btn kind="ghost" onClick={onClose} disabled={busy}>{t("v3.common.cancel")}</Btn>
            <Btn kind="primary" onClick={() => void submit()} disabled={busy}>{t("v3.data.add")}</Btn>
          </>
        )
      }
    >
      {result ? (
        <div style={{ display: "grid", gap: 10 }}>
          <Notice s="ok">{t("v2.addToDataset.result", { count: result.added, name: result.dataset.name, total: result.dataset.item_count })}</Notice>
          {duplicates > 0 && <Notice s="wait">{t("v2.addToDataset.duplicates", { count: duplicates })}</Notice>}
          {skipped.length > 0 && <Notice s="wait">{t("v2.addToDataset.skipped", { count: skipped.length })}{reasons(skipped)}</Notice>}
        </div>
      ) : (
        <div style={{ display: "grid", gap: 14 }}>
          <Notice>{t("v2.addToDataset.hint", { count: sessionIds.length })}</Notice>
          {error && <Notice s="act">{error}{refused.length > 0 && reasons(refused)}</Notice>}
          <Filters
            value={mode}
            onChange={(m) => {
              setMode(m);
              setError(null);
            }}
            options={[
              { value: "new", label: t("v2.addToDataset.new") },
              { value: "existing", label: t("v2.addToDataset.existing") },
            ]}
          />
          {mode === "existing" ? (
            <label className="v3-field">
              <span>{t("v2.addToDataset.dataset")}</span>
              <select className="v3-select" value={datasetId} onChange={(e) => setDatasetId(e.target.value)}>
                <option value="">{t("v3.data.choose")}</option>
                {receivable.map((d) => (
                  <option key={d.id} value={d.id}>{`${d.name} · ${t("v2.datasets.items", { count: d.item_count })}`}</option>
                ))}
              </select>
            </label>
          ) : (
            <>
              <label className="v3-field">
                <span>{t("v2.datasets.colName")}</span>
                <input className="v3-input" value={name} maxLength={64} onChange={(e) => setName(e.target.value)} />
              </label>
              <label className="v3-field">
                <span>{t("v2.datasets.description")}</span>
                <input className="v3-input" value={description} maxLength={1000} onChange={(e) => setDescription(e.target.value)} />
              </label>
            </>
          )}
          <label style={{ display: "flex", gap: 8, alignItems: "center", color: "var(--v3-text-2)", fontSize: 13 }}>
            <input type="checkbox" checked={firstTurnOnly} onChange={(e) => setFirstTurnOnly(e.target.checked)} />
            {t("v2.addToDataset.firstTurnOnly")}
          </label>
        </div>
      )}
    </Dialog>
  );
}
