// Console V3 hooks and the toast channel (components live in ui.tsx).
import { createContext, useCallback, useContext, useEffect, useRef, useState } from "react";

import { errorMessage } from "../lib/api";

export interface Loaded<T> {
  data: T | null;
  error: string | null;
  /** What the fetcher rejected with — hand it to `<ApiErrorDetail>` for the AWS details (projects §19). */
  failure: unknown;
  loading: boolean;
  reload: () => void;
}

/** Fetch on mount and when `key` changes; a stale response never overwrites a fresher one. */
export function useLoad<T>(fetcher: () => Promise<T>, key: string): Loaded<T> {
  const [state, setState] = useState<Omit<Loaded<T>, "reload"> & { key: string }>({
    key,
    data: null,
    error: null,
    failure: null,
    loading: true,
  });
  const [nonce, setNonce] = useState(0);
  const ref = useRef(fetcher);
  ref.current = fetcher;
  useEffect(() => {
    let live = true;
    setState((prev) => ({ key, data: prev.key === key ? prev.data : null, loading: true, error: null, failure: null }));
    ref
      .current()
      .then((data) => live && setState({ key, data, error: null, failure: null, loading: false }))
      .catch((err: unknown) => live && setState((prev) => ({ ...prev, error: errorMessage(err), failure: err, loading: false })));
    return () => {
      live = false;
    };
  }, [key, nonce]);
  const reload = useCallback(() => setNonce((n) => n + 1), []);
  // Do not paint the previous workspace/resource while the new effect starts.
  return state.key === key ? { ...state, reload } : { data: null, error: null, failure: null, loading: true, reload };
}


/**
 * `err` (an `act` toast's caught error) adds its request id and AWS details; such a
 * toast stays until dismissed so the id can be copied (projects §19).
 */
export type ToastFn = (s: "ok" | "act", text: string, err?: unknown) => void;
export const ToastContext = createContext<ToastFn>(() => undefined);

export function useToast(): ToastFn {
  return useContext(ToastContext);
}
