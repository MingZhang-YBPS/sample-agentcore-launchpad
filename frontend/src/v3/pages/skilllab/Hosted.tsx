import type { ReactNode } from "react";

import { V2ToastProvider } from "../../../v2/ui";

/**
 * A V2 Skill Lab piece rendered in place on the V3 theme (`v3/host.css` re-points
 * the `--v2-*` tokens on `.v2.v3-host`). Used for the parts V3 reuses as they are
 * — result tables, the artifact browser — and for the editors and wizards, which
 * keep working with the same `?tab=&view=` params on `/v3/skill-lab`.
 */
export function Hosted({ children }: { children: ReactNode }) {
  return (
    <V2ToastProvider>
      <div className="v2 v3-host">{children}</div>
    </V2ToastProvider>
  );
}
