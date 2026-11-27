import { useEffect, useState, type ReactNode } from 'react';
import { Alert, Code, Group, Loader, Stack, Tabs, Text, Title } from '@mantine/core';
import { useParams, useSearchParams } from 'react-router';

import { api } from '../api';
import { CaptureTab } from '../components/CaptureTab';
import { CommandRunner, LINUX_COMMANDS, SONIC_COMMANDS } from '../components/CommandRunner';
import { GrafanaFrame } from '../components/GrafanaFrame';
import { LogsViewer } from '../components/LogsViewer';
import { Terminal } from '../components/Terminal';
import { CopyValue, PhaseLabel } from '../components/common';
import { useLab } from '../hooks';
import type { LabState, NodeCard } from '../types';
import { PHASES, errorMessage, stateColor } from '../utils';
import { ClientStatusTab, TestsTab } from './client/ClientTabs';
import { ConfigTab } from './router/ConfigTab';
import { RouterStatusTab } from './router/RouterStatusTab';
import { SnapshotsTab } from './router/SnapshotsTab';

/** Hauteur du terminal et des logs : le reste de la fenêtre sous l'en-tête et les onglets. */
const PANE_HEIGHT = 'max(360px, calc(100vh - 270px))';
// tableau « Flux réseau » : liste « Trafic » sur « Tous les ports » (valeur = tag edge exclu, voir lab-flows.json)
const FLOWS_ALL_PORTS = { vue: 'tous' };

interface TabDef {
  value: string;
  label: string;
  render: (active: boolean) => ReactNode;
}

function NodeHeader({ card }: { card: NodeCard }) {
  const meta = PHASES[card.phase] ?? PHASES.down;
  return (
    <div>
      <Group gap="md" align="center">
        <Title order={2}>{card.name}</Title>
        <PhaseLabel phase={card.phase} detail={card.detail} />
      </Group>
      <Group mt={8} wrap="wrap" align="center" style={{ columnGap: 24, rowGap: 4 }}>
        <Text size="sm" c="dimmed">
          {card.role === 'router' ? 'Routeur SONiC (docker-sonic-vs)' : 'Client Alpine'} · kind <span className="mono">{card.kind}</span>
        </Text>
        <Text size="sm" component="div" c="dimmed">
          mgmt <CopyValue value={card.mgmt_ipv4} />
        </Text>
        <Text size="sm" component="div" c="dimmed">
          conteneur <CopyValue value={card.container} />
        </Text>
      </Group>
      {card.detail && card.phase !== 'ready' && (
        <Text size="sm" c={stateColor(meta.color)} mt={8}>
          {card.detail}
        </Text>
      )}
    </div>
  );
}

/** Onglets synchronisés avec ?tab= ; un onglet n'est monté qu'à sa première ouverture (terminal, logs...). */
function NodeTabs({ tabs }: { tabs: TabDef[] }) {
  const [params, setParams] = useSearchParams();
  const tab = params.get('tab') && tabs.some((t) => t.value === params.get('tab')) ? params.get('tab')! : tabs[0].value;
  const [visited, setVisited] = useState<Set<string>>(() => new Set([tab]));
  useEffect(() => {
    setVisited((v) => (v.has(tab) ? v : new Set(v).add(tab)));
  }, [tab]);
  return (
    <Tabs value={tab} onChange={(v) => v && setParams({ tab: v }, { replace: true })} keepMounted>
      <Tabs.List className="tabs-scroll">
        {tabs.map((t) => (
          <Tabs.Tab key={t.value} value={t.value}>
            {t.label}
          </Tabs.Tab>
        ))}
      </Tabs.List>
      {tabs.map((t) => (
        <Tabs.Panel key={t.value} value={t.value} pt="md">
          {visited.has(t.value) && t.render(t.value === tab)}
        </Tabs.Panel>
      ))}
    </Tabs>
  );
}

function RouterPage({ card }: { card: NodeCard }) {
  const name = card.name;
  const tabs: TabDef[] = [
    { value: 'status', label: 'État', render: (a) => <RouterStatusTab name={name} active={a} /> },
    {
      value: 'metrics',
      label: 'Métrologie',
      // monté seulement quand l'onglet est affiché : pas de rafraîchissement Grafana en arrière-plan
      render: (a) => (a ? <GrafanaFrame dashboard="router" node={name} /> : null),
    },
    {
      value: 'flows',
      label: 'Flux',
      // types de trafic échantillonnés par sFlow sur les ports du routeur (tableau « Flux réseau »), tous
      // ports : un spine ou un routeur IPN n'a aucun port vers un client (vue « Entrée du réseau » vide)
      render: (a) => (a ? <GrafanaFrame dashboard="flows" node={name} extraVars={FLOWS_ALL_PORTS} /> : null),
    },
    { value: 'config', label: 'Configuration', render: () => <ConfigTab name={name} /> },
    { value: 'backups', label: 'Sauvegardes', render: () => <SnapshotsTab name={name} /> },
    { value: 'cli', label: 'CLI', render: () => <Terminal node={name} shells={card.shells} height={PANE_HEIGHT} /> },
    { value: 'capture', label: 'Capture', render: (a) => <CaptureTab name={name} active={a} height={PANE_HEIGHT} /> },
    {
      value: 'commands',
      label: 'Commandes',
      render: () => <CommandRunner node={name} quick={SONIC_COMMANDS} />,
    },
    {
      value: 'logs',
      label: 'Logs',
      render: () => <LogsViewer queryKey={['logs', name]} fetchLogs={(n) => api.logs(name, n)} height={PANE_HEIGHT} />,
    },
  ];
  return <NodeTabs tabs={tabs} />;
}

function ClientPage({ card, lab }: { card: NodeCard; lab: LabState }) {
  const name = card.name;
  const tabs: TabDef[] = [
    { value: 'status', label: 'État', render: (a) => <ClientStatusTab name={name} active={a} /> },
    {
      value: 'metrics',
      label: 'Métrologie',
      render: (a) => (a ? <GrafanaFrame dashboard="client" node={name} /> : null),
    },
    {
      value: 'flows',
      label: 'Flux',
      // trafic émis ou reçu par ce nœud, échantillonné par sFlow sur les routeurs qu'il traverse
      render: (a) => (a ? <GrafanaFrame dashboard="flows" node={name} nodeVar="client" /> : null),
    },
    { value: 'tests', label: 'Tests réseau', render: () => <TestsTab name={name} lab={lab} /> },
    { value: 'cli', label: 'CLI', render: () => <Terminal node={name} shells={card.shells} height={PANE_HEIGHT} /> },
    { value: 'capture', label: 'Capture', render: (a) => <CaptureTab name={name} active={a} height={PANE_HEIGHT} /> },
    {
      value: 'commands',
      label: 'Commandes',
      render: () => <CommandRunner node={name} quick={LINUX_COMMANDS} />,
    },
  ];
  return <NodeTabs tabs={tabs} />;
}

export function NodePage() {
  const { name = '' } = useParams();
  const { data: lab, isLoading, error } = useLab();
  if (isLoading) return <Loader />;
  if (!lab) {
    return (
      <Alert color="red" title="API injoignable">
        Aucune donnée reçue de l'API de l'interface : {errorMessage(error)}
      </Alert>
    );
  }
  const card = lab.nodes.find((n) => n.name === name);
  if (!card) return <Alert color="red">Nœud inconnu : {name}</Alert>;
  return (
    <Stack key={name}>
      <NodeHeader card={card} />
      {card.phase === 'down' ? (
        <Alert color="gray" title="Nœud arrêté">
          {card.detail}. Démarrez le lab depuis le tableau de bord (« Actions du lab » → « Démarrer le lab », ou <Code>make start</Code>),
          ou redéployez-le s'il tourne déjà.
        </Alert>
      ) : card.role === 'router' ? (
        <RouterPage card={card} />
      ) : (
        <ClientPage card={card} lab={lab} />
      )}
    </Stack>
  );
}
