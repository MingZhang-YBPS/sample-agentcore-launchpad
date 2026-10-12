import type { ReactNode } from "react";
import { createPortal } from "react-dom";

import { fmtScore } from "../../../v2/format";
import { V2ToastProvider } from "../../../v2/ui";
import { Chip } from "../../ui";
import { scoreSignal } from "./model";

export function ScoreChip({ value }: { value: number | null }) {
  if (value == null) return <span style={{ color: "var(--v3-text-3)" }}>—</span>;
  return <Chip s={scoreSignal(value)}>{fmtScore(value)}</Chip>;
}

/**
 * A V2 component reused as is (the insight clusters, run recommendations, the
 * add-to-dataset dialog, the CloudWatch source fields): it renders on the V3
 * theme through the hosted V2 skin, with the V2 toast channel it reports to.
 */
export function HostedV2({ children }: { children: ReactNode }) {
  return (
    <V2ToastProvider>
      <div className="v2 v3-host">{children}</div>
    </V2ToastProvider>
  );
}

/**
 * Render a dialog at the V3 shell root. A dialog opened from inside a panel would
 * otherwise sit in the panel's box: the reveal animation leaves a transform on
 * panels, which makes them the containing block of `position: fixed`.
 */
export function AtShell({ children }: { children: ReactNode }) {
  const root = typeof document === "undefined" ? null : document.querySelector(".v3[data-testid='v3-shell']");
  return root ? createPortal(children, root) : <>{children}</>;
}
