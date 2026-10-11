import { useCallback, useEffect, useState } from 'react';
import { getJson } from '../api/client';

export function useApi<T>(path: string) {
  const [revision, setRevision] = useState(0);
  const [state, setState] = useState<{
    path: string; data: T | null; loading: boolean; error: string | null; fetchedAt: string | null;
  }>({ path, data: null, loading: true, error: null, fetchedAt: null });
  const reload = useCallback(() => setRevision(value => value + 1), []);

  useEffect(() => {
    const controller = new AbortController();
    setState({ path, data: null, loading: true, error: null, fetchedAt: null });
    getJson<T>(path, controller.signal).then(
      data => {
        if (!controller.signal.aborted) {
          setState({ path, data, loading: false, error: null, fetchedAt: new Date().toISOString() });
        }
      },
      error => {
        if (!controller.signal.aborted) {
          setState({ path, data: null, loading: false, error: error.message, fetchedAt: null });
        }
      },
    );
    // Route/filter changes and manual refresh cannot overwrite newer results.
    return () => controller.abort();
  }, [path, revision]);

  return state.path === path ? { ...state, reload }
    : { path, data: null, loading: true, error: null, fetchedAt: null, reload };
}
