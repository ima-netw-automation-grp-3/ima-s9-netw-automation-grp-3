import { useState } from 'react';
import { Alert, Button, Card, Group, Loader, NumberInput, ScrollArea, SimpleGrid, Stack, Table, Text, TextInput } from '@mantine/core';
import { IconRoute, IconWaveSine } from '@tabler/icons-react';
import { useMutation } from '@tanstack/react-query';

import { api } from '../../api';
import { ConnectivityButton } from '../../components/ConnectivityModal';
import { Led, OutputBlock, SectionCard, StaleDataAlert, StateLabel } from '../../components/common';
import { useLab, useNodeStatus, useRates } from '../../hooks';
import type { ClientStatus, ExecResult, LabState } from '../../types';
import { bareIp, errorMessage, formatCounters, formatRate, notifyError, plural } from '../../utils';

export function ClientStatusTab({ name, active }: { name: string; active: boolean }) {
  const { data, isLoading, error, dataUpdatedAt } = useNodeStatus(name, active);
  const { error: labError } = useLab();
  const st = data as ClientStatus | undefined;
  const rates = useRates(st?.interfaces, undefined);
  if (isLoading) return <Loader />;
  if (!st) return <Alert color="red" title="État illisible">{errorMessage(error)}</Alert>;
  if (st.phase === 'down') return <Alert color="gray">Le client n'est pas démarré : {st.detail}</Alert>;
  const ifaces = Object.entries(st.interfaces ?? {});
  const routes = st.routes ?? [];
  return (
    <Stack gap="lg">
      {/* si l'API entière ne répond plus, l'alerte globale suffit */}
      {error && !labError && <StaleDataAlert error={error} updatedAt={dataUpdatedAt} />}
      {st.detail && <Alert color="blue">{st.detail}</Alert>}
      <SectionCard title="Interfaces" count={ifaces.length}>
        <ScrollArea type="auto">
          <Table fz="sm" highlightOnHover miw={860}>
            <Table.Thead>
              <Table.Tr>
                <Table.Th>Interface</Table.Th>
                <Table.Th>État</Table.Th>
                <Table.Th className="num">MTU</Table.Th>
                <Table.Th>MAC</Table.Th>
                <Table.Th>Adresses</Table.Th>
                <Table.Th className="num nowrap">RX / TX</Table.Th>
                <Table.Th className="num nowrap">Débit RX / TX</Table.Th>
              </Table.Tr>
            </Table.Thead>
            <Table.Tbody>
              {ifaces.map(([n, i]) => (
                <Table.Tr key={n}>
                  <Table.Td ff="monospace" fw={500}>
                    {n}
                    {n === 'eth0' && (
                      <Text span className="muted" fz="xs" fw={400}>
                        {' '}
                        (mgmt)
                      </Text>
                    )}
                  </Table.Td>
                  <Table.Td>
                    <StateLabel value={i.up ? 'up' : 'down'} />
                  </Table.Td>
                  <Table.Td className="num mono">{i.mtu ?? '—'}</Table.Td>
                  <Table.Td ff="monospace" fz="xs">
                    {i.mac ?? '—'}
                  </Table.Td>
                  <Table.Td ff="monospace" fz="xs">
                    {i.addresses.join(', ') || '—'}
                  </Table.Td>
                  <Table.Td className="num mono" fz="xs">
                    {formatCounters(i.stats)}
                  </Table.Td>
                  <Table.Td className="num mono" fz="xs">
                    {formatRate(rates[n]?.rx)} / {formatRate(rates[n]?.tx)}
                  </Table.Td>
                </Table.Tr>
              ))}
            </Table.Tbody>
          </Table>
        </ScrollArea>
      </SectionCard>
      <SectionCard title="Table de routage" count={routes.length}>
        {routes.length === 0 ? (
          <Text size="sm" c="dimmed">
            Aucune route
          </Text>
        ) : (
          <ScrollArea type="auto">
            <Table fz="sm" highlightOnHover miw={560}>
              <Table.Thead>
                <Table.Tr>
                  <Table.Th>Destination</Table.Th>
                  <Table.Th>Passerelle</Table.Th>
                  <Table.Th>Interface</Table.Th>
                  <Table.Th>Source</Table.Th>
                </Table.Tr>
              </Table.Thead>
              <Table.Tbody>
                {routes.map((r) => (
                  <Table.Tr key={`${r.prefix}-${r.dev}`}>
                    <Table.Td ff="monospace">{r.prefix}</Table.Td>
                    <Table.Td ff="monospace" c={r.via ? undefined : 'dimmed'}>
                      {r.via ?? 'directe'}
                    </Table.Td>
                    <Table.Td className="mono">{r.dev}</Table.Td>
                    <Table.Td ff="monospace">{r.src ?? '—'}</Table.Td>
                  </Table.Tr>
                ))}
              </Table.Tbody>
            </Table>
          </ScrollArea>
        )}
      </SectionCard>
    </Stack>
  );
}

/** Nombre de sauts d'une sortie traceroute (lignes « N  adresse … »). */
const hops = (output: string) => output.split('\n').filter((l) => /^\s*\d+\s/.test(l)).length;

interface Run {
  id: number;
  kind: 'ping' | 'traceroute';
  target: string;
  result: ExecResult;
}

/** Bilan d'un test : LED et chiffres ; un échec colore son texte. */
function RunSummary({ run }: { run: Run }) {
  const { result } = run;
  const s = result.summary;
  let ok: boolean;
  let text: string;
  if (run.kind === 'ping' && s) {
    ok = s.ok;
    text = ok ? `${s.received}/${s.transmitted} reçus · ${s.rtt_avg ?? '?'} ms` : `échec (${s.loss ?? 100} % de perte)`;
  } else {
    ok = result.exit_code === 0;
    text = ok ? plural(hops(result.output ?? result.stdout), 'saut', 'sauts') : `échec (code ${result.exit_code})`;
  }
  return (
    <span className="state-label">
      <Led tone={ok ? 'ok' : 'err'} />
      <Text span size="sm" className="mono" c={ok ? 'dimmed' : 'var(--state-err)'}>
        {text}
      </Text>
    </span>
  );
}

export function TestsTab({ name, lab }: { name: string; lab: LabState }) {
  const [target, setTarget] = useState('');
  const [count, setCount] = useState<number | string>(3);
  const [runs, setRuns] = useState<Run[]>([]);
  const add = (kind: Run['kind'], t: string) => (result: ExecResult) =>
    setRuns((r) => [{ id: Date.now(), kind, target: t, result }, ...r].slice(0, 10));
  const ping = useMutation({
    mutationFn: (t: string) => api.ping(name, t, Number(count) || 3),
    onSuccess: (res, t) => add('ping', t)(res),
    onError: (e) => notifyError(e, 'Ping'),
  });
  const trace = useMutation({
    mutationFn: (t: string) => api.traceroute(name, t),
    onSuccess: (res, t) => add('traceroute', t)(res),
    onError: (e) => notifyError(e, 'Traceroute'),
  });

  const targets = lab.nodes
    .filter((n) => n.name !== name)
    .flatMap((n) => Object.entries(n.addresses).map(([iface, addrs]) => ({ node: n.name, iface, ip: bareIp(addrs[0]) })));

  return (
    <Stack gap="lg">
      <Card>
        <Stack gap="md">
          <Group align="flex-end" gap="sm">
            <TextInput
              label="Cible"
              placeholder="adresse IP"
              w={240}
              styles={{ input: { fontFamily: 'var(--mantine-font-family-monospace)' } }}
              value={target}
              onChange={(e) => setTarget(e.currentTarget.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter' && target) ping.mutate(target.trim());
              }}
            />
            <NumberInput label="Paquets" w={100} min={1} max={20} value={count} onChange={setCount} />
            <Button leftSection={<IconWaveSine size={16} />} loading={ping.isPending} disabled={!target} onClick={() => ping.mutate(target.trim())}>
              Ping
            </Button>
            <Button
              variant="default"
              leftSection={<IconRoute size={16} />}
              loading={trace.isPending}
              disabled={!target}
              onClick={() => trace.mutate(target.trim())}
            >
              Traceroute
            </Button>
            <ConnectivityButton variant="default" />
          </Group>
          <Group gap={8}>
            <Text size="xs" c="dimmed">
              Cibles du lab
            </Text>
            {targets.map((t) => (
              <button
                key={`${t.node}-${t.iface}`}
                type="button"
                className="chip"
                data-selected={target.trim() === t.ip || undefined}
                aria-pressed={target.trim() === t.ip}
                onClick={() => setTarget(t.ip)}
              >
                <span>
                  {t.node} <span className="chip-meta">{t.iface}</span>
                </span>
                <span>{t.ip}</span>
              </button>
            ))}
          </Group>
        </Stack>
      </Card>
      {runs.length === 0 && (
        <Text c="dimmed" size="sm">
          Lancez un ping ou un traceroute depuis {name} : les résultats s'affichent ici
        </Text>
      )}
      <SimpleGrid cols={{ base: 1, xl: 2 }} spacing="md">
        {runs.map((r) => (
          <Card key={r.id} padding="md">
            <Group justify="space-between" mb={8} gap="sm">
              <Text size="sm" className="mono">
                {r.kind} {r.target}
              </Text>
              <RunSummary run={r} />
            </Group>
            <OutputBlock text={r.result.output ?? `${r.result.stdout}${r.result.stderr}`} maxHeight={260} />
          </Card>
        ))}
      </SimpleGrid>
    </Stack>
  );
}
