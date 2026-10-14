import type { Signal } from "../../ui";

/** A check row's lamp: block → act, warn → wait; `info` rows are informational. */
export const SEVERITY_SIGNAL: Record<string, Signal> = { ok: "ok", warn: "wait", block: "act", info: "info" };
/** An entry's check status chip. */
export const STATUS_SIGNAL: Record<string, Signal> = { ok: "ok", warn: "wait", blocked: "act", unchecked: "off" };
