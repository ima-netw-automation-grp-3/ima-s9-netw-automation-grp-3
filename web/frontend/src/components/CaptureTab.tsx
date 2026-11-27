import { useEffect, useRef, useState } from 'react';
import { useSearchParams } from 'react-router';
import { ActionIcon, Alert, Box, Button, Center, Group, Loader, Select, Stack, Table, Text, TextInput, Tooltip } from '@mantine/core';
import { IconDownload, IconExternalLink, IconPlayerStop, IconRefresh, IconTrash } from '@tabler/icons-react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import { api } from '../api';
import { POLL_MS } from '../hooks';
import type { Capture } from '../types';
import { errorMessage, formatBytes, formatDuration, notifyError } from '../utils';
import { Led, SectionCard } from './common';

/** Interface proposée par défaut : le premier port de données (eth1), jamais l'interface de management. */
function defaultInterface(interfaces: string[]): string | null {
  return interfaces.find((i) => i === 'eth1') ?? interfaces.find((i) => i !== 'eth0' && i !== 'lo') ?? interfaces[0] ?? null;
}

const time = (ts: number) => new Date(ts * 1000).toLocaleTimeString('fr-FR');

/** Heure courante, rafraîchie chaque seconde tant que `enabled` (durée des captures en cours). */
function useNow(enabled: boolean): number {
  const [now, setNow] = useState(() => Date.now() / 1000);
  useEffect(() => {
    if (!enabled) return;
    setNow(Date.now() / 1000);
    const t = window.setInterval(() => setNow(Date.now() / 1000), 1000);
    return () => window.clearInterval(t);
  }, [enabled]);
  return now;
}

/**
 * Capture de paquets d'un nœud : une sonde dumpcap dans son réseau écrit un pcap, et un vrai Wireshark
 * (dans le navigateur) le lit en direct. Le visionneur reste monté quand on change d'onglet : la session
 * Wireshark est aussi conservée côté serveur, on la retrouve en revenant.
 */
export function CaptureTab({ name, active, height }: { name: string; active: boolean; height: string }) {
  const qc = useQueryClient();
  const [iface, setIface] = useState<string | null>(null);
  const [filter, setFilter] = useState('');
  const [selected, setSelected] = useState<string | null>(null);
  const [reload, setReload] = useState(0);
  const [params, setParams] = useSearchParams();

  const interfaces = useQuery({ queryKey: ['capture-interfaces', name], queryFn: () => api.captureInterfaces(name), enabled: active });
  const list = useQuery({
    queryKey: ['captures'],
    queryFn: api.captures,
    // une visionneuse qui démarre est suivie de près, pour l'afficher dès qu'elle répond
    refetchInterval: (q) =>
      !active ? false : q.state.data?.captures.some((c) => c.node === name && c.viewer === 'starting') ? 1000 : POLL_MS,
  });
  const options = interfaces.data?.interfaces ?? [];
  useEffect(() => {
    if (iface === null && options.length) setIface(defaultInterface(options));
  }, [iface, options]);

  const mine = (list.data?.captures ?? []).filter((c) => c.node === name);
  const current = mine.find((c) => c.id === selected) ?? null;
  const now = useNow(active && mine.some((c) => c.state === 'running'));
  const refresh = () => qc.invalidateQueries({ queryKey: ['captures'] });

  const start = useMutation({
    mutationFn: (v: { iface: string; filter: string }) => api.startCapture(name, v.iface, v.filter),
    onSuccess: (c) => {
      setSelected(c.id);
      void refresh();
    },
    onError: (e) => notifyError(e, 'Capture'),
  });
  const stop = useMutation({ mutationFn: api.stopCapture, onSuccess: refresh, onError: (e) => notifyError(e, 'Capture') });
  const remove = useMutation({
    mutationFn: api.deleteCapture,
    onSuccess: (_, id) => {
      if (selected === id) setSelected(null);
      void refresh();
    },
    onError: (e) => notifyError(e, 'Capture'),
  });

  // clic droit sur la topologie (?iface=eth1) : reprend la capture sans filtre déjà en cours sur cette
  // interface, sinon en démarre une
  const wanted = params.get('iface');
  const handled = useRef<string | null>(null);
  useEffect(() => {
    if (!wanted || !active || !list.data || handled.current === wanted) return;
    handled.current = wanted;
    setParams(
      (p) => {
        p.delete('iface');
        return p;
      },
      { replace: true },
    );
    setIface(wanted);
    setFilter('');
    const existing = list.data.captures.find((c) => c.node === name && c.interface === wanted && c.state === 'running' && !c.filter);
    if (existing) setSelected(existing.id);
    else start.mutate({ iface: wanted, filter: '' });
  }, [wanted, active, list.data, name, setParams, start]);

  const full = (list.data?.captures.length ?? 0) >= (list.data?.max_sessions ?? Infinity);
  return (
    <Stack gap="md">
      <SectionCard title="Nouvelle capture" count={`${list.data?.captures.length ?? 0}/${list.data?.max_sessions ?? '–'}`}>
        <Group align="flex-end" gap="sm" wrap="wrap">
          <Select
            label="Interface"
            data={options}
            value={iface}
            onChange={setIface}
            allowDeselect={false}
            w={160}
            placeholder={interfaces.isLoading ? 'chargement…' : 'aucune'}
          />
          <TextInput
            label="Filtre de capture (facultatif)"
            placeholder="icmp or port 179"
            value={filter}
            onChange={(e) => setFilter(e.currentTarget.value)}
            w={280}
            maxLength={200}
          />
          <Button onClick={() => start.mutate({ iface: iface ?? '', filter })} loading={start.isPending} disabled={!iface || full}>
            Démarrer la capture
          </Button>
        </Group>
        <Text size="xs" c="dimmed" mt="xs">
          Capture en direct dans Wireshark, sans rien changer sur le nœud. Le premier lancement télécharge l'image Wireshark (quelques
          minutes). Un fichier pcap s'arrête à {list.data?.max_mb ?? '…'} Mo.
        </Text>
        {interfaces.error && (
          <Alert color="red" mt="sm" title="Interfaces indisponibles">
            {errorMessage(interfaces.error)}
          </Alert>
        )}
      </SectionCard>

      {mine.length > 0 && (
        <SectionCard title="Captures de ce nœud" count={mine.length}>
          <Table verticalSpacing={6} highlightOnHover>
            <Table.Thead>
              <Table.Tr>
                <Table.Th>État</Table.Th>
                <Table.Th>Interface</Table.Th>
                <Table.Th>Filtre</Table.Th>
                <Table.Th>Début</Table.Th>
                <Table.Th>Durée</Table.Th>
                <Table.Th>Taille</Table.Th>
                <Table.Th />
              </Table.Tr>
            </Table.Thead>
            <Table.Tbody>
              {mine.map((c) => (
                <CaptureRow
                  key={c.id}
                  capture={c}
                  now={now}
                  selected={c.id === selected}
                  onSelect={() => setSelected(c.id)}
                  onStop={() => stop.mutate(c.id)}
                  onDelete={() => remove.mutate(c.id)}
                />
              ))}
            </Table.Tbody>
          </Table>
        </SectionCard>
      )}

      {current && (
        <Box>
          <Group justify="space-between" mb="xs" gap="xs">
            <Text size="sm" fw={500}>
              Wireshark · <span className="mono">{current.interface}</span>
            </Text>
            <Group gap={4}>
              <Tooltip label="Recharger le visionneur">
                <ActionIcon aria-label="Recharger le visionneur" onClick={() => setReload((n) => n + 1)}>
                  <IconRefresh size={16} />
                </ActionIcon>
              </Tooltip>
              <Tooltip label="Ouvrir dans une nouvelle fenêtre">
                <ActionIcon component="a" href={current.url} target="_blank" rel="noreferrer" aria-label="Ouvrir dans une nouvelle fenêtre">
                  <IconExternalLink size={16} />
                </ActionIcon>
              </Tooltip>
            </Group>
          </Group>
          {current.viewer === 'ready' ? (
            <iframe key={`${current.id}-${reload}`} src={current.url} title={`Wireshark ${current.interface}`} className="capture-frame" style={{ height }} />
          ) : current.viewer === 'starting' ? (
            <Center className="capture-frame capture-wait" style={{ height }}>
              <Group gap="sm">
                <Loader size="sm" />
                <Text size="sm" c="dimmed">
                  Démarrage de Wireshark…
                </Text>
              </Group>
            </Center>
          ) : (
            <Alert color="orange" title="Visionneur arrêté">
              Le conteneur Wireshark de cette capture ne tourne plus. Supprimez la capture et relancez-en une.
            </Alert>
          )}
        </Box>
      )}
    </Stack>
  );
}

function CaptureRow({
  capture: c,
  now,
  selected,
  onSelect,
  onStop,
  onDelete,
}: {
  capture: Capture;
  now: number;
  selected: boolean;
  onSelect: () => void;
  onStop: () => void;
  onDelete: () => void;
}) {
  const running = c.state === 'running';
  return (
    <Table.Tr bg={selected ? 'var(--mantine-primary-color-light)' : undefined}>
      <Table.Td>
        <Group gap={6} wrap="nowrap">
          <Led tone={running ? 'ok' : 'off'} />
          <Text size="sm">{running ? 'en cours' : 'arrêtée'}</Text>
        </Group>
      </Table.Td>
      <Table.Td className="mono">{c.interface}</Table.Td>
      <Table.Td className="mono">{c.filter || '—'}</Table.Td>
      <Table.Td>{time(c.created)}</Table.Td>
      <Table.Td>{formatDuration((running ? now : (c.ended ?? now)) - c.created)}</Table.Td>
      <Table.Td>{formatBytes(c.size)}</Table.Td>
      <Table.Td>
        <Group gap={4} justify="flex-end" wrap="nowrap">
          <Button size="compact-xs" variant={selected ? 'filled' : 'light'} onClick={onSelect}>
            Ouvrir Wireshark
          </Button>
          <Tooltip label="Télécharger le .pcap">
            <ActionIcon component="a" href={c.download} download aria-label="Télécharger le .pcap">
              <IconDownload size={16} />
            </ActionIcon>
          </Tooltip>
          {running && (
            <Tooltip label="Arrêter la capture (le fichier est conservé)">
              <ActionIcon aria-label="Arrêter la capture" onClick={onStop}>
                <IconPlayerStop size={16} />
              </ActionIcon>
            </Tooltip>
          )}
          <Tooltip label="Supprimer la capture et son fichier">
            <ActionIcon color="red" variant="subtle" aria-label="Supprimer la capture" onClick={onDelete}>
              <IconTrash size={16} />
            </ActionIcon>
          </Tooltip>
        </Group>
      </Table.Td>
    </Table.Tr>
  );
}
