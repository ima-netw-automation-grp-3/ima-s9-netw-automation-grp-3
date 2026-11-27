import { useMemo, useState } from 'react';
import { Button, Group, Loader, Modal, Stack, Switch, Table, Text, Tooltip } from '@mantine/core';
import { IconNetwork } from '@tabler/icons-react';
import { useMutation } from '@tanstack/react-query';

import { api } from '../api';
import type { ConnectivityCell, ConnectivityResult, ConnectivityTarget } from '../types';
import { notifyError } from '../utils';
import { Led } from './common';

const STICKY = { position: 'sticky', left: 0, zIndex: 1, background: 'var(--mantine-color-body)' } as const;

function Cell({ r }: { r: ConnectivityCell }) {
  let content;
  if (r.status === 'na') {
    content = (
      <span className="state-label">
        <Led tone="off" />
        <Text span c="dimmed" fz="xs">
          non routé
        </Text>
      </span>
    );
  } else if (r.status === 'ok') {
    const lossy = r.loss !== null && r.loss > 0;
    content = (
      <span className="state-label">
        <Led tone={lossy ? 'warn' : 'ok'} />
        <span className="mono">{r.rtt_avg?.toFixed(2) ?? '?'} ms</span>
        {lossy && <Text span fz="xs" c="var(--state-warn)">({r.loss} %)</Text>}
      </span>
    );
  } else {
    content = (
      <span className="state-label">
        <Led tone="err" />
        <span style={{ color: 'var(--state-err)' }}>échec{r.loss !== null ? ` (${r.loss} %)` : ''}</span>
      </span>
    );
  }
  return (
    <Tooltip label={<pre style={{ margin: 0, fontSize: 12 }}>{r.output}</pre>} maw={560} multiline>
      {content}
    </Tooltip>
  );
}

function TargetHeader({ t }: { t: ConnectivityTarget }) {
  const label = t.addresses.map((a) => `${a.ip} (${a.interface})`).join('\n');
  return (
    <Tooltip label={<pre style={{ margin: 0, fontSize: 12 }}>{label}</pre>} multiline>
      <div>
        <Text fw={500} fz="sm" c="var(--mantine-color-text)" style={{ whiteSpace: 'nowrap' }}>
          {t.node}
        </Text>
        <Text fz="xs" className="mono muted" fw={400} style={{ whiteSpace: 'nowrap' }}>
          {t.addresses.length > 1 ? `${t.addresses.length} adresses` : t.addresses[0]?.ip}
        </Text>
      </div>
    </Tooltip>
  );
}

function Matrix({ data, showUnrouted }: { data: ConnectivityResult; showUnrouted: boolean }) {
  const cells = useMemo(() => new Map(data.results.map((r) => [`${r.source}\u0000${r.node}`, r])), [data]);
  // Cibles qu'aucune source ne peut joindre par le lab (ex. loopbacks d'underlay vues depuis des VRF)
  const targets = useMemo(
    () =>
      showUnrouted
        ? data.targets
        : data.targets.filter((t) => data.results.some((r) => r.node === t.node && r.status !== 'na')),
    [data, showUnrouted],
  );
  if (!data.sources.length) {
    return <Text c="dimmed">Aucun client prêt : impossible de tester la connectivité</Text>;
  }
  if (!targets.length) {
    return <Text c="dimmed">Aucune cible routée par le lab depuis les clients</Text>;
  }
  return (
    <Table.ScrollContainer minWidth={0} maxHeight="70vh">
      <Table withTableBorder withColumnBorders stickyHeader fz="sm">
        <Table.Thead>
          <Table.Tr>
            <Table.Th style={{ ...STICKY, zIndex: 3 }}>source ↓ / cible →</Table.Th>
            {targets.map((t) => (
              <Table.Th key={t.node}>
                <TargetHeader t={t} />
              </Table.Th>
            ))}
          </Table.Tr>
        </Table.Thead>
        <Table.Tbody>
          {data.sources.map((s) => (
            <Table.Tr key={s}>
              <Table.Td fw={500} style={{ ...STICKY, whiteSpace: 'nowrap' }}>
                {s}
              </Table.Td>
              {targets.map((t) => {
                const r = cells.get(`${s}\u0000${t.node}`);
                return (
                  <Table.Td key={t.node} c={t.node === s ? 'dimmed' : undefined} style={{ whiteSpace: 'nowrap' }}>
                    {t.node === s ? '—' : r ? <Cell r={r} /> : null}
                  </Table.Td>
                );
              })}
            </Table.Tr>
          ))}
        </Table.Tbody>
      </Table>
    </Table.ScrollContainer>
  );
}

function Totals({ data }: { data: ConnectivityResult }) {
  const { ok, fail, na } = data.totals;
  return (
    <Group gap="lg" fz="sm">
      <span className="state-label">
        <Led tone="ok" /> {ok} joignable{ok > 1 ? 's' : ''}
      </span>
      <span className="state-label">
        <Led tone={fail ? 'err' : 'off'} />
        <span style={fail ? { color: 'var(--state-err)' } : undefined}>
          {fail} en échec
        </span>
      </span>
      <span className="state-label">
        <Led tone="off" />
        <Text span c="dimmed" fz="sm">
          {na} non routée{na > 1 ? 's' : ''} (segmentation, rien n'est pingué)
        </Text>
      </span>
    </Group>
  );
}

export function ConnectivityButton({ variant = 'filled' }: { variant?: string }) {
  const [opened, setOpened] = useState(false);
  const [showUnrouted, setShowUnrouted] = useState(false);
  const test = useMutation({ mutationFn: api.connectivity, onError: (e) => notifyError(e, 'Test de connectivité') });
  return (
    <>
      <Button
        variant={variant}
        leftSection={<IconNetwork size={16} />}
        onClick={() => {
          setOpened(true);
          test.mutate();
        }}
      >
        Tester la connectivité
      </Button>
      <Modal opened={opened} onClose={() => setOpened(false)} title="Matrice de connectivité (ping)" size="auto">
        <Stack>
          <Text size="sm" c="dimmed" maw={900}>
            Chaque client pingue les autres clients et la Loopback0 de chaque routeur (2 paquets). Seules les
            adresses que sa table de routage envoie dans le lab sont essayées : une cible joignable uniquement par
            le réseau de management (autre VRF, autre plateforme) est « non routée », pas en échec. Pour un client
            à plusieurs adresses, la case est verte dès que l'une répond (détail au survol).
          </Text>
          {test.data && (
            <Group justify="space-between">
              <Totals data={test.data} />
              <Switch
                size="xs"
                label="Afficher les cibles non routées"
                checked={showUnrouted}
                onChange={(e) => setShowUnrouted(e.currentTarget.checked)}
              />
            </Group>
          )}
          {test.isPending && <Loader size="sm" />}
          {test.data && <Matrix data={test.data} showUnrouted={showUnrouted} />}
          <Button variant="default" onClick={() => test.mutate()} loading={test.isPending} w="fit-content">
            Relancer le test
          </Button>
        </Stack>
      </Modal>
    </>
  );
}
