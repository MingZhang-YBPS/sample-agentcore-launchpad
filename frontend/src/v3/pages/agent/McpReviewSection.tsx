import { type ReactNode, useState } from "react";
import { useTranslation } from "react-i18next";

import { ApiErrorDetail } from "../../../components/ApiErrorDetail";
import {
  approveMcpReview,
  changeMark,
  checkMcpDrift,
  getMcpReview,
  listMcpReviews,
  type McpGateState,
  type McpHint,
  type McpReviewDetail,
  type McpReviewList,
  type McpReviewStatus,
  rejectMcpReview,
} from "../../../lib/api/mcpReviews";
import { ago } from "../../format";
import { useLoad, useToast } from "../../hooks";
import { Btn, Chip, Notice, type Signal, Skeleton } from "../../ui";

const GATE_SIGNAL: Record<McpGateState | McpReviewStatus, Signal> = {
  none: "off",
  reviewing: "wait",
  approved: "ok",
  rejected: "act",
  superseded: "off",
};

/**
 * Tool review (projects §15.6): every tool list the server presents is read by a
 * person before agents can call it. The open review shows the full list against the
 * last approved one, with hints; approve / reject need `mcp.approve`, and the
 * deployer can do neither (the server answers 409, shown as a toast).
 */
export function McpReviewSection({ agentId, drift, onChanged }: { agentId: string; drift: boolean; onChanged: () => void }) {
  const { t } = useTranslation();
  const toast = useToast();
  const [nonce, setNonce] = useState(0);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState("");
  const list = useLoad(() => listMcpReviews(agentId), `v3-mcp-reviews:${agentId}:${nonce}`);
  const openId = list.data?.open_review_id ?? null;
  const latestId = list.data?.reviews[0]?.id ?? null;
  const shownId = openId ?? latestId;
  const detail = useLoad<McpReviewDetail | null>(
    () => (shownId ? getMcpReview(agentId, shownId) : Promise.resolve(null)),
    `v3-mcp-review:${agentId}:${shownId ?? "-"}:${nonce}`,
  );
  const refresh = () => {
    setNonce((n) => n + 1);
    onChanged();
  };

  const decide = async (approve: boolean) => {
    if (!openId) return;
    setBusy(true);
    try {
      await (approve ? approveMcpReview : rejectMcpReview)(agentId, openId, note.trim());
      toast("ok", t(approve ? "mcpReview.approved" : "mcpReview.rejected"));
      setNote("");
      refresh();
    } catch (err) {
      toast("act", t("mcpReview.decideFailed"), err);
    } finally {
      setBusy(false);
    }
  };

  const check = async () => {
    setBusy(true);
    try {
      const res = await checkMcpDrift(agentId);
      toast("ok", t(!res.checked ? "mcpReview.checkNoTarget" : res.drift ? "mcpReview.checkDrift" : "mcpReview.checkClean"));
      refresh();
    } catch (err) {
      toast("act", t("mcpReview.checkFailed"), err);
    } finally {
      setBusy(false);
    }
  };

  if (list.error) return <Notice s="act"><ApiErrorDetail error={list.failure ?? list.error} /></Notice>;
  if (!list.data) return <Skeleton rows={2} />;
  return (
    <McpReviewView
      list={list.data}
      review={detail.data}
      reviewError={detail.error ? <ApiErrorDetail error={detail.failure ?? detail.error} /> : null}
      drift={drift}
      busy={busy}
      note={note}
      onNote={setNote}
      onDecide={decide}
      onCheck={check}
    />
  );
}

/** The section's markup from loaded data (split out so it renders without fetching). */
export function McpReviewView({
  list,
  review,
  reviewError,
  drift,
  busy,
  note,
  onNote,
  onDecide,
  onCheck,
}: {
  list: McpReviewList;
  review: McpReviewDetail | null;
  reviewError: ReactNode;
  drift: boolean;
  busy: boolean;
  note: string;
  onNote: (value: string) => void;
  onDecide: (approve: boolean) => void;
  onCheck: () => void;
}) {
  const { t } = useTranslation();
  const state = list.state;
  const openId = list.open_review_id;
  const hintsByTool = new Map<string, McpHint[]>();
  for (const h of review?.hints ?? []) hintsByTool.set(h.tool, [...(hintsByTool.get(h.tool) ?? []), h]);
  const before = new Map((review?.base_tools ?? []).map((tool) => [tool.name, tool]));

  return (
    <div style={{ display: "grid", gap: 10 }} data-testid="v3-mcp-review">
      <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
        <strong>{t("mcpReview.title")}</strong>
        <Chip s={GATE_SIGNAL[state]}>{t(`mcpReview.state.${state}`)}</Chip>
        <span style={{ flex: 1 }} />
        <Btn size="sm" kind="ghost" disabled={busy} onClick={onCheck} title={t("mcpReview.checkHint")}>{t("mcpReview.check")}</Btn>
      </div>
      {drift && state === "approved" && <Notice s="wait">{t("mcpReview.driftNotice")}</Notice>}
      {state === "reviewing" && <Notice s="wait">{t("mcpReview.withheld")}</Notice>}
      {state === "rejected" && <Notice s="act">{t("mcpReview.rejectedNotice")}</Notice>}
      {state === "none" && <Notice s="info">{t("mcpReview.none")}</Notice>}
      {review && !openId && review.publish_status === "publishing" && <Notice s="wait">{t("mcpReview.publishing")}</Notice>}
      {review && !openId && review.publish_status === "failed" && (
        <Notice s="act">{t("mcpReview.publishFailed")} {review.publish_error}</Notice>
      )}
      {reviewError && <Notice s="act">{reviewError}</Notice>}
      {review && (
        <>
          <div className="v3-hint">
            {t("mcpReview.meta", {
              version: review.version ?? "—",
              source: t(`mcpReview.source.${review.source}`),
              by: review.requested_by || "—",
              ago: ago(review.created_at),
            })}
            {review.decided_by && ` · ${t("mcpReview.decidedBy", { by: review.decided_by, status: t(`mcpReview.state.${review.status}`) })}`}
          </div>
          <div className="v3-hint">
            {t("mcpReview.diff", {
              added: review.diff.added?.length ?? 0,
              removed: review.diff.removed?.length ?? 0,
              described: review.diff.description_changed?.length ?? 0,
              schema: review.diff.schema_changed?.length ?? 0,
              hints: review.hints.length,
            })}
          </div>
          <table className="v3-table">
            <thead>
              <tr>
                <th style={{ width: 24 }} />
                <th>{t("mcpReview.tool")}</th>
                <th>{t("mcpReview.description")}</th>
                <th>{t("mcpReview.hints")}</th>
              </tr>
            </thead>
            <tbody>
              {review.tools.map((tool) => {
                const mark = changeMark(review.diff, tool.name);
                const old = before.get(tool.name);
                return (
                  <tr key={tool.name}>
                    <td className="mono">{mark}</td>
                    <td className="mono">{tool.name}</td>
                    <td style={{ color: "var(--v3-text-2)", whiteSpace: "pre-wrap" }}>
                      {tool.description || "—"}
                      {old && review.diff.description_changed?.includes(tool.name) && (
                        <div className="v3-hint" style={{ textDecoration: "line-through" }}>{old.description || "—"}</div>
                      )}
                      {review.diff.schema_changed?.includes(tool.name) && <div className="v3-hint">{t("mcpReview.schemaChanged")}</div>}
                      <details>
                        <summary className="v3-hint">{t("mcpReview.schema")}</summary>
                        <pre className="mono" style={{ fontSize: 11, whiteSpace: "pre-wrap" }}>{JSON.stringify(tool.input_schema, null, 2)}</pre>
                      </details>
                    </td>
                    <td>
                      {(hintsByTool.get(tool.name) ?? []).map((h, i) => (
                        <div key={i}>
                          <Chip s="wait" title={h.detail || undefined}>{t(`mcpReview.hint.${h.kind}`)}</Chip>
                        </div>
                      ))}
                    </td>
                  </tr>
                );
              })}
              {(review.diff.removed ?? []).map((name) => (
                <tr key={`removed-${name}`}>
                  <td className="mono">−</td>
                  <td className="mono" style={{ textDecoration: "line-through" }}>{name}</td>
                  <td className="v3-hint">{t("mcpReview.removed")}</td>
                  <td />
                </tr>
              ))}
            </tbody>
          </table>
          {openId === review.id && (
            <div style={{ display: "grid", gap: 8 }}>
              <label className="v3-hint" htmlFor={`mcp-review-note-${review.id}`}>{t("mcpReview.note")}</label>
              <textarea
                id={`mcp-review-note-${review.id}`}
                className="v3-input"
                rows={2}
                maxLength={2000}
                value={note}
                onChange={(e) => onNote(e.target.value)}
              />
              <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
                <Btn kind="danger" size="sm" disabled={busy} onClick={() => onDecide(false)}>{t("mcpReview.reject")}</Btn>
                <Btn kind="primary" size="sm" disabled={busy} onClick={() => onDecide(true)}>{t("mcpReview.approve")}</Btn>
                <span className="v3-hint">{t("mcpReview.sod")}</span>
              </div>
            </div>
          )}
        </>
      )}
      {list.reviews.length > 1 && (
        <details>
          <summary className="v3-hint">{t("mcpReview.history", { count: list.reviews.length })}</summary>
          <table className="v3-table">
            <tbody>
              {list.reviews.map((r) => (
                <tr key={r.id}>
                  <td><Chip s={GATE_SIGNAL[r.status]}>{t(`mcpReview.state.${r.status}`)}</Chip></td>
                  <td>{t(`mcpReview.source.${r.source}`)}</td>
                  <td className="mono">{r.tool_count}</td>
                  <td>{r.requested_by || "—"}</td>
                  <td>{r.decided_by ?? "—"}</td>
                  <td className="v3-hint">{ago(r.created_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </details>
      )}
    </div>
  );
}
