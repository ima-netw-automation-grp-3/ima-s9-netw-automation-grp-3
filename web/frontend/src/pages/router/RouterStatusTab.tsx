import { useState, type ReactNode } from 'react';
import {
  Accordion,
  Alert,
  Box,
  Button,
  Card,
  Group,
  Loader,
  Progress,
  ScrollArea,
  SimpleGrid,
  Stack,
  Switch,
  Table,
  Text,
  Tooltip,
} from '@mantine/core';
import { IconPlayerPlay } from '@tabler/icons-react';
import { useMutation, useQueryClient } from '@tanstack/react-query';

import { api } from '../../api';
import { FrontPanel } from '../../components/FrontPanel';
import { CopyValue, Led, SectionCard, StaleDataAlert, StateLabel, StatusDot, type Tone } from '../../components/common';
import { useLab, useNodeStatus, useRates } from '../../hooks';
import type { RouterStatus } from '../../types';
import {
  PHASES,
  errorMessage,
  formatCounters,
  formatRate,
  formatSpeed,
  notifyError,
  notifySuccess,
  plural,
  stateColor,
  uptimeSince,
} from '../../utils';

/**
 * États supervisord, mêmes couleurs que Grafana (« Services », « État des services »).
 * EXITED : programme de démarrage terminé, normal ; STOPPED : non lancé dans cette image.
 */
const SERVICE_TONES: Record<string, Tone> = {
  RUNNING: 'ok',
  STARTING: 'info',
  STOPPING: 'busy',
  BACKOFF: 'warn',
  FATAL: 'err',
  EXITED: 'off',
  STOPPED: 'off',
  UNKNOWN: 'off',
};
const SERVICE_ORDER = ['FATAL', 'BACKOFF', 'STARTING', 'STOPPING', 'RUNNING', 'EXITED', 'STOPPED', 'UNKNOWN'];
const FAILED = new Set(['FATAL', 'BACKOFF']);

function StatCard({ title, children }: { title: string; children: ReactNode }) {
  return (
    <Card h="100%">
      <Text size="xs" c="dimmed" mb={8}>
        {title}
      </Text>
      <Stack gap={4} align="flex-start">
        {children}
      </Stack>
    </Card>
  );
}

export function RouterStatusTab({ name, active }: { name: string; active: boolean }) {
  const qc = useQueryClient();
  const { data, isLoading, error, dataUpdatedAt } = useNodeStatus(name, active);
  const { error: labError } = useLab();
  const st = data as RouterStatus | undefined;
  const rates = useRates(st?.interfaces, st?.ts);
  const [allPorts, setAllPorts] = useState(false);
  const bootstrap = useMutation({
    mutationFn: () => api.bootstrap(name),
    onSuccess: (r) => {
      if (r.exit_code === 0) notifySuccess('Bootstrap terminé : conf FRR de démarrage appliquée');
      else notifyError(new Error(r.stdout || r.stderr), 'Bootstrap');
      qc.invalidateQueries({ queryKey: ['node', name] });
    },
    onError: (e) => notifyError(e, 'Bootstrap'),
  });

  if (isLoading) return <Loader />;
  if (!st) return <Alert color="red" title="État illisible">{errorMessage(error)}</Alert>;
  if (st.phase === 'down') return <Alert color="gray">Le routeur n'est pas démarré : {st.detail}</Alert>;

  const meta = PHASES[st.phase] ?? PHASES.down;
  const ifaces = st.interfaces ?? {};
  const ports = (st.ports ?? []).filter(
    (p) => allPorts || p.wire || p.admin_status === 'up' || (ifaces[p.name]?.addresses.length ?? 0) > 0,
  );
  const extra = Object.keys(ifaces).filter((n) => !n.startsWith('Ethernet') && n !== 'eth0');
  const services = st.services ?? [];
  const key = new Set(st.key_services ?? []);
  const failed = services.filter((s) => FAILED.has(s.state));
  const rank = (state: string) => (SERVICE_ORDER.includes(state) ? SERVICE_ORDER.indexOf(state) : SERVICE_ORDER.length);
  const sortedServices = [...services].sort((a, b) => rank(a.state) - rank(b.state) || a.name.localeCompare(b.name));
  const stateCounts = SERVICE_ORDER.map((state) => [state, services.filter((s) => s.state === state).length] as const).filter(
    ([, n]) => n > 0,
  );
  const peers = st.bgp?.peers ?? [];
  const established = peers.filter((p) => p.state === 'Established').length;
  const errors = Object.entries(st.errors ?? {});
  const neighbors = st.neighbors ?? [];
  const routes = st.routes ?? [];

  return (
    <Stack gap="lg">
      {/* si l'API entière ne répond plus, l'alerte globale suffit */}
      {error && !labError && <StaleDataAlert error={error} updatedAt={dataUpdatedAt} />}
      {errors.length > 0 && (
        <Alert color="orange" title="Certaines informations n'ont pas pu être lues">
          {errors.map(([k, v]) => (
            <Text size="xs" key={k} ff="monospace">
              {k} : {v}
            </Text>
          ))}
        </Alert>
      )}
      <SimpleGrid cols={{ base: 1, sm: 3 }} spacing="md">
        <StatCard title="État">
          <Group gap={8} wrap="nowrap">
            <StatusDot phase={st.phase} decorative />
            <Text fz="md" fw={500} c={st.phase === 'ready' ? undefined : stateColor(meta.color)}>
              {meta.label}
            </Text>
          </Group>
          {st.detail && <Text size="xs">{st.detail}</Text>}
          {(st.phase === 'booting' || st.phase === 'bootstrap') && (
            <Progress value={(st.progress ?? 0) * 100} size="sm" w="100%" aria-label="Avancement du démarrage" />
          )}
          <Text size="xs" c="dimmed">
            conteneur up <span className="mono">{uptimeSince(st.started_at)}</span>
          </Text>
          <Button
            size="compact-sm"
            variant={st.phase === 'warning' ? 'filled' : 'subtle'}
            color={st.phase === 'warning' ? undefined : 'gray'}
            mt={8}
            leftSection={<IconPlayerPlay size={16} />}
            loading={bootstrap.isPending}
            onClick={() => bootstrap.mutate()}
          >
            Relancer le bootstrap
          </Button>
        </StatCard>
        <StatCard title="Version">
          <Text size="sm" className="mono">
            {st.version?.build_version ?? '—'}
          </Text>
          <Text size="xs" c="dimmed">
            {st.version?.frr ?? 'FRR —'} · Debian {st.version?.debian_version ?? '—'} · ASIC {st.version?.asic_type ?? '—'}
          </Text>
        </StatCard>
        <StatCard title="BGP">
          <Text fz="md" fw={500} className="mono">
            AS {st.bgp?.as ?? '—'}
          </Text>
          <Text size="xs" c="dimmed" component="div">
            router-id <CopyValue value={st.bgp?.router_id} />
          </Text>
          {peers.length === 0 ? (
            <Text size="sm" c="dimmed">
              Aucune session configurée
            </Text>
          ) : (
            <span className="state-label">
              <Led tone={established === peers.length ? 'ok' : 'warn'} />
              <Text span size="sm" c={established === peers.length ? 'dimmed' : 'var(--state-warn)'}>
                <span className="mono">
                  {established}/{peers.length}
                </span>{' '}
                {peers.length >= 2 ? 'sessions établies' : 'session établie'}
              </Text>
            </span>
          )}
        </StatCard>
      </SimpleGrid>

      <SectionCard
        title="Interfaces"
        actions={
          <Switch size="xs" label="Afficher les 32 ports" checked={allPorts} onChange={(e) => setAllPorts(e.currentTarget.checked)} />
        }
      >
        {(st.ports?.length ?? 0) > 0 && (
          <Box mb="md">
            <FrontPanel ports={st.ports!} node={name} />
          </Box>
        )}
        <ScrollArea type="auto">
          <Table highlightOnHover fz="sm" miw={980}>
            <Table.Thead>
              <Table.Tr>
                <Table.Th>Port</Table.Th>
                <Table.Th>Câble</Table.Th>
                <Table.Th>Description</Table.Th>
                <Table.Th>Admin</Table.Th>
                <Table.Th>Oper</Table.Th>
                <Table.Th className="num">MTU</Table.Th>
                <Table.Th className="num">Vitesse</Table.Th>
                <Table.Th>Adresses</Table.Th>
                <Table.Th className="num nowrap">RX / TX</Table.Th>
                <Table.Th className="num nowrap">Débit RX / TX</Table.Th>
              </Table.Tr>
            </Table.Thead>
            <Table.Tbody>
              {ports.map((p) => {
                const i = ifaces[p.name];
                const r = rates[p.name];
                return (
                  <Table.Tr key={p.name}>
                    <Table.Td ff="monospace" fw={500}>
                      {p.name}
                      <Text span className="muted" fz="xs" fw={400}>
                        {' '}
                        {p.alias}
                      </Text>
                    </Table.Td>
                    <Table.Td ff="monospace">{p.wire ?? '—'}</Table.Td>
                    <Table.Td>{p.description || '—'}</Table.Td>
                    <Table.Td>
                      <StateLabel value={p.admin_status} />
                    </Table.Td>
                    <Table.Td>
                      <StateLabel value={p.oper_status} />
                    </Table.Td>
                    <Table.Td className="num mono">{p.mtu ?? '—'}</Table.Td>
                    <Table.Td className="num mono">{formatSpeed(p.speed)}</Table.Td>
                    <Table.Td ff="monospace" fz="xs">
                      {i?.addresses.join(', ') || '—'}
                    </Table.Td>
                    <Table.Td className="num mono" fz="xs">
                      {formatCounters(i?.stats)}
                    </Table.Td>
                    <Table.Td className="num mono" fz="xs">
                      {formatRate(r?.rx)} / {formatRate(r?.tx)}
                    </Table.Td>
                  </Table.Tr>
                );
              })}
              {extra.map((n) => (
                <Table.Tr key={n}>
                  <Table.Td ff="monospace" fw={500}>
                    {n}
                  </Table.Td>
                  <Table.Td c="dimmed">—</Table.Td>
                  <Table.Td c="dimmed" className="nowrap">
                    interface logique
                  </Table.Td>
                  <Table.Td c="dimmed">—</Table.Td>
                  <Table.Td>
                    <Tooltip
                      label="Le noyau ne rapporte pas d'état opérationnel pour cette interface logique (normal pour une loopback)"
                      multiline
                      maw={300}
                      disabled={ifaces[n].operstate?.toLowerCase() !== 'unknown'}
                    >
                      <span>
                        <StateLabel value={ifaces[n].operstate} />
                      </span>
                    </Tooltip>
                  </Table.Td>
                  <Table.Td className="num mono">{ifaces[n].mtu ?? '—'}</Table.Td>
                  <Table.Td className="num" c="dimmed">
                    —
                  </Table.Td>
                  <Table.Td ff="monospace" fz="xs">
                    {ifaces[n].addresses.join(', ') || '—'}
                  </Table.Td>
                  <Table.Td className="num" c="dimmed">
                    —
                  </Table.Td>
                  <Table.Td className="num" c="dimmed">
                    —
                  </Table.Td>
                </Table.Tr>
              ))}
            </Table.Tbody>
          </Table>
        </ScrollArea>
      </SectionCard>

      <SimpleGrid cols={{ base: 1, lg: 2 }} spacing="lg">
        <SectionCard title="Voisins BGP" count={peers.length}>
          {peers.length === 0 ? (
            <Text c="dimmed" size="sm">
              Aucun voisin BGP configuré
            </Text>
          ) : (
            <ScrollArea type="auto">
              <Table fz="sm" horizontalSpacing={8}>
                <Table.Thead>
                  <Table.Tr>
                    <Table.Th>Voisin</Table.Th>
                    <Table.Th>AS</Table.Th>
                    <Table.Th>État</Table.Th>
                    <Table.Th>Depuis</Table.Th>
                    <Table.Th className="num">Préfixes reçus / envoyés</Table.Th>
                  </Table.Tr>
                </Table.Thead>
                <Table.Tbody>
                  {peers.map((p) => (
                    <Table.Tr key={`${p.afi}-${p.peer}`}>
                      <Table.Td>
                        <Text ff="monospace" size="sm">
                          {p.peer}
                        </Text>
                        <Text size="xs" c="dimmed">
                          {p.desc || p.hostname || ''}
                        </Text>
                      </Table.Td>
                      <Table.Td className="mono">{p.remote_as}</Table.Td>
                      <Table.Td>
                        <StateLabel value={p.state} good={['established']} />
                      </Table.Td>
                      <Table.Td className="mono">{p.uptime}</Table.Td>
                      <Table.Td className="num mono">
                        {p.pfx_rcd ?? 0} / {p.pfx_snt ?? 0}
                      </Table.Td>
                    </Table.Tr>
                  ))}
                </Table.Tbody>
              </Table>
            </ScrollArea>
          )}
        </SectionCard>
        <SectionCard title="Voisins ARP / NDP" count={neighbors.length}>
          {neighbors.length === 0 ? (
            <Text size="sm" c="dimmed">
              Aucune entrée ARP / NDP
            </Text>
          ) : (
            <ScrollArea type="auto">
              <Table fz="sm" horizontalSpacing={8}>
                <Table.Thead>
                  <Table.Tr>
                    <Table.Th>Adresse</Table.Th>
                    <Table.Th>Interface</Table.Th>
                    <Table.Th>MAC</Table.Th>
                    <Table.Th>État</Table.Th>
                  </Table.Tr>
                </Table.Thead>
                <Table.Tbody>
                  {neighbors.map((n) => (
                    <Table.Tr key={`${n.ip}-${n.dev}`}>
                      <Table.Td ff="monospace" fz="xs">
                        {n.ip}
                      </Table.Td>
                      <Table.Td className="mono" fz="xs">
                        {n.dev}
                      </Table.Td>
                      <Table.Td ff="monospace" fz="xs">
                        {n.mac ?? '—'}
                      </Table.Td>
                      <Table.Td fz="xs" className="mono" c="dimmed">
                        {n.state}
                      </Table.Td>
                    </Table.Tr>
                  ))}
                </Table.Tbody>
              </Table>
            </ScrollArea>
          )}
        </SectionCard>
      </SimpleGrid>

      <SectionCard title="Table de routage IPv4" count={routes.length}>
        {routes.length === 0 ? (
          <Text size="sm" c="dimmed">
            Aucune route lue dans FRR
          </Text>
        ) : (
          <ScrollArea type="auto">
            <Table fz="sm" highlightOnHover miw={720}>
              <Table.Thead>
                <Table.Tr>
                  <Table.Th>Préfixe</Table.Th>
                  <Table.Th>Protocole</Table.Th>
                  <Table.Th>Next-hop</Table.Th>
                  <Table.Th>Sélectionnée / FIB</Table.Th>
                  <Table.Th>Depuis</Table.Th>
                </Table.Tr>
              </Table.Thead>
              <Table.Tbody>
                {routes.map((r) => (
                  <Table.Tr key={`${r.prefix}-${r.protocol}`}>
                    <Table.Td ff="monospace">{r.prefix}</Table.Td>
                    <Table.Td ff="monospace" fz="xs" c="dimmed">
                      {r.protocol}
                    </Table.Td>
                    <Table.Td ff="monospace" fz="xs">
                      {r.nexthops.map((n) => (n.ip ? `${n.ip} via ${n.interface ?? '?'}` : `direct ${n.interface ?? ''}`)).join(' · ')}
                    </Table.Td>
                    <Table.Td>
                      {r.selected ? 'oui' : 'non'} / {r.installed ? 'oui' : 'non'}
                    </Table.Td>
                    <Table.Td ff="monospace" fz="xs" c="dimmed">
                      {r.uptime ?? ''}
                    </Table.Td>
                  </Table.Tr>
                ))}
              </Table.Tbody>
            </Table>
          </ScrollArea>
        )}
      </SectionCard>

      {(st.lldp?.length ?? 0) > 0 && (
        <SectionCard title="Voisins LLDP" count={st.lldp!.length}>
          <ScrollArea type="auto">
            <Table fz="sm">
              <Table.Thead>
                <Table.Tr>
                  <Table.Th>Port local</Table.Th>
                  <Table.Th>Système distant</Table.Th>
                  <Table.Th>ID de port distant</Table.Th>
                  <Table.Th>Description du port</Table.Th>
                </Table.Tr>
              </Table.Thead>
              <Table.Tbody>
                {st.lldp!.map((l) => (
                  <Table.Tr key={l.local_port}>
                    <Table.Td ff="monospace">{l.local_port}</Table.Td>
                    <Table.Td>{l.system_name}</Table.Td>
                    <Table.Td ff="monospace">{l.port_id}</Table.Td>
                    <Table.Td>{l.port_desc}</Table.Td>
                  </Table.Tr>
                ))}
              </Table.Tbody>
            </Table>
          </ScrollArea>
        </SectionCard>
      )}

      <Accordion variant="contained" radius="md" defaultValue={failed.length ? 'services' : null}>
        <Accordion.Item value="services">
          <Accordion.Control>
            <Group gap={8} wrap="wrap">
              <Text span fw={500}>
                Services SONiC (supervisord)
              </Text>
              <Text span size="xs" className="mono muted">
                {stateCounts.map(([state, n]) => `${n} ${state}`).join(' · ')}
              </Text>
              {failed.length > 0 && (
                <Text span size="sm" c="var(--state-err)">
                  {plural(failed.length, 'service en échec', 'services en échec')} : {failed.map((s) => s.name).join(', ')}
                </Text>
              )}
            </Group>
          </Accordion.Control>
          <Accordion.Panel>
            <SimpleGrid cols={{ base: 1, sm: 2, lg: 3 }} spacing={16} verticalSpacing={4}>
              {sortedServices.map((s) => {
                const tone = SERVICE_TONES[s.state] ?? 'off';
                return (
                  <Group key={s.name} gap={8} wrap="nowrap">
                    <span className="state-label" style={{ minWidth: 88 }}>
                      <Led tone={tone} />
                      <Text span fz="xs" className="mono" c={FAILED.has(s.state) ? `var(--state-${tone})` : 'dimmed'}>
                        {s.state}
                      </Text>
                    </span>
                    <Text size="sm" ff="monospace" fw={key.has(s.name) ? 600 : 400}>
                      {s.name}
                    </Text>
                    <Text size="xs" c="dimmed" truncate title={s.detail}>
                      {s.detail}
                    </Text>
                  </Group>
                );
              })}
            </SimpleGrid>
            <Text size="xs" c="dimmed" mt="sm">
              En gras : services surveillés pour l'état « prêt ». EXITED : programme de démarrage terminé (normal). STOPPED : non
              lancé dans cette image. snmp attend une communauté (non configurée dans ce lab).
            </Text>
          </Accordion.Panel>
        </Accordion.Item>
      </Accordion>
    </Stack>
  );
}
