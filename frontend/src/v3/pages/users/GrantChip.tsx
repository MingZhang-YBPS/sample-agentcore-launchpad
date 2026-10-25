import { Check } from "lucide-react";
import type { ReactNode } from "react";

/** A toggle for one grant (workspace / permission): on = granted; `busy` locks it while a save runs. */
export function GrantChip({
  on,
  busy,
  title,
  onClick,
  children,
}: {
  on: boolean;
  busy?: boolean;
  title?: string;
  onClick: () => void;
  children: ReactNode;
}) {
  return (
    <button type="button" className={on ? "v3-usr-chip on" : "v3-usr-chip"} aria-pressed={on}
      disabled={busy} title={title} onClick={onClick}>
      {on && <Check size={13} aria-hidden="true" />}
      {children}
    </button>
  );
}
