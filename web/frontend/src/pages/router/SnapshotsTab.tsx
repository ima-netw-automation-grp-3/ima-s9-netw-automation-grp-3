import { useState } from 'react';
import {
  ActionIcon,
  Alert,
  Badge,
  Button,
  Code,
  Group,
  Loader,
  Modal,
  ScrollArea,
  Stack,
  Table,
  Tabs,
  Text,
  TextInput,
  Tooltip,
} from '@mantine/core';
import { modals } from '@mantine/modals';
import { IconCamera, IconDeviceFloppy, IconEye, IconGitCompare, IconRestore, IconTrash } from '@tabler/icons-react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import { api } from '../../api';
import { CodeEditor } from '../../components/CodeEditor';
import { ConfigDiffModal } from '../../components/ConfigDiff';
import { SectionCard } from '../../components/common';
import type { RestoreResult } from '../../types';
import { useLab } from '../../hooks';
import { downloadText, errorMessage, formatDate, notifyError, notifySave, notifySuccess, saveTarget } from '../../utils';

function SnapshotViewer({ node, id, onClose }: { node: string; id: string | null; onClose: () => void }) {
  const q = useQuery({ queryKey: ['snapshot', node, id], queryFn: () => api.getSnapshot(node, id!), enabled: id !== null });
  return (
    <Modal opened={id !== null} onClose={onClose} size="xl" title={`Snapshot ${id ?? ''}`}>
      {q.isLoading && <Loader size="sm" />}
      {q.isError && <Alert color="red">{errorMessage(q.error)}</Alert>}
      {q.data && (
        <Tabs defaultValue={q.data.domains[0]}>
          <Tabs.List>
            {q.data.domains.map((d) => (
              <Tabs.Tab key={d} value={d}>
                {d === 'configdb' ? 'CONFIG_DB' : 'FRR'}
              </Tabs.Tab>
            ))}
          </Tabs.List>
          {q.data.domains.map((d) => (
            <Tabs.Panel key={d} value={d} pt="sm">
              <Group justify="flex-end" mb={8}>
                <Button
                  size="xs"
                  variant="default"
                  onClick={() => downloadText(`${node}-${id}-${d === 'frr' ? 'frr.conf' : 'config_db.json'}`, q.data!.contents?.[d] ?? '')}
                >
                  Télécharger
                </Button>
              </Group>
              <CodeEditor value={q.data!.contents?.[d] ?? ''} language={d === 'configdb' ? 'json' : 'text'} height={480} readOnly />
            </Tabs.Panel>
          ))}
        </Tabs>
      )}
    </Modal>
  );
}

export function SnapshotsTab({ name }: { name: string }) {
  const qc = useQueryClient();
  const { data: lab } = useLab();
  const [label, setLabel] = useState('');
  const [viewing, setViewing] = useState<string | null>(null);
  const [compare, setCompare] = useState<string | null>(null);
  const list = useQuery({ queryKey: ['snapshots', name], queryFn: () => api.snapshots(name) });

  const afterRestore = (r: RestoreResult) => {
    qc.invalidateQueries({ queryKey: ['diff', name] });
    qc.invalidateQueries({ queryKey: ['node', name] });
    qc.invalidateQueries({ queryKey: ['lab'] });
    if (r.ok) notifySuccess(`Configuration restaurée depuis ${r.source}`, 'Restauration');
    else notifyError(new Error('certaines opérations ont échoué'), 'Restauration');
  };
  const create = useMutation({
    mutationFn: () => api.createSnapshot(name, label.trim()),
    onSuccess: (s) => {
      notifySuccess(`Snapshot ${s.id} créé dans lab/backups/${name}/`, 'Snapshot');
      setLabel('');
      qc.invalidateQueries({ queryKey: ['snapshots', name] });
    },
    onError: (e) => notifyError(e, 'Snapshot'),
  });
  const remove = useMutation({
    mutationFn: (id: string) => api.deleteSnapshot(name, id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['snapshots', name] }),
    onError: (e) => notifyError(e, 'Suppression'),
  });
  const restore = useMutation({
    mutationFn: (source: string) => api.restore(name, source),
    onSuccess: afterRestore,
    onError: (e) => notifyError(e, 'Restauration'),
  });
  const save = useMutation({
    mutationFn: () => api.save(name),
    onSuccess: (r) => {
      notifySave(r, 'Sauvegarde');
      qc.invalidateQueries({ queryKey: ['diff', name] });
    },
    onError: (e) => notifyError(e, 'Sauvegarde'),
  });

  const confirmRestore = (source: string, what: string) =>
    modals.openConfirmModal({
      title: 'Restaurer la configuration',
      children: (
        <Text size="sm">
          {what} remplace la conf running de <b>{name}</b> (mode replace : CONFIG_DB puis FRR). Les sessions BGP peuvent
          être réinitialisées.
        </Text>
      ),
      labels: { confirm: 'Restaurer', cancel: 'Annuler' },
      confirmProps: { color: 'orange.6' },
      onConfirm: () => restore.mutate(source),
    });

  const snapshots = list.data?.snapshots ?? [];
  return (
    <Stack gap="lg">
      <SectionCard title="Conf de démarrage">
        <Text size="sm" mb="md" c="dimmed">
          Fichiers <Code>lab/configs/{name}/config_db.json</Code> et <Code>frr.conf</Code>, chargés au déploiement du lab
          (CONFIG_DB via <Code>/var/sonic/config_db.json</Code>, FRR via le bootstrap).
        </Text>
        <Group gap="xs">
          <Button variant="default" leftSection={<IconGitCompare size={16} />} onClick={() => setCompare('startup')}>
            Comparer avec la running
          </Button>
          <Button
            variant="default"
            leftSection={<IconRestore size={16} />}
            loading={restore.isPending && restore.variables === 'startup'}
            onClick={() => confirmRestore('startup', 'La conf de démarrage')}
          >
            Restaurer la conf de démarrage
          </Button>
          <Button
            variant="default"
            leftSection={<IconDeviceFloppy size={16} />}
            loading={save.isPending}
            onClick={() =>
              modals.openConfirmModal({
                title: 'Sauvegarder running → startup',
                children: <Text size="sm">{saveTarget(lab?.gitops, `La conf running de ${name}`)}</Text>,
                labels: { confirm: 'Sauvegarder', cancel: 'Annuler' },
                onConfirm: () => save.mutate(),
              })
            }
          >
            Sauvegarder running → startup
          </Button>
        </Group>
      </SectionCard>

      <SectionCard title="Snapshots" count={snapshots.length}>
        <Group align="flex-end" mb="md" gap="sm">
          <TextInput
            label="Nouveau snapshot de la running"
            placeholder="Libellé facultatif, ex. avant test OSPF"
            w={360}
            maw="100%"
            value={label}
            maxLength={80}
            onChange={(e) => setLabel(e.currentTarget.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') create.mutate();
            }}
          />
          <Button leftSection={<IconCamera size={16} />} loading={create.isPending} onClick={() => create.mutate()}>
            Créer le snapshot
          </Button>
        </Group>
        {snapshots.length === 0 ? (
          <Text c="dimmed" size="sm">
            Aucun snapshot. Ils sont stockés dans <Code>lab/backups/{name}/</Code>.
          </Text>
        ) : (
          <ScrollArea type="auto">
          <Table highlightOnHover fz="sm" miw={720}>
            <Table.Thead>
              <Table.Tr>
                <Table.Th>Date</Table.Th>
                <Table.Th>Libellé</Table.Th>
                <Table.Th>Contenu</Table.Th>
                <Table.Th>Identifiant</Table.Th>
                <Table.Th w={136}>Actions</Table.Th>
              </Table.Tr>
            </Table.Thead>
            <Table.Tbody>
              {snapshots.map((s) => (
                <Table.Tr key={s.id}>
                  <Table.Td className="mono" fz="xs">
                    {formatDate(s.created)}
                  </Table.Td>
                  <Table.Td>{s.label || <Text c="dimmed" size="sm">—</Text>}</Table.Td>
                  <Table.Td>
                    <Group gap={4}>
                      {s.domains.map((d) => (
                        <Badge key={d} size="sm" ff="monospace">
                          {d === 'configdb' ? 'CONFIG_DB' : 'FRR'}
                        </Badge>
                      ))}
                    </Group>
                  </Table.Td>
                  <Table.Td ff="monospace" fz="xs">
                    {s.id}
                  </Table.Td>
                  <Table.Td>
                    <Group gap={4} wrap="nowrap">
                      <Tooltip label="Voir">
                        <ActionIcon aria-label={`Voir ${s.id}`} onClick={() => setViewing(s.id)}>
                          <IconEye size={16} />
                        </ActionIcon>
                      </Tooltip>
                      <Tooltip label="Comparer avec la running">
                        <ActionIcon aria-label={`Comparer ${s.id} avec la running`} onClick={() => setCompare(`snapshot:${s.id}`)}>
                          <IconGitCompare size={16} />
                        </ActionIcon>
                      </Tooltip>
                      <Tooltip label="Restaurer (replace)">
                        <ActionIcon
                          aria-label={`Restaurer ${s.id}`}
                          loading={restore.isPending && restore.variables === `snapshot:${s.id}`}
                          onClick={() => confirmRestore(`snapshot:${s.id}`, `Le snapshot ${s.label ? `« ${s.label} »` : s.id}`)}
                        >
                          <IconRestore size={16} />
                        </ActionIcon>
                      </Tooltip>
                      <Tooltip label="Supprimer">
                        <ActionIcon
                          aria-label={`Supprimer ${s.id}`}
                          onClick={() =>
                            modals.openConfirmModal({
                              title: 'Supprimer le snapshot',
                              children: <Text size="sm">Supprimer définitivement {s.id}{'\u00a0'}?</Text>,
                              labels: { confirm: 'Supprimer', cancel: 'Annuler' },
                              confirmProps: { color: 'red.9' },
                              onConfirm: () => remove.mutate(s.id),
                            })
                          }
                        >
                          <IconTrash size={16} />
                        </ActionIcon>
                      </Tooltip>
                    </Group>
                  </Table.Td>
                </Table.Tr>
              ))}
            </Table.Tbody>
          </Table>
          </ScrollArea>
        )}
      </SectionCard>

      <SnapshotViewer node={name} id={viewing} onClose={() => setViewing(null)} />
      <ConfigDiffModal node={name} against={compare} onClose={() => setCompare(null)} />
    </Stack>
  );
}
