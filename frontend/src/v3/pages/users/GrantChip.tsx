import { Check } from "lucide-react";
import type { ReactNode } from "react";

/** A toggle for one grant (workspace / permission): on = granted. */
export function GrantChip({
  on,
  disabled,
  title,
  onClick,
  children,
}: {
  on: boolean;
  disabled?: boolean;
  title?: string;
  onClick: () => void;
  children: ReactNode;
}) {
  return (
    <button type="button" className={on ? "v3-usr-chip on" : "v3-usr-chip"} aria-pressed={on}
      disabled={disabled} title={title} onClick={onClick}>
      {on && <Check size={13} aria-hidden="true" />}
      {children}
    </button>
  );
}
