import { Square } from "lucide-react";
import { useTranslation } from "react-i18next";

import { activityLabel, seconds, turnActivity, turnElapsedMs, type TurnStatus } from "../../lib/chat";
import "./turnStatus.css";

/**
 * The working-status line under a running turn — what the agent is doing, the
 * turn's elapsed time and a stop button — for the V2 chat and the try-chat, whose
 * threads have no placeholder bubble of their own. While the answer streams it
 * keeps only the timer and the stop button.
 */
export function TurnStatusLine({
  status,
  now,
  onStop,
  testId = "turn-status",
}: {
  status: TurnStatus;
  now: number;
  onStop?: () => void;
  testId?: string;
}) {
  const { t } = useTranslation();
  const activity = turnActivity(status, now);
  return (
    <div
      className="lp-turn-status"
      data-kind={activity?.kind ?? "streaming"}
      data-testid={testId}
      role="status"
      aria-live="polite"
    >
      <span className="lp-turn-dot" aria-hidden="true" />
      {activity && <span className="lp-turn-label">{activityLabel(t, activity)}</span>}
      <span className="lp-turn-time">
        {t("v3.chat.status.elapsed", { s: seconds(turnElapsedMs(status, now)) })}
      </span>
      {onStop && (
        <button type="button" className="lp-turn-stop" onClick={onStop} data-testid={`${testId}-stop`}>
          <Square size={11} aria-hidden="true" /> {t("v3.chat.status.stop")}
        </button>
      )}
    </div>
  );
}
