import { Alert, Button, Code, Drawer, Group, Loader, Stack, Text } from '@mantine/core';
import { IconAlertTriangle, IconInfoCircle } from '@tabler/icons-react';

import { api } from '../api';
import type { GitOpsState, RunnerState } from '../types';
import { runnerDisplayState, runnerState } from '../utils';
import { LogsViewer } from './LogsViewer';

/** Message par état affiché ; {topo} : fichier de topologie (lab/sonic-lab.clab.yml). */
const MESSAGES: Record<string, string> = {
  absent: "Le service compose « clab » ne tourne pas : lancez `make up` (ou `docker compose up -d`) à la racine du projet.",
  stopped:
    "Pour changer d'infra, modifiez ou remplacez `{topo}` (et `lab/configs/<routeur>/`), puis « Actions du lab » → « Démarrer le lab » (ou `make start`). Le tableau de bord montre déjà la topologie du fichier.",
  failed: 'Le déploiement a échoué : consultez les logs, corrigez `{topo}` si besoin, puis « Redéployer le lab » (ou Arrêter puis Démarrer).',
  deployed: 'Le lab est déployé.',
  'loading-image': "Chargement de l'image docker-sonic-vs fournie (une seule fois)…",
  starting: 'Le service clab démarre : containerlab va déployer `{topo}`…',
  deploying: 'containerlab crée les conteneurs et les liens, puis les routeurs SONiC démarrent (≈ 20 s).',
  redeploying: 'Destruction puis nouveau déploiement du lab depuis `{topo}`…',
  stopping: 'Arrêt du service clab : containerlab va détruire les nœuds du lab…',
  destroying: 'containerlab détruit les nœuds du lab…',
};

/** Les commandes entre accents graves deviennent du code en ligne. */
export function withCode(text: string) {
  return text.split(/`([^`]+)`/).map((part, i) => (i % 2 ? <Code key={i}>{part}</Code> : part));
}

export function RunnerBanner({
  runner,
  topologyFile,
  gitops,
  onShowLogs,
}: {
  runner: RunnerState;
  topologyFile: string;
  gitops?: GitOpsState;
  onShowLogs: () => void;
}) {
  const state = runnerDisplayState(runner);
  if (state === 'deployed' && !runner.error) return null;
  const meta = runnerState(state);
  const color = runner.error && !meta.busy ? 'red' : meta.color;
  const icon = meta.busy ? (
    <Loader size={16} color={color} />
  ) : color === 'red' ? (
    <IconAlertTriangle size={16} />
  ) : (
    <IconInfoCircle size={16} />
  );
  const message = (MESSAGES[state] ?? `État du runner : ${state}`).replace(/\{topo\}/g, topologyFile);
  const changingInfra = state === 'stopped' || state === 'failed';
  return (
    <Alert color={color} icon={icon} title={meta.label} variant="light">
      <Group justify="space-between" wrap="nowrap" align="flex-start" gap="md">
        <Stack gap={4}>
          <Text size="sm">{withCode(message)}</Text>
          {runner.error && (
            <Text size="sm" c="red">
              {runner.error}
            </Text>
          )}
          {gitops?.enabled && changingInfra && (
            <Text size="sm">{withCode(gitopsNote(gitops))}</Text>
          )}
        </Stack>
        <Button size="xs" variant="default" onClick={onShowLogs} style={{ flexShrink: 0 }}>
          Logs du déploiement
        </Button>
      </Group>
      {runner.container_status && (
        <Text size="xs" c="dimmed" mt={4}>
          conteneur <span className="mono">{runner.container}</span> : {runner.container_status}
        </Text>
      )}
    </Alert>
  );
}

/** Infra en GitOps : un fichier changé localement ne survit pas au prochain merge. */
export function gitopsNote(gitops: GitOpsState): string {
  const project = gitops.project ? ` (${gitops.project})` : '';
  return `Infra en GitOps${project} : un fichier modifié ici est remplacé au prochain merge, et les merges faits pendant l'arrêt s'appliquent au démarrage. Pour changer d'infra durablement, passez par une merge request (ou \`GITOPS_ENABLED=0\` dans .env).`;
}

export function RunnerLogsDrawer({ opened, onClose }: { opened: boolean; onClose: () => void }) {
  return (
    <Drawer opened={opened} onClose={onClose} position="right" size="xl" title="Logs du service clab (containerlab)">
      {opened && <LogsViewer queryKey={['runner-logs']} fetchLogs={(n) => api.runnerLogs(n)} height={720} defaultFollow />}
    </Drawer>
  );
}
