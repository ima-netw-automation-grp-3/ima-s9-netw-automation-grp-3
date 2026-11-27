import { Alert, Group, Loader, Modal, Stack, Text } from '@mantine/core';
import { useQuery } from '@tanstack/react-query';

import { api } from '../api';
import type { DiffResult } from '../types';
import { errorMessage, plural } from '../utils';
import { ChangesTable, DiffView, Led } from './common';

export function DiffResultView({ diff }: { diff: DiffResult }) {
  return (
    <Stack>
      <Group gap="md">
        <span className="state-label">
          <Led tone={diff.dirty ? 'warn' : 'ok'} />
          <Text span size="sm" c={diff.dirty ? 'var(--state-warn)' : undefined}>
            {diff.dirty ? 'différences détectées' : 'identiques'}
          </Text>
        </span>
        <Text size="sm" c="dimmed">
          référence : <span className="mono">{diff.against}</span> → running
        </Text>
      </Group>
      <Text fw={500}>
        CONFIG_DB{' '}
        <Text span size="xs" className="mono muted" fw={400}>
          {plural(diff.configdb.count, 'changement', 'changements')}
        </Text>
      </Text>
      <ChangesTable changes={diff.configdb.changes} />
      {diff.configdb.diff && <DiffView diff={diff.configdb.diff} maxHeight={260} />}
      <Text fw={500}>FRR</Text>
      <DiffView diff={diff.frr.diff} maxHeight={300} />
    </Stack>
  );
}

/** Fenêtre de comparaison running ↔ startup ou snapshot. */
export function ConfigDiffModal({
  node,
  against,
  onClose,
}: {
  node: string;
  against: string | null;
  onClose: () => void;
}) {
  const q = useQuery({
    queryKey: ['diff', node, against],
    queryFn: () => api.diff(node, against ?? 'startup'),
    enabled: against !== null,
  });
  return (
    <Modal opened={against !== null} onClose={onClose} size="xl" title={`Comparaison : ${against ?? ''} → running`}>
      {q.isLoading && <Loader size="sm" />}
      {q.isError && <Alert color="red">{errorMessage(q.error)}</Alert>}
      {q.data && <DiffResultView diff={q.data} />}
    </Modal>
  );
}
