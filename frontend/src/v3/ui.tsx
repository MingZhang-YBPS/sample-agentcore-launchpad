// Console V3 primitives. Styling lives in v3.css under `.v3`.
//
// The vocabulary is deliberately small: a Signal is one of ok / wait / act / info /
// off, and every status in the console — an agent, a gate, a release, a run — is
// mapped onto it once, here, so the same colour always means the same thing.
import { AlertTriangle, CheckCircle2, Inbox, Info, XCircle } from "lucide-react";
import { type ReactNode, useCallback, useEffect, useId, useMemo, useRef, useState } from "react";

import { ToastContext, type ToastFn } from "./hooks";

export type Signal = "ok" | "wait" | "act" | "info" | "off";

export function Lamp({ s, live = false, title }: { s: Signal; live?: boolean; title?: string }) {
  return <span className="v3-lamp" data-s={s} data-live={live} title={title} aria-hidden={!title} />;
}

export function Chip({ s, children, title }: { s?: Signal; children: ReactNode; title?: string }) {
  return (
    <span className="v3-chip" data-s={s} title={title}>
      {children}
    </span>
  );
}

export function Panel({
  title,
  end,
  signal,
  flush,
  children,
  className,
}: {
  title?: ReactNode;
  end?: ReactNode;
  signal?: Signal;
  flush?: boolean;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section
      className={["v3-panel", flush ? "flush" : "", className ?? ""].join(" ").trim()}
      data-signal={signal}
    >
      {(title || end) && (
        <div className="v3-panel-head">
          {title}
          {end && <span className="end">{end}</span>}
        </div>
      )}
      {children}
    </section>
  );
}

export function Stat({
  label,
  value,
  unit,
  foot,
  signal,
}: {
  label: ReactNode;
  value: ReactNode;
  unit?: string;
  foot?: ReactNode;
  signal?: Signal;
}) {
  return (
    <div className="v3-stat" data-signal={signal}>
      <div className="label">{label}</div>
      <div className="value">
        {value}
        {unit && <small>{unit}</small>}
      </div>
      {foot && <div className="foot">{foot}</div>}
    </div>
  );
}

export function Btn({
  children,
  kind,
  size,
  disabled,
  title,
  onClick,
  type = "button",
}: {
  children: ReactNode;
  kind?: "primary" | "danger" | "ghost";
  size?: "sm";
  disabled?: boolean;
  title?: string;
  onClick?: () => void;
  type?: "button" | "submit";
}) {
  return (
    <button
      type={type}
      className={["v3-btn", kind ?? "", size ?? ""].join(" ").trim()}
      disabled={disabled}
      title={title}
      onClick={onClick}
    >
      {children}
    </button>
  );
}

export function PageHead({
  eyebrow,
  title,
  sub,
  end,
}: {
  eyebrow: string;
  title: ReactNode;
  sub?: ReactNode;
  end?: ReactNode;
}) {
  return (
    <header className="v3-head">
      <div>
        <div className="v3-eyebrow">{eyebrow}</div>
        <h1 className="v3-title">{title}</h1>
        {sub && <p className="v3-sub">{sub}</p>}
      </div>
      {end && <div className="v3-head-end">{end}</div>}
    </header>
  );
}

export function Empty({ title, children }: { title: ReactNode; children?: ReactNode }) {
  return (
    <div className="v3-empty">
      <Inbox size={30} aria-hidden="true" />
      <strong>{title}</strong>
      {children}
    </div>
  );
}

export function Skeleton({ rows = 4 }: { rows?: number }) {
  return (
    <div style={{ display: "grid", gap: 12, padding: 4 }} aria-busy="true">
      {Array.from({ length: rows }, (_, i) => (
        <div key={i} className="v3-skel" style={{ width: `${90 - i * 12}%` }} />
      ))}
    </div>
  );
}

export function Notice({ s = "info", children }: { s?: Signal; children: ReactNode }) {
  const Icon = s === "act" ? XCircle : s === "wait" ? AlertTriangle : s === "ok" ? CheckCircle2 : Info;
  return (
    <div className="v3-alert" data-s={s} role={s === "act" ? "alert" : "status"}>
      <Icon size={15} aria-hidden="true" />
      <div>{children}</div>
    </div>
  );
}

/** A trend as a filled line. Nothing to draw (< 2 points) renders nothing. */
export function Spark({ values, s = "ok" }: { values: number[]; s?: "ok" | "act" }) {
  const id = useId();
  if (values.length < 2) return null;
  const W = 200;
  const H = 44;
  const max = Math.max(...values);
  const min = Math.min(...values);
  const span = max - min || 1;
  const pts = values.map((v, i) => [(i / (values.length - 1)) * W, H - 4 - ((v - min) / span) * (H - 8)]);
  const line = pts.map(([x, y]) => `${x.toFixed(1)},${y.toFixed(1)}`).join(" ");
  const color = s === "act" ? "var(--v3-act)" : "var(--v3-ok)";
  return (
    <svg className="v3-spark" viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" aria-hidden="true">
      <defs>
        <linearGradient id={id} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor={color} stopOpacity="0.28" />
          <stop offset="100%" stopColor={color} stopOpacity="0" />
        </linearGradient>
      </defs>
      <polygon points={`0,${H} ${line} ${W},${H}`} fill={`url(#${id})`} />
      <polyline points={line} className={`line${s === "act" ? " act" : ""}`} />
    </svg>
  );
}

/* ── toasts ────────────────────────────────────────────────────────────── */


export function ToastProvider({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<{ id: number; s: "ok" | "act"; text: string }[]>([]);
  const seq = useRef(0);
  const push = useCallback<ToastFn>((s, text) => {
    const id = ++seq.current;
    setItems((prev) => [...prev, { id, s, text }]);
    window.setTimeout(() => setItems((prev) => prev.filter((t) => t.id !== id)), 4200);
  }, []);
  const value = useMemo(() => push, [push]);
  return (
    <ToastContext.Provider value={value}>
      {children}
      <div className="v3-toasts" aria-live="polite">
        {items.map((t) => (
          <div key={t.id} className="v3-toast">
            <Lamp s={t.s} />
            <span>{t.text}</span>
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  );
}


/* ── confirm ───────────────────────────────────────────────────────────── */

/** Esc closes an overlay wherever focus is, unless it is busy. */
function useEscape(onClose: () => void, enabled = true) {
  const ref = useRef(onClose);
  ref.current = onClose;
  useEffect(() => {
    if (!enabled) return;
    const onKey = (e: globalThis.KeyboardEvent) => e.key === "Escape" && ref.current();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [enabled]);
}

/**
 * The one confirmation dialog: says what will happen, names the thing, and puts
 * the destructive choice on the right. Esc and the mask cancel; nothing confirms
 * by default.
 */
export function Confirm({
  title,
  children,
  confirmLabel,
  cancelLabel,
  danger,
  busy,
  onConfirm,
  onCancel,
}: {
  title: ReactNode;
  children: ReactNode;
  confirmLabel: string;
  cancelLabel: string;
  danger?: boolean;
  busy?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  useEscape(onCancel, !busy);
  return (
    <div className="v3-modal-mask" onMouseDown={(e) => e.target === e.currentTarget && !busy && onCancel()}>
      <div className="v3-modal" role="alertdialog" aria-modal="true" aria-label={typeof title === "string" ? title : undefined}>
        <h2>{title}</h2>
        <div className="body">{children}</div>
        <div className="foot">
          <Btn kind="ghost" onClick={onCancel} disabled={busy}>
            {cancelLabel}
          </Btn>
          <Btn kind={danger ? "danger" : "primary"} onClick={onConfirm} disabled={busy}>
            {confirmLabel}
          </Btn>
        </div>
      </div>
    </div>
  );
}

/* ── track ─────────────────────────────────────────────────────────────── */

export interface TrackNode {
  key: string;
  label: ReactNode;
  detail?: ReactNode;
  s: Signal;
  /** the node the subject is at now: drawn larger and pulsing when it is fine */
  here?: boolean;
}

/**
 * A lifecycle drawn as one line of lamps. Where the line is lit up to is where
 * the subject is; the colour of that node says whether that is fine.
 */
export function Track({ nodes }: { nodes: TrackNode[] }) {
  return (
    <ol className="v3-track" style={{ gridTemplateColumns: `repeat(${nodes.length}, minmax(0, 1fr))` }}>
      {nodes.map((n, i) => (
        <li key={n.key} data-s={n.s} data-here={n.here ? "true" : undefined}>
          {i < nodes.length - 1 && <span className="rail" data-lit={n.s === "ok" ? "true" : undefined} aria-hidden="true" />}
          <Lamp s={n.s} live={n.here && n.s !== "off"} />
          <div className="label">{n.label}</div>
          {n.detail != null && <div className="detail">{n.detail}</div>}
        </li>
      ))}
    </ol>
  );
}

/* ── filter chips ──────────────────────────────────────────────────────── */

export function Filters<T extends string>({
  value,
  options,
  onChange,
}: {
  value: T;
  options: { value: T; label: ReactNode; count?: number; s?: Signal }[];
  onChange: (next: T) => void;
}) {
  return (
    <div className="v3-filters" role="group">
      {options.map((o) => (
        <button
          key={o.value}
          type="button"
          className={`v3-btn sm${value === o.value ? "" : " ghost"}`}
          aria-pressed={value === o.value}
          onClick={() => onChange(o.value)}
        >
          {o.s && <Lamp s={o.s} />}
          {o.label}
          {o.count != null && <span className="mono" style={{ color: "var(--v3-text-3)" }}>{o.count}</span>}
        </button>
      ))}
    </div>
  );
}

/** A form in a dialog (add a source, edit a description). Esc and the mask close. */
export function Dialog({
  title,
  children,
  foot,
  wide,
  onClose,
}: {
  title: ReactNode;
  children: ReactNode;
  foot: ReactNode;
  wide?: boolean;
  onClose: () => void;
}) {
  useEscape(onClose);
  return (
    <div className="v3-modal-mask" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className={wide ? "v3-modal wide" : "v3-modal"} role="dialog" aria-modal="true" aria-label={typeof title === "string" ? title : undefined}>
        <h2>{title}</h2>
        <div className="body">{children}</div>
        <div className="foot">{foot}</div>
      </div>
    </div>
  );
}
