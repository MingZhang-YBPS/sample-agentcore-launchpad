import "./toolApproval.css";

import { Check, Hand, ShieldOff, X } from "lucide-react";
import { useTranslation } from "react-i18next";

import { type ApprovalPhase, maskedCount, maskedInputPieces, type ToolApproval } from "../../lib/tool-approval";

/**
 * A tool call the agent paused for the user's approval (SB-2 Hooks, strands-builder
 * §2.8): the tool, its input as the runtime masked it (a masked argument is drawn
 * as a redaction bar, never as its value), and Approve / Decline. Approve runs the
 * next turn with that tool approved; Decline leaves the call unrun. Once answered
 * the card keeps the verdict as a stamp. Skin-neutral like StructuredAnswer:
 * `toolApproval.css` maps it onto the V2 or V3 tokens of the page that hosts it.
 */
export function ToolApprovalCard({
  approval,
  phase,
  disabled = false,
  onApprove,
  onDecline,
}: {
  approval: ToolApproval;
  phase: ApprovalPhase;
  /** a turn is running: the buttons wait */
  disabled?: boolean;
  onApprove?: () => void;
  onDecline?: () => void;
}) {
  const { t } = useTranslation();
  const tool = approval.tool || "—";
  const masked = maskedCount(approval.input);
  const hasInput = Object.keys(approval.input).length > 0;
  const Icon = phase === "refused" ? ShieldOff : Hand;
  return (
    <figure className="lp-approval" data-phase={phase} data-testid="tool-approval-card">
      <figcaption className="lp-approval-head">
        <span className="lp-approval-lamp" aria-hidden="true" />
        <Icon size={14} aria-hidden="true" />
        <span className="lp-approval-label">{t(phase === "refused" ? "chatApproval.refusedLabel" : "chatApproval.label")}</span>
        <span className="lp-approval-state" data-testid="tool-approval-state">{t(`chatApproval.phase.${phase}`)}</span>
      </figcaption>
      <div className="lp-approval-body">
        <div className="lp-approval-tool">
          <span className="lp-approval-verb">{t("chatApproval.wants")}</span>
          <code data-testid="tool-approval-tool">{tool}</code>
        </div>
        <p className="lp-approval-note">{t(`chatApproval.note.${phase}`, { tool })}</p>
        {hasInput && (
          <div className="lp-approval-input">
            <div className="lp-approval-input-head">
              <span>{t("chatApproval.input")}</span>
              {masked > 0 && <span className="lp-approval-masked">{t("chatApproval.masked", { count: masked })}</span>}
            </div>
            <pre data-testid="tool-approval-input">
              {maskedInputPieces(approval.input).map((piece, i) =>
                piece.masked ? (
                  <span key={i} className="lp-approval-redact" title={t("chatApproval.maskedHint")}
                    aria-label={t("chatApproval.maskedHint")}>{piece.text}</span>
                ) : (
                  piece.text
                ),
              )}
            </pre>
          </div>
        )}
      </div>
      {phase === "pending" && (
        <div className="lp-approval-actions">
          <button type="button" className="lp-approval-btn go" onClick={onApprove} disabled={disabled}
            data-testid="tool-approval-approve">
            <Check size={13} aria-hidden="true" /> {t("chatApproval.approve")}
          </button>
          <button type="button" className="lp-approval-btn" onClick={onDecline} disabled={disabled}
            data-testid="tool-approval-decline">
            <X size={13} aria-hidden="true" /> {t("chatApproval.decline")}
          </button>
          <span className="lp-approval-once">{t("chatApproval.once")}</span>
        </div>
      )}
      {(phase === "approved" || phase === "declined") && (
        <div className="lp-approval-stamp" role="status">{t(`chatApproval.stamp.${phase}`, { tool })}</div>
      )}
    </figure>
  );
}
