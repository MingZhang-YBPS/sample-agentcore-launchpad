import { useCallback, useEffect, useRef, useState } from "react";

import { errorMessage } from "../api";

export interface AdminLoaded<T> {
  data: T | null;
  error: string | null;
  loading: boolean;
  reload: () => void;
}

/**
 * Fetch on mount and whenever `key` changes; `reload()` refetches. The previous
 * data stays while a refetch runs (a list that follows a publish does not blink),
 * and a response that arrives after a newer request started is dropped. Same
 * contract as the V2 `useLoad`, so the V2 cards behave as before.
 */
export function useAdminLoad<T>(fetcher: () => Promise<T>, key: string): AdminLoaded<T> {
  const [state, setState] = useState<{ data: T | null; error: string | null; loading: boolean }>({
    data: null,
    error: null,
    loading: true,
  });
  const [nonce, setNonce] = useState(0);
  const fetcherRef = useRef(fetcher);
  fetcherRef.current = fetcher;
  useEffect(() => {
    let live = true;
    setState((prev) => ({ ...prev, loading: true, error: null }));
    fetcherRef
      .current()
      .then((data) => {
        if (live) setState({ data, error: null, loading: false });
      })
      .catch((err: unknown) => {
        if (live) setState((prev) => ({ ...prev, error: errorMessage(err), loading: false }));
      });
    return () => {
      live = false;
    };
  }, [key, nonce]);
  const reload = useCallback(() => setNonce((n) => n + 1), []);
  return { ...state, reload };
}
