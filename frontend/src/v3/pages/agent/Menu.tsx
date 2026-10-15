import { type ReactNode, useEffect, useId, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import { Reasoned } from "../../reason";

export interface MenuItem {
  key: string;
  label: ReactNode;
  onSelect: () => void;
  disabled?: boolean;
  /** why it is disabled (prod lock, a deploy in flight, admin only) */
  title?: string;
  danger?: boolean;
  /** draw a separator above this item */
  separated?: boolean;
}

/** A button that opens a small action menu; Esc, an outside click or a pick closes it. */
export function Menu({
  label,
  items,
  kind,
  testId,
  title,
}: {
  label: ReactNode;
  items: MenuItem[];
  kind?: "primary" | "ghost";
  testId?: string;
  /** the trigger button's tooltip */
  title?: string;
}) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  const id = useId();
  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);
  if (items.length === 0) return null;
  return (
    <div className="v3-agent-menu" ref={ref}>
      <button
        type="button"
        className={["v3-btn", kind ?? ""].join(" ").trim()}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-controls={open ? id : undefined}
        onClick={() => setOpen((v) => !v)}
        title={title}
        data-testid={testId}
      >
        {label}
      </button>
      {open && (
        <div role="menu" id={id}>
          {items.map((item) => (
            <div key={item.key} style={{ display: "contents" }}>
              {item.separated && <div className="sep" role="separator" />}
              <Reasoned disabledReason={item.title ?? t("story.reason.unavailable")}>
              <button
                type="button"
                role="menuitem"
                disabled={item.disabled}
                title={item.disabled ? undefined : item.title}
                data-danger={item.danger ? "true" : undefined}
                data-testid={testId ? `${testId}-${item.key}` : undefined}
                onClick={() => {
                  setOpen(false);
                  item.onSelect();
                }}
              >
                {item.label}
              </button>
              </Reasoned>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
