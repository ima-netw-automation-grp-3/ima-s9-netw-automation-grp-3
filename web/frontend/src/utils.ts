import { createElement, type ReactNode } from 'react';
import { notifications } from '@mantine/notifications';

import { ApiError } from './api';
import type { DashboardKey, GitOpsState, MonitoringInfo, Phase, RunnerState, SaveResult } from './types';

/**
 * États d'un nœud. warning : en service mais dégradé (bootstrap non terminé) ; error : services SONiC
 * arrêtés ou collecte impossible ; down : conteneur arrêté. Mêmes couleurs que les mappings Grafana.
 */
export const PHASES: Record<Phase, { color: string; label: string }> = {
  ready: { color: 'green', label: 'Prêt' },
  booting: { color: 'yellow', label: 'Démarrage' },
  bootstrap: { color: 'blue', label: 'Bootstrap' },
  warning: { color: 'orange', label: 'Dégradé' },
  error: { color: 'red', label: 'En échec' },
  down: { color: 'gray', label: 'Arrêté' },
};

export const RUNNER_STATES: Record<string, { color: string; label: string; busy?: boolean }> = {
  'loading-image': { color: 'blue', label: "Chargement de l'image SONiC", busy: true },
  starting: { color: 'blue', label: 'Démarrage du lab', busy: true },
  deploying: { color: 'blue', label: 'Déploiement en cours', busy: true },
  redeploying: { color: 'blue', label: 'Redéploiement demandé', busy: true },
  stopping: { color: 'orange', label: 'Arrêt du lab', busy: true },
  destroying: { color: 'orange', label: 'Destruction du lab', busy: true },
  deployed: { color: 'green', label: 'Lab déployé' },
  failed: { color: 'red', label: 'Échec du déploiement' },
  stopped: { color: 'gray', label: 'Lab arrêté' },
  absent: { color: 'gray', label: 'Service clab absent' },
  unknown: { color: 'gray', label: 'État inconnu' },
};

export function runnerState(state: string) {
  return RUNNER_STATES[state] ?? RUNNER_STATES.unknown;
}

/** État à afficher : l'action lancée depuis l'interface prime sur l'état écrit par le runner. */
export function runnerDisplayState(runner: RunnerState): string {
  if (runner.action === 'stop') return runner.state === 'destroying' ? 'destroying' : 'stopping';
  if (runner.action === 'redeploy') return 'redeploying';
  return runner.state;
}

/** Le service clab tourne : le lab est déployé, en cours de déploiement ou en échec (pas arrêté). */
export function runnerRunning(runner: RunnerState): boolean {
  return runner.container_state === 'running' || runner.container_state === 'restarting';
}

/** Couleur d'état (noms des mappings Grafana) → variable CSS du thème, ajustée au schéma clair ou sombre. */
const STATE_VARS: Record<string, string> = {
  green: 'var(--state-ok)',
  yellow: 'var(--state-busy)',
  blue: 'var(--state-info)',
  orange: 'var(--state-warn)',
  red: 'var(--state-err)',
  gray: 'var(--state-off)',
};

export function stateColor(name?: string | null): string {
  return STATE_VARS[name ?? 'gray'] ?? STATE_VARS.gray;
}

/** « 1 session établie », « 3 sessions établies » : accord selon le nombre (0 et 1 au singulier). */
export function plural(n: number, one: string, many: string): string {
  return `${n} ${Math.abs(n) >= 2 ? many : one}`;
}

export function formatBytes(value?: number | null): string {
  if (value === undefined || value === null) return '—';
  const units = ['o', 'Kio', 'Mio', 'Gio', 'Tio']; // puissances de 1024, comme les KiB/MiB de Grafana
  let v = value;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i += 1;
  }
  return `${v.toFixed(i === 0 ? 0 : 1)} ${units[i]}`;
}

/** Compteurs d'une interface : « paquets (octets) » par sens, « — » pour une valeur non relevée. */
export function formatCounters(stats?: { rx_packets?: number; tx_packets?: number; rx_bytes?: number; tx_bytes?: number } | null): string {
  if (!stats || [stats.rx_packets, stats.tx_packets, stats.rx_bytes, stats.tx_bytes].every((v) => v === undefined || v === null)) return '—';
  const pkt = (v?: number) => (v === undefined || v === null ? '—' : `${v} pkt`);
  return `${pkt(stats.rx_packets)} (${formatBytes(stats.rx_bytes)}) / ${pkt(stats.tx_packets)} (${formatBytes(stats.tx_bytes)})`;
}

export function formatRate(bitsPerSecond?: number | null): string {
  if (bitsPerSecond === undefined || bitsPerSecond === null || Number.isNaN(bitsPerSecond)) return '—';
  const units = ['bit/s', 'kbit/s', 'Mbit/s', 'Gbit/s'];
  let v = bitsPerSecond;
  let i = 0;
  while (v >= 1000 && i < units.length - 1) {
    v /= 1000;
    i += 1;
  }
  return `${v.toFixed(i === 0 ? 0 : 1)} ${units[i]}`;
}

/** Vitesse d'un port SONiC (en Mbit/s dans CONFIG_DB) : « 100G ». */
export function formatSpeed(speed?: string | null): string {
  return speed && /^\d+$/.test(speed) ? `${Number(speed) / 1000}G` : (speed ?? '—');
}

export function formatDuration(seconds?: number | null): string {
  if (seconds === undefined || seconds === null || seconds < 0) return '—';
  const s = Math.floor(seconds);
  const d = Math.floor(s / 86400);
  const h = Math.floor((s % 86400) / 3600);
  const m = Math.floor((s % 3600) / 60);
  if (d) return `${d} j ${h} h`;
  if (h) return `${h} h ${m} min`;
  if (m) return `${m} min ${s % 60} s`;
  return `${s} s`;
}

export function uptimeSince(startedAt?: number | null): string {
  if (!startedAt) return '—';
  return formatDuration(Date.now() / 1000 - startedAt);
}

export function formatDate(iso?: string): string {
  if (!iso) return '—';
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString('fr-FR');
}

export function errorMessage(err: unknown): string {
  if (err instanceof ApiError) {
    const detail = typeof err.detail === 'string' && err.detail ? `\n${err.detail}` : '';
    return `${err.message}${detail}`;
  }
  if (err instanceof Error) return err.message;
  return String(err);
}

export function notifyError(err: unknown, title = 'Erreur') {
  notifications.show({ color: 'red', title, message: errorMessage(err), autoClose: 8000 });
}

export function notifySuccess(message: ReactNode, title = 'OK') {
  notifications.show({ color: 'green', title, message, autoClose: 4000 });
}

/** Notification d'une sauvegarde running → startup : fichiers locaux, ou merge request (GitOps). */
export function notifySave(r: SaveResult, title = 'Sauvegarde') {
  if (!r.gitops) {
    notifySuccess(`Conf de démarrage mise à jour : ${(r.paths ?? []).join(', ') || 'lab/configs/'}`, title);
    return;
  }
  if (!r.changed || !r.merge_request) {
    notifySuccess(r.message ?? 'Conf de démarrage déjà à jour dans Git', title);
    return;
  }
  const mr = r.merge_request;
  notifications.show({
    color: 'green',
    title,
    autoClose: 12000,
    message: createElement(
      'span',
      null,
      'Merge request ',
      createElement('a', { href: mr.web_url, target: '_blank', rel: 'noreferrer' }, `!${mr.iid}`),
      " créée dans le projet de l'infra : elle deviendra la conf de démarrage une fois fusionnée.",
    ),
  });
}

/** Phrase des confirmations de sauvegarde, selon que l'infra est en GitOps ou non. */
export function saveTarget(gitops: GitOpsState | undefined, what: string): string {
  if (gitops?.enabled) {
    return `${what} est proposée dans une merge request du projet ${gitops.project ?? "de l'infra"} : elle deviendra la conf de démarrage une fois fusionnée (et déployée par l'agent GitOps).`;
  }
  return `${what} remplace la conf de démarrage dans lab/configs/. Elle sera rechargée au prochain déploiement.`;
}

export function downloadText(filename: string, content: string, mime = 'text/plain') {
  const url = URL.createObjectURL(new Blob([content], { type: `${mime};charset=utf-8` }));
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

export function openTerminalWindow(node: string, shell?: string) {
  const url = `/terminal/${encodeURIComponent(node)}${shell ? `?shell=${encodeURIComponent(shell)}` : ''}`;
  window.open(url, `term-${node}-${shell ?? ''}`, 'popup,width=1050,height=680');
}

/** Retire le préfixe de longueur d'une adresse CIDR. */
export const bareIp = (cidr: string) => cidr.split('/')[0];

/**
 * URL d'un tableau de bord Grafana vue par le navigateur : GRAFANA_URL si elle est configurée, sinon le
 * même hôte que l'interface sur le port publié de Grafana. kiosk : sans les menus de Grafana (iframe).
 */
export function grafanaUrl(
  info: MonitoringInfo | undefined,
  dashboard: DashboardKey,
  params: Record<string, string | undefined> = {},
  kiosk = false,
): string | undefined {
  if (!info) return undefined;
  const base = info.grafana.url ?? `${window.location.protocol}//${window.location.hostname}:${info.grafana.port}`;
  const query = new URLSearchParams({ orgId: '1' });
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined) query.set(key, value);
  }
  return `${base}/d/${info.dashboards[dashboard]}/?${query.toString()}${kiosk ? '&kiosk' : ''}`;
}
