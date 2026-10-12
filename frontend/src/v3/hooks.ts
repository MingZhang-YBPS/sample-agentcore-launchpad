// Console V3 hooks and the toast channel (components live in ui.tsx).
import { createContext, useCallback, useContext, useEffect, useRef, useState } from "react";

import { errorMessage } from "../lib/api";

export interface Loaded<T> {
  data: T | null;
  error: string | null;
  loading: boolean;
  reload: () => void;
}

/** Fetch on mount and when `key` changes; a stale response never overwrites a fresher one. */
export function useLoad<T>(fetcher: () => Promise<T>, key: string): Loaded<T> {
  const [state, setState] = useState<{ key: string; data: T | null; error: string | null; loading: boolean }>({
    key,
    data: null,
    error: null,
    loading: true,
  });
  const [nonce, setNonce] = useState(0);
  const ref = useRef(fetcher);
  ref.current = fetcher;
  useEffect(() => {
    let live = true;
    setState((prev) => ({ key, data: prev.key === key ? prev.data : null, loading: true, error: null }));
    ref
      .current()
      .then((data) => live && setState({ key, data, error: null, loading: false }))
      .catch((err: unknown) => live && setState((prev) => ({ ...prev, error: errorMessage(err), loading: false })));
    return () => {
      live = false;
    };
  }, [key, nonce]);
  const reload = useCallback(() => setNonce((n) => n + 1), []);
  // Do not paint the previous workspace/resource while the new effect starts.
  return state.key === key ? { ...state, reload } : { data: null, error: null, loading: true, reload };
}


export type ToastFn = (s: "ok" | "act", text: string) => void;
export const ToastContext = createContext<ToastFn>(() => undefined);

export function useToast(): ToastFn {
  return useContext(ToastContext);
}
