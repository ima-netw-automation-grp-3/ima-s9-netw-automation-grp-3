import { useEffect, useRef, useState } from 'react';
import { useQuery } from '@tanstack/react-query';

import { api } from './api';
import type { IfaceInfo } from './types';

export const POLL_MS = 3000;

export function useLab() {
  return useQuery({ queryKey: ['lab'], queryFn: api.lab, refetchInterval: POLL_MS });
}

/** Où trouver Grafana (onglet « Métrologie ») ; la disponibilité est revérifiée toutes les 30 s. */
export function useMonitoring() {
  return useQuery({ queryKey: ['monitoring'], queryFn: api.monitoring, refetchInterval: 30_000, staleTime: 15_000 });
}

export function useNodeStatus(name: string, poll = true) {
  return useQuery({
    queryKey: ['node', name],
    queryFn: () => api.node(name),
    refetchInterval: poll ? POLL_MS : false,
  });
}

export interface Rates {
  rx: number | null;
  tx: number | null;
}

/** Débits (bit/s) calculés entre deux échantillons successifs des compteurs d'interfaces. */
export function useRates(interfaces: Record<string, IfaceInfo> | undefined, ts: number | undefined) {
  const prev = useRef<{ ts: number; bytes: Record<string, { rx: number; tx: number }> } | null>(null);
  const [rates, setRates] = useState<Record<string, Rates>>({});

  useEffect(() => {
    if (!interfaces) return;
    const now = ts ?? Date.now() / 1000;
    const bytes: Record<string, { rx: number; tx: number }> = {};
    for (const [name, iface] of Object.entries(interfaces)) {
      bytes[name] = { rx: iface.stats?.rx_bytes ?? 0, tx: iface.stats?.tx_bytes ?? 0 };
    }
    const p = prev.current;
    if (p && now > p.ts) {
      const dt = now - p.ts;
      const next: Record<string, Rates> = {};
      for (const [name, b] of Object.entries(bytes)) {
        const old = p.bytes[name];
        next[name] = old
          ? { rx: Math.max(0, ((b.rx - old.rx) * 8) / dt), tx: Math.max(0, ((b.tx - old.tx) * 8) / dt) }
          : { rx: null, tx: null };
      }
      setRates(next);
    }
    prev.current = { ts: now, bytes };
  }, [interfaces, ts]);

  return rates;
}
