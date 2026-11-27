import { Alert, Button, Code, Group, Loader, Stack, useComputedColorScheme } from '@mantine/core';
import { IconExternalLink } from '@tabler/icons-react';

import { useMonitoring } from '../hooks';
import type { DashboardKey } from '../types';
import { errorMessage, grafanaUrl } from '../utils';

/**
 * Tableau de bord Grafana intégré (mode kiosque), au thème de l'interface, pour un nœud du lab.
 * Grafana garde en kiosque ses sélecteurs de période et de variables : rien à dupliquer ici.
 * nodeVar : variable du tableau qui reçoit le nœud (« client » dans « Flux réseau » pour un client).
 * extraVars : autres variables du tableau (sans le préfixe var-), ex. { vue: 'tous' }.
 */
export function GrafanaFrame({
  dashboard,
  node,
  nodeVar = 'node',
  extraVars = {},
}: {
  dashboard: DashboardKey;
  node?: string;
  nodeVar?: string;
  extraVars?: Record<string, string>;
}) {
  const { data, isLoading, error } = useMonitoring();
  const scheme = useComputedColorScheme('light');

  if (isLoading) return <Loader />;
  if (error || !data) {
    return (
      <Alert color="red" title="Métrologie indisponible">
        L'API n'a pas indiqué où joindre Grafana : {errorMessage(error)}
      </Alert>
    );
  }
  const extra = Object.fromEntries(Object.entries(extraVars).map(([k, v]) => [`var-${k}`, v]));
  const vars = { [`var-${nodeVar}`]: node, ...extra, from: 'now-1h', to: 'now' };
  const embedded = grafanaUrl(data, dashboard, { ...vars, refresh: '10s', theme: scheme }, true);
  const full = grafanaUrl(data, dashboard, vars);

  return (
    <Stack gap="sm">
      {/* période, rafraîchissement et nœud se règlent dans les contrôles de Grafana, juste en dessous */}
      <Group justify="flex-end" gap="sm">
        <Button
          component="a"
          href={full}
          target="_blank"
          rel="noreferrer"
          size="xs"
          variant="default"
          leftSection={<IconExternalLink size={16} />}
        >
          Ouvrir dans Grafana
        </Button>
      </Group>
      {data.grafana.available ? (
        <iframe key={embedded} src={embedded} title={`Métrologie de ${node ?? 'lab'}`} className="grafana-frame" />
      ) : (
        <Alert color="orange" title="Grafana ne répond pas">
          La stack de métrologie n'est pas démarrée ou démarre encore. Vérifiez <Code>make ps</Code> et{' '}
          <Code>docker compose logs grafana telegraf influxdb</Code>.
        </Alert>
      )}
    </Stack>
  );
}
