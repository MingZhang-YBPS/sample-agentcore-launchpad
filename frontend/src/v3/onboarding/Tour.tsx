import { ArrowLeft, ArrowRight, X } from "lucide-react";
import { type CSSProperties, useCallback, useEffect, useLayoutEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";

import { Lamp, type Signal } from "../ui";
import { markTourDone } from "./state";

interface Stop {
  key: "cmdk" | "launch" | "queue" | "signals" | "modules" | "help";
  /** what to light up; a stop whose target is not on screen is skipped */
  target: string;
}

const STOPS: Stop[] = [
  { key: "cmdk", target: '[data-tour="cmdk"]' },
  { key: "launch", target: '[data-tour="launch"]' },
  { key: "queue", target: '[data-tour="queue"]' },
  { key: "signals", target: '[data-tour="stats"]' },
  { key: "modules", target: '[data-tour="modules"]' },
  { key: "help", target: '[data-tour="help"]' },
];

const LEGEND: Signal[] = ["ok", "wait", "act", "info", "off"];
const PAD = 8;

function rectOf(selector: string): DOMRect | null {
  const el = document.querySelector(selector);
  if (!el) return null;
  const r = el.getBoundingClientRect();
  return r.width > 0 && r.height > 0 ? r : null;
}

/**
 * The first-visit tour: one lit area at a time over a dimmed console, with a
 * short card beside it. Keyboard-first (→ / Enter next, ← back, Esc to leave),
 * skippable at any point, never shown again on its own once finished or skipped
 * — the help menu and ⌘K reopen it.
 */
export function Tour({ onClose }: { onClose: () => void }) {
  const { t } = useTranslation();
  // only the stops whose target exists right now (a dismissed launch sequence, a
  // member without the queue): computed once when the tour opens
  const stops = useMemo(() => STOPS.filter((s) => document.querySelector(s.target)), []);
  const [i, setI] = useState(0);
  const [rect, setRect] = useState<DOMRect | null>(null);
  const stop = stops[i];

  const finish = useCallback(() => {
    markTourDone();
    onClose();
  }, [onClose]);

  useLayoutEffect(() => {
    if (!stop) return;
    const el = document.querySelector(stop.target);
    el?.scrollIntoView({ block: "nearest", behavior: "smooth" });
    const measure = () => setRect(rectOf(stop.target));
    measure();
    // the smooth scroll and the page's own reveal move the target for a moment
    const settle = window.setTimeout(measure, 380);
    window.addEventListener("resize", measure);
    window.addEventListener("scroll", measure, true);
    return () => {
      window.clearTimeout(settle);
      window.removeEventListener("resize", measure);
      window.removeEventListener("scroll", measure, true);
    };
  }, [stop]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") finish();
      else if (e.key === "ArrowRight" || e.key === "Enter") setI((n) => (n + 1 < stops.length ? n + 1 : n));
      else if (e.key === "ArrowLeft") setI((n) => Math.max(0, n - 1));
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [finish, stops.length]);

  if (!stop) return null;
  const last = i === stops.length - 1;

  // the card sits below the lit area when there is room, otherwise above, and to
  // its right for the rail (a tall, narrow target)
  const vw = window.innerWidth;
  const vh = window.innerHeight;
  const W = 360;
  let style: CSSProperties = { left: vw / 2 - W / 2, top: vh / 2 - 100 };
  if (rect) {
    const tall = rect.height > vh * 0.5 && rect.width < 320;
    if (tall) style = { left: Math.min(rect.right + 18, vw - W - 16), top: Math.max(16, Math.min(rect.top + 40, vh - 300)) };
    else if (vh - rect.bottom > 260) style = { left: Math.max(16, Math.min(rect.left, vw - W - 16)), top: rect.bottom + 14 };
    else style = { left: Math.max(16, Math.min(rect.left, vw - W - 16)), top: Math.max(16, rect.top - 250) };
  }

  return (
    <div className="v3-tour" role="dialog" aria-modal="true" aria-label={t("v3.onboard.tour.label")}>
      {rect ? (
        <div className="v3-tour-spot" aria-hidden="true"
          style={{ left: rect.left - PAD, top: rect.top - PAD, width: rect.width + PAD * 2, height: rect.height + PAD * 2 }} />
      ) : (
        <div className="v3-tour-dim" aria-hidden="true" />
      )}
      <div className="v3-tour-card" style={{ ...style, width: W }} key={stop.key}>
        <div className="head">
          <span className="mono">{String(i + 1).padStart(2, "0")} / {String(stops.length).padStart(2, "0")}</span>
          <button type="button" className="v3-btn ghost sm" onClick={finish} aria-label={t("v3.onboard.tour.skip")}><X size={14} /></button>
        </div>
        <h3>{t(`v3.onboard.tour.${stop.key}Title`)}</h3>
        <p>{t(`v3.onboard.tour.${stop.key}Body`)}</p>
        {stop.key === "signals" && (
          <ul className="legend">
            {LEGEND.map((s) => (
              <li key={s}><Lamp s={s} /> <b>{t(`v3.onboard.signal.${s}`)}</b> <span>{t(`v3.onboard.signal.${s}Sub`)}</span></li>
            ))}
          </ul>
        )}
        <div className="foot">
          <button type="button" className="v3-btn ghost sm" onClick={finish}>{t("v3.onboard.tour.skip")}</button>
          <span style={{ flex: 1 }} />
          {i > 0 && <button type="button" className="v3-btn sm" onClick={() => setI(i - 1)}><ArrowLeft size={13} /> {t("v3.onboard.tour.back")}</button>}
          {last ? (
            <button type="button" className="v3-btn sm primary" onClick={finish} autoFocus>{t("v3.onboard.tour.done")}</button>
          ) : (
            <button type="button" className="v3-btn sm primary" onClick={() => setI(i + 1)} autoFocus>{t("v3.onboard.tour.next")} <ArrowRight size={13} /></button>
          )}
        </div>
      </div>
    </div>
  );
}
