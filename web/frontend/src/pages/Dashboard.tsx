import { Fragment, useState, type ReactNode } from 'react';
import {
  Alert,
  Anchor,
  Badge,
  Button,
  Card,
  Code,
  Group,
  Loader,
  Menu,
  Progress,
  ScrollArea,
  SimpleGrid,
  Stack,
  Table,
  Text,
  Title,
  Tooltip,
} from '@mantine/core';
import { modals } from '@mantine/modals';
import {
  IconAlertTriangle,
  IconChevronDown,
  IconDeviceFloppy,
  IconFileText,
  IconGitMerge,
  IconInfoCircle,
  IconPlayerPlay,
  IconPlayerStop,
  IconRefreshAlert,
  IconRestore,
  IconSettings,
  IconTerminal2,
} from '@tabler/icons-react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { Link } from 'react-router';

import { api } from '../api';
import { ConnectivityButton } from '../components/ConnectivityModal';
import { RunnerBanner, RunnerLogsDrawer, gitopsNote, withCode } from '../components/RunnerPanel';
import { TopologyLegend, TopologyView } from '../components/TopologyView';
import { CopyValue, Led, SectionCard, StatusDot } from '../components/common';
import { useLab } from '../hooks';
import type { BgpPeer, GitOpsState, LabState, LinkEnd, NodeCard, Phase } from '../types';
import {
  PHASES,
  errorMessage,
  notifyError,
  notifySave,
  notifySuccess,
  plural,
  runnerDisplayState,
  runnerRunning,
  saveTarget,
  stateColor,
  uptimeSince,
} from '../utils';

/** Voisin BGP sur une ligne : LED de session, adresse, AS distant, état (coloré seulement s'il n'est pas établi). */
function PeerLine({ peer }: { peer: BgpPeer }) {
  const ok = peer.state === 'Established';
  return (
    <Group component="span" gap={8} wrap="wrap" style={{ display: 'flex' }}>
      <span className="state-label">
        <Led tone={ok ? 'ok' : 'err'} />
        {peer.peer}
      </span>
      <span className="muted">AS {peer.remote_as ?? '—'}</span>
      <span style={{ color: ok ? undefined : 'var(--state-err)' }}>{peer.state ?? '—'}</span>
    </Group>
  );
}

function NodeSummary({ node }: { node: NodeCard }) {
  const meta = PHASES[node.phase] ?? PHASES.down;
  const ready = node.phase === 'ready';
  const ips = Object.entries(node.addresses);
  const peers = node.bgp?.peers ?? [];
  return (
    <Card h="100%" style={{ display: 'flex', flexDirection: 'column' }}>
      <Group justify="space-between" wrap="nowrap" gap="sm">
        <Group gap={8} wrap="nowrap">
          <StatusDot phase={node.phase} />
          <Link to={`/nodes/${node.name}`} className="node-name">
            {node.name}
          </Link>
        </Group>
        {!ready && (
          <Text size="sm" c={stateColor(meta.color)}>
            {meta.label}
          </Text>
        )}
      </Group>
      <Text fz="xs" className="mono muted" mt={4}>
        {node.image ?? node.kind}
      </Text>
      {node.detail && !ready && (
        <Text size="xs" c={stateColor(meta.color)} mt={8}>
          {node.detail}
        </Text>
      )}
      {(node.phase === 'booting' || node.phase === 'bootstrap') && (
        <Progress value={(node.progress ?? 0) * 100} size="sm" mt={8} aria-label="Avancement du démarrage" />
      )}
      <dl className="kv" style={{ marginTop: 16 }}>
        <dt>mgmt</dt>
        <dd>
          <CopyValue value={node.mgmt_ipv4} />
        </dd>
        {ips.map(([iface, addrs]) => (
          <Fragment key={iface}>
            <dt>{iface}</dt>
            <dd>{addrs.join(', ')}</dd>
          </Fragment>
        ))}
        {node.role === 'router' && (
          <>
            <dt>BGP</dt>
            <dd>
              {peers.length ? (
                peers.map((p) => <PeerLine key={p.peer} peer={p} />)
              ) : (
                <span className="muted">aucun voisin</span>
              )}
            </dd>
          </>
        )}
        {node.started_at ? (
          <>
            <dt>up</dt>
            <dd>{uptimeSince(node.started_at)}</dd>
          </>
        ) : null}
      </dl>
      <Group gap={4} mt="auto" pt="md">
        <Button
          size="xs"
          variant="subtle"
          color="gray"
          leftSection={<IconInfoCircle size={16} />}
          component={Link}
          to={`/nodes/${node.name}`}
        >
          Détails
        </Button>
        <Button
          size="xs"
          variant="subtle"
          color="gray"
          leftSection={<IconTerminal2 size={16} />}
          component={Link}
          to={`/nodes/${node.name}?tab=cli`}
        >
          CLI
        </Button>
        {node.role === 'router' && (
          <Button
            size="xs"
            variant="subtle"
            color="gray"
            leftSection={<IconSettings size={16} />}
            component={Link}
            to={`/nodes/${node.name}?tab=config`}
          >
            Config
          </Button>
        )}
      </Group>
    </Card>
  );
}

interface Anomaly {
  key: string;
  phase: Phase;
  what: string;
  where: ReactNode;
  evidence: string;
  check: { to: string; label: string };
}

const endName = (e: LinkEnd) => `${e.node}:${e.port ?? e.interface}`;

/** Constat pour un nœud qui n'est pas prêt. */
const NODE_ISSUE: Record<Phase, string> = {
  ready: 'Nœud prêt',
  booting: 'Nœud en démarrage',
  bootstrap: 'Nœud en bootstrap',
  warning: 'Nœud dégradé',
  error: 'Nœud en échec',
  down: 'Nœud arrêté',
};
const endState = (e: LinkEnd) => (e.admin === 'down' ? 'shutdown' : e.up === false ? 'down' : e.up ? 'up' : 'état inconnu');

/** Ce qui ne va pas, où, et quelle vue le confirme : nœuds non prêts, liens non up, sessions BGP non établies. */
function anomalies(lab: LabState): Anomaly[] {
  const out: Anomaly[] = [];
  for (const n of lab.nodes) {
    if (n.phase === 'ready') continue;
    out.push({
      key: `node-${n.name}`,
      phase: n.phase,
      what: NODE_ISSUE[n.phase] ?? `Nœud ${n.phase}`,
      where: <Anchor component={Link} to={`/nodes/${n.name}`} size="sm">{n.name}</Anchor>,
      evidence: n.detail || '—',
      check: n.phase === 'down' ? { to: `/nodes/${n.name}`, label: 'Page du nœud' } : { to: `/nodes/${n.name}?tab=status`, label: 'État du nœud' },
    });
  }
  for (const l of lab.links) {
    if (l.state === 'up') continue;
    // côté à inspecter : de préférence un nœud en service (un conteneur arrêté n'a pas d'onglet État)
    const running = (e: LinkEnd) => lab.nodes.find((n) => n.name === e.node)?.phase !== 'down';
    const probe = running(l.a) ? l.a : l.z;
    out.push({
      key: `link-${l.id}`,
      phase: l.state === 'down' ? 'error' : 'warning',
      what: l.state === 'down' ? 'Lien down' : 'Lien : état inconnu',
      where: (
        <Text span ff="monospace" fz="sm">
          {endName(l.a)} ↔ {endName(l.z)}
        </Text>
      ),
      evidence: `${endName(l.a)} ${endState(l.a)} · ${endName(l.z)} ${endState(l.z)}`,
      check: { to: `/nodes/${probe.node}?tab=status`, label: `Interfaces de ${probe.node}` },
    });
  }
  for (const b of lab.bgp_sessions) {
    if (b.state === 'Established') continue;
    out.push({
      key: `bgp-${b.a}-${b.z}`,
      phase: 'error',
      what: 'Session eBGP non établie',
      where: (
        <>
          <Anchor component={Link} to={`/nodes/${b.a}`} size="sm">{b.a}</Anchor> ↔{' '}
          <Anchor component={Link} to={`/nodes/${b.z}`} size="sm">{b.z}</Anchor>
        </>
      ),
      evidence: `état ${b.state}`,
      check: { to: `/nodes/${b.a}?tab=status`, label: `Voisins BGP de ${b.a}` },
    });
  }
  return out;
}

/** Un compteur de santé : « 3/4 nœuds prêts », le nombre en orange tant qu'il n'est pas complet. */
function Health({ n, total, one, many }: { n: number; total: number; one: string; many: string }) {
  return (
    <Text span size="sm" c="dimmed" style={{ whiteSpace: 'nowrap' }}>
      <Text span inherit className="mono" fw={500} c={n < total ? 'var(--state-warn)' : 'var(--mantine-color-text)'}>
        {n}/{total}
      </Text>{' '}
      {total >= 2 ? many : one}
    </Text>
  );
}

function AnomalyList({ lab }: { lab: LabState }) {
  const items = anomalies(lab);
  const ready = lab.nodes.filter((n) => n.phase === 'ready').length;
  const established = lab.bgp_sessions.filter((b) => b.state === 'Established').length;
  const up = lab.links.filter((l) => l.state === 'up').length;
  return (
    <SectionCard
      title="Anomalies"
      count={items.length || undefined}
      actions={
        <Group gap="lg" wrap="wrap">
          <Health n={ready} total={lab.nodes.length} one="nœud prêt" many="nœuds prêts" />
          <Health n={up} total={lab.links.length} one="lien up" many="liens up" />
          <Health n={established} total={lab.bgp_sessions.length} one="session eBGP établie" many="sessions eBGP établies" />
        </Group>
      }
    >
      {items.length === 0 ? (
        <Group gap={8}>
          <StatusDot phase="ready" decorative />
          <Text size="sm" c="dimmed">
            Aucune anomalie
          </Text>
        </Group>
      ) : (
        <ScrollArea type="auto">
          <Table fz="sm" miw={720}>
            <Table.Thead>
              <Table.Tr>
                <Table.Th>Constat</Table.Th>
                <Table.Th>Où</Table.Th>
                <Table.Th>Données</Table.Th>
                <Table.Th>Vérifier</Table.Th>
              </Table.Tr>
            </Table.Thead>
            <Table.Tbody>
              {items.map((a) => (
                <Table.Tr key={a.key}>
                  <Table.Td>
                    <Group gap={8} wrap="nowrap">
                      <StatusDot phase={a.phase} />
                      <Text size="sm" fw={500}>
                        {a.what}
                      </Text>
                    </Group>
                  </Table.Td>
                  <Table.Td>{a.where}</Table.Td>
                  <Table.Td>
                    <Text size="xs" ff="monospace">
                      {a.evidence}
                    </Text>
                  </Table.Td>
                  <Table.Td>
                    <Anchor size="sm" component={Link} to={a.check.to}>
                      {a.check.label}
                    </Anchor>
                  </Table.Td>
                </Table.Tr>
              ))}
            </Table.Tbody>
          </Table>
        </ScrollArea>
      )}
    </SectionCard>
  );
}

const GITOPS_STATES: Record<string, { color: string; label: string }> = {
  deployed: { color: 'teal', label: 'déployé' },
  deploying: { color: 'blue', label: 'déploiement…' },
  failed: { color: 'red', label: 'échec' },
  interrupted: { color: 'orange', label: 'interrompu' },
};

/** Infra en GitOps : commit appliqué au lab, lien vers le projet GitLab. */
function GitOpsBadge({ gitops }: { gitops?: GitOpsState }) {
  if (!gitops?.enabled) return null;
  const st = GITOPS_STATES[gitops.status ?? ''] ?? { color: 'gray', label: gitops.status ?? '?' };
  return (
    <Tooltip label={gitops.message || gitops.project} multiline w={360} withArrow>
      <Badge
        component="a"
        href={gitops.project_url ?? undefined}
        target="_blank"
        rel="noreferrer"
        variant="light"
        color={st.color}
        leftSection={<IconGitMerge size={12} />}
        style={{ cursor: 'pointer', textTransform: 'none' }}
      >
        {gitops.applied_sha ? gitops.applied_sha.slice(0, 8) : '—'} {st.label}
      </Badge>
    </Tooltip>
  );
}

/**
 * Le fichier de topologie ne permet pas de déployer, ou ne correspond plus au lab : fichier illisible ou
 * mal nommé, nœuds déployés qui n'y figurent plus, conteneurs restés après l'arrêt.
 */
function TopologyAlerts({ lab }: { lab: LabState }) {
  const stopped = !runnerRunning(lab.runner);
  const left = stopped
    ? [...lab.stale_nodes, ...lab.nodes.filter((n) => n.container_state !== 'absent').map((n) => n.name)]
    : [];
  return (
    <>
      {lab.topology_error && (
        <Alert color="red" icon={<IconAlertTriangle size={16} />} title={`Fichier de topologie : ${lab.topology_file}`}>
          <Text size="sm">{lab.topology_error}</Text>
        </Alert>
      )}
      {left.length > 0 && (
        <Alert color="orange" icon={<IconAlertTriangle size={16} />} title="Des conteneurs du lab sont restés après l'arrêt">
          <Text size="sm">
            {left.join(', ')} : ils seront supprimés au démarrage du lab (le runner supprime tous les conteneurs du lab
            avant de déployer).
          </Text>
        </Alert>
      )}
      {!stopped && lab.stale_nodes.length > 0 && (
        <Alert color="orange" icon={<IconAlertTriangle size={16} />} title="Le lab déployé ne correspond plus au fichier de topologie">
          <Text size="sm">
            Nœuds déployés absents de <Code>{lab.topology_file}</Code> : {lab.stale_nodes.join(', ')}. Pour appliquer le
            fichier, redéployez le lab, ou arrêtez-le puis démarrez-le : ces nœuds seront supprimés.
          </Text>
        </Alert>
      )}
    </>
  );
}

function LabActions({ lab, onShowLogs }: { lab: LabState; onShowLogs: () => void }) {
  const { gitops, runner } = lab;
  const qc = useQueryClient();
  const refresh = () => qc.invalidateQueries();
  const display = runnerDisplayState(runner);
  const canRedeploy = runner.state !== 'absent' && runner.action === null && display !== 'destroying';
  const saveAll = useMutation({
    mutationFn: api.saveAll,
    onSuccess: (r) => notifySave(r, 'Sauvegarde'),
    onError: (e) => notifyError(e, 'Sauvegarde'),
  });
  const resetAll = useMutation({
    mutationFn: api.resetAll,
    onSuccess: (r) => {
      refresh();
      if (r.ok) notifySuccess('Routeurs remis dans leur conf de démarrage');
      else notifyError(new Error('certaines opérations ont échoué'), 'Réinitialisation');
    },
    onError: (e) => notifyError(e, 'Réinitialisation'),
  });
  const redeploy = useMutation({
    mutationFn: api.redeploy,
    onSuccess: (r) => {
      refresh();
      notifySuccess(r.message, 'Redéploiement');
    },
    onError: (e) => notifyError(e, 'Redéploiement'),
  });
  // Démarrer / arrêter le lab, c'est démarrer / arrêter le service compose clab : son runner déploie le
  // fichier de topologie au démarrage et détruit le lab à l'arrêt. Entre les deux, on peut changer d'infra.
  // Mutations ici et non dans le menu : son contenu est démonté à la fermeture.
  const start = useMutation({
    mutationFn: api.startLab,
    onSuccess: (r) => {
      refresh();
      notifySuccess(r.message, 'Démarrage du lab');
    },
    onError: (e) => notifyError(e, 'Démarrage du lab'),
  });
  const stop = useMutation({
    mutationFn: api.stopLab,
    onSuccess: (r) => {
      refresh();
      notifySuccess(r.message, 'Arrêt du lab');
    },
    onError: (e) => notifyError(e, 'Arrêt du lab'),
  });
  const running = runnerRunning(runner);
  const stopping = display === 'stopping' || display === 'destroying';

  const confirm = (title: string, text: string, label: string, color: string, onConfirm: () => void) =>
    modals.openConfirmModal({
      title,
      children: <Text size="sm">{text}</Text>,
      labels: { confirm: label, cancel: 'Annuler' },
      confirmProps: { color },
      onConfirm,
    });
  const confirmStop = () =>
    modals.openConfirmModal({
      title: 'Arrêter le lab',
      children: (
        <Stack gap="xs">
          <Text size="sm">
            containerlab détruit tous les nœuds du lab (conteneurs et liens) : les confs running non sauvegardées sont
            perdues. Les confs de démarrage (<Code>lab/configs/</Code>), les snapshots et <Code>{lab.topology_file}</Code>{' '}
            sont conservés.
          </Text>
          <Text size="sm">
            Une fois le lab arrêté, vous pouvez modifier ou remplacer <Code>{lab.topology_file}</Code>, puis le relancer avec
            « Actions du lab » → « Démarrer le lab ».
          </Text>
          {gitops?.enabled && (
            <Text size="sm" c="orange">
              {withCode(gitopsNote(gitops))}
            </Text>
          )}
        </Stack>
      ),
      labels: { confirm: 'Arrêter', cancel: 'Annuler' },
      confirmProps: { color: 'red' },
      onConfirm: () => stop.mutate(),
    });

  const powerItem = running ? (
    // l'arrêt reste possible pendant un déploiement, pour abandonner une topologie qui se déploie mal
    <Menu.Item
      color="red"
      leftSection={<IconPlayerStop size={16} />}
      disabled={runner.action !== null || stopping}
      onClick={confirmStop}
    >
      {stopping ? 'Arrêt du lab en cours…' : 'Arrêter le lab'}
    </Menu.Item>
  ) : (
    <Menu.Item
      color="green"
      leftSection={<IconPlayerPlay size={16} />}
      disabled={runner.state === 'absent' || runner.action !== null}
      onClick={() => start.mutate()}
    >
      Démarrer le lab
      {runner.state === 'absent' && (
        <Text fz="xs" c="dimmed">
          Service clab absent : lancez <Code>make up</Code>
        </Text>
      )}
    </Menu.Item>
  );

  return (
    <Group gap="xs">
      <ConnectivityButton />
      <Menu position="bottom-end" withinPortal>
        <Menu.Target>
          <Button
            variant="default"
            rightSection={<IconChevronDown size={16} />}
            loading={saveAll.isPending || resetAll.isPending || redeploy.isPending || start.isPending || stop.isPending}
          >
            Actions du lab
          </Button>
        </Menu.Target>
        <Menu.Dropdown>
          {powerItem}
          <Menu.Item
            color="red"
            leftSection={<IconRefreshAlert size={16} />}
            disabled={!canRedeploy}
            onClick={() =>
              confirm(
                'Redéployer le lab',
                `Le lab est détruit puis redéployé par containerlab depuis ${lab.topology_file}, même modifié depuis le déploiement : tous les conteneurs sont recréés avec leur conf de démarrage. Les modifications non sauvegardées sont perdues.`,
                'Redéployer',
                'red',
                () => redeploy.mutate(),
              )
            }
          >
            Redéployer le lab
          </Menu.Item>
          <Menu.Divider />
          <Menu.Item
            leftSection={<IconDeviceFloppy size={16} />}
            onClick={() =>
              confirm(
                'Sauvegarder toutes les confs',
                saveTarget(gitops, 'La conf running de chaque routeur (CONFIG_DB + FRR)'),
                'Sauvegarder',
                'blue',
                () => saveAll.mutate(),
              )
            }
          >
            Sauvegarder tout (running → startup)
          </Menu.Item>
          <Menu.Item
            leftSection={<IconRestore size={16} />}
            onClick={() =>
              confirm(
                'Réinitialiser les routeurs',
                'Chaque routeur reçoit sa conf de démarrage en mode replace (les sessions BGP peuvent être réinitialisées).',
                'Réinitialiser',
                'orange',
                () => resetAll.mutate(),
              )
            }
          >
            Réinitialiser (startup → running)
          </Menu.Item>
          <Menu.Divider />
          <Menu.Item leftSection={<IconFileText size={16} />} onClick={onShowLogs}>
            Logs du déploiement
          </Menu.Item>
        </Menu.Dropdown>
      </Menu>
    </Group>
  );
}

export function Dashboard() {
  const { data: lab, isLoading, error } = useLab();
  const [logsOpen, setLogsOpen] = useState(false);

  if (isLoading) return <Loader />;
  if (!lab) {
    return (
      <Alert color="red" title="API injoignable">
        Aucune donnée reçue de l'API de l'interface : {errorMessage(error)}
      </Alert>
    );
  }
  const display = runnerDisplayState(lab.runner);
  const labDown = !runnerRunning(lab.runner) || display === 'stopping' || display === 'destroying';

  return (
    <Stack gap="lg">
      <Group justify="space-between" align="flex-end" gap="md">
        <div>
          <Title order={2}>{lab.name}</Title>
          <Text size="sm" c="dimmed" mt={4}>
            containerlab · <span className="nowrap">
              mgmt <span className="mono">{lab.mgmt.subnet ?? '—'}</span>
            </span>{' '}
            · <span className="nowrap">{plural(lab.nodes.length, 'nœud', 'nœuds')}</span> ·{' '}
            <span className="nowrap">{plural(lab.links.length, 'lien', 'liens')}</span>
          </Text>
          <GitOpsBadge gitops={lab.gitops} />
        </div>
        <LabActions lab={lab} onShowLogs={() => setLogsOpen(true)} />
      </Group>
      <RunnerBanner
        runner={lab.runner}
        topologyFile={lab.topology_file}
        gitops={lab.gitops}
        onShowLogs={() => setLogsOpen(true)}
      />
      <TopologyAlerts lab={lab} />
      {/* lab arrêté ou en cours d'arrêt : une ligne « Nœud arrêté » par nœud ne dirait rien de plus */}
      {!labDown && <AnomalyList lab={lab} />}
      <SectionCard
        title={labDown ? `Topologie de ${lab.topology_file} (non déployée)` : 'Topologie'}
        actions={<TopologyLegend />}
      >
        {lab.nodes.length > 0 ? (
          <TopologyView lab={lab} />
        ) : (
          <Text size="sm" c="dimmed">
            Aucun nœud à afficher.
          </Text>
        )}
      </SectionCard>
      <SimpleGrid cols={{ base: 1, md: 2 }} spacing="md">
        {lab.nodes.map((n) => (
          <NodeSummary key={n.name} node={n} />
        ))}
      </SimpleGrid>
      <RunnerLogsDrawer opened={logsOpen} onClose={() => setLogsOpen(false)} />
    </Stack>
  );
}
