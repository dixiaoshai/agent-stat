import { useCallback, useEffect, useRef, useState } from 'react';
import { getOverview, refreshImport } from '../lib/api';
import type { Filters, ImportReport, Overview } from '../types';

interface StatsState {
  data: Overview | null;
  loading: boolean;
  refreshing: boolean;
  error: string | null;
  queryTime: string | null;
  messages: string[];
}

const initial: StatsState = {
  data: null,
  loading: true,
  refreshing: false,
  error: null,
  queryTime: null,
  messages: [],
};
const keyOf = (filters: Filters) => JSON.stringify(filters);
export function useStats(filters: Filters) {
  const [state, setState] = useState<StatsState>(initial);
  const filtersRef = useRef(filters);
  const key = keyOf(filters);
  const keyRef = useRef(key);
  const generation = useRef(0);
  const refreshGeneration = useRef(0);
  const queryController = useRef<AbortController | null>(null);
  const importController = useRef<AbortController | null>(null);
  const mounted = useRef(false);
  filtersRef.current = filters;
  keyRef.current = key;

  const load = useCallback(async () => {
    const requestFilters = { ...filtersRef.current };
    const requestKey = keyOf(requestFilters);
    const requestGeneration = ++generation.current;
    queryController.current?.abort();
    const controller = new AbortController();
    queryController.current = controller;
    setState((s) => ({ ...s, loading: true, error: null }));
    try {
      const data = await getOverview(requestFilters, controller.signal);
      if (
        !mounted.current ||
        controller.signal.aborted ||
        requestGeneration !== generation.current ||
        requestKey !== keyRef.current
      )
        return;
      setState((s) => ({
        ...s,
        data,
        loading: false,
        error: null,
        queryTime: new Date().toISOString(),
        messages: [],
      }));
    } catch (error) {
      if (!mounted.current || controller.signal.aborted || requestGeneration !== generation.current)
        return;
      setState((s) => ({
        ...s,
        loading: false,
        error: error instanceof Error ? error.message : String(error),
      }));
    }
  }, []);

  useEffect(() => {
    void load();
    return () => {
      generation.current++;
      queryController.current?.abort();
    };
  }, [key, load]);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      generation.current++;
      refreshGeneration.current++;
      queryController.current?.abort();
      importController.current?.abort();
    };
  }, []);

  const refresh = useCallback(async () => {
    const currentRefresh = ++refreshGeneration.current;
    generation.current++;
    queryController.current?.abort();
    importController.current?.abort();
    const controller = new AbortController();
    importController.current = controller;
    setState((s) => ({ ...s, refreshing: true, loading: true, error: null, messages: [] }));

    const isCurrent = () =>
      mounted.current && !controller.signal.aborted && currentRefresh === refreshGeneration.current;

    try {
      const report: ImportReport = await refreshImport(controller.signal);
      if (!isCurrent()) return;

      // A filter change aborts this read. Retry against the newest filter until
      // a response matches the current key, while preserving the import result.
      while (isCurrent()) {
        const requestFilters = { ...filtersRef.current };
        const requestKey = keyOf(requestFilters);
        const requestGeneration = ++generation.current;
        queryController.current?.abort();
        const query = new AbortController();
        queryController.current = query;

        try {
          const response = await getOverview(requestFilters, query.signal);
          if (!isCurrent()) return;
          if (
            query.signal.aborted ||
            requestGeneration !== generation.current ||
            requestKey !== keyRef.current
          )
            continue;

          const data: Overview = { ...response, last_import: report };
          setState((s) => ({
            ...s,
            data,
            loading: false,
            refreshing: false,
            error: null,
            queryTime: new Date().toISOString(),
            messages: [...report.errors, ...report.warnings],
          }));
          return;
        } catch (error) {
          if (query.signal.aborted && isCurrent()) continue;
          throw error;
        }
      }
    } catch (error) {
      if (!isCurrent()) return;
      setState((s) => ({
        ...s,
        loading: false,
        refreshing: false,
        error: error instanceof Error ? error.message : String(error),
      }));
    } finally {
      // If import/query was aborted by a replacement refresh, that newer run
      // owns the busy indicators. Never clear its state from this stale run.
      if (
        mounted.current &&
        currentRefresh === refreshGeneration.current &&
        controller.signal.aborted
      ) {
        setState((s) => ({ ...s, loading: false, refreshing: false }));
      }
    }
  }, []);

  const reload = useCallback(() => {
    void load();
  }, [load]);
  return { ...state, refresh, reload };
}
