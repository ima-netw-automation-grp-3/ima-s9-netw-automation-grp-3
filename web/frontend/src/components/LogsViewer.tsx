import { useMemo, useState } from 'react';
import { ActionIcon, Box, Group, ScrollArea, Select, Switch, Text, TextInput, Tooltip } from '@mantine/core';
import { IconRefresh, IconSearch } from '@tabler/icons-react';
import { useQuery } from '@tanstack/react-query';

import { plural } from '../utils';
import { CopyIcon } from './common';

interface Props {
  queryKey: unknown[];
  fetchLogs: (lines: number) => Promise<{ logs: string }>;
  height?: number | string;
  defaultFollow?: boolean;
}

function lineClass(line: string): string {
  if (/\b(ERR|ERROR|CRIT|FATAL|failed|ÉCHEC|ERREUR)\b/i.test(line)) return 'log-err';
  if (/\b(WARNING|WARN)\b/i.test(line)) return 'log-warn';
  return '';
}

export function LogsViewer({ queryKey, fetchLogs, height = 520, defaultFollow = false }: Props) {
  const [lines, setLines] = useState('300');
  const [filter, setFilter] = useState('');
  const [follow, setFollow] = useState(defaultFollow);
  const q = useQuery({
    queryKey: [...queryKey, lines],
    queryFn: () => fetchLogs(Number(lines)),
    refetchInterval: follow ? 3000 : false,
  });
  const text = q.data?.logs ?? '';
  const shown = useMemo(() => {
    const all = text.split('\n');
    const f = filter.trim().toLowerCase();
    return f ? all.filter((l) => l.toLowerCase().includes(f)) : all;
  }, [text, filter]);

  return (
    <Box>
      <Group mb="xs" gap="sm" wrap="wrap">
        <Select
          size="xs"
          w={130}
          value={lines}
          onChange={(v) => setLines(v ?? '300')}
          data={['100', '300', '1000', '3000'].map((v) => ({ value: v, label: `${v} lignes` }))}
          allowDeselect={false}
        />
        <TextInput
          size="xs"
          w={240}
          leftSection={<IconSearch size={16} />}
          placeholder="Filtrer"
          value={filter}
          onChange={(e) => setFilter(e.currentTarget.value)}
        />
        <Switch size="xs" label="Suivi (3 s)" checked={follow} onChange={(e) => setFollow(e.currentTarget.checked)} />
        <Tooltip label="Rafraîchir">
          <ActionIcon aria-label="Rafraîchir" onClick={() => q.refetch()} loading={q.isFetching}>
            <IconRefresh size={16} />
          </ActionIcon>
        </Tooltip>
        <CopyIcon value={shown.join('\n')} />
        {filter && (
          <Text size="xs" c="dimmed" className="mono">
            {plural(shown.length, 'ligne', 'lignes')}
          </Text>
        )}
      </Group>
      <ScrollArea h={height} type="auto" className="logs-box">
        <pre className="logs-view">
          {q.isError ? (
            <span className="log-err">Impossible de lire les logs : {String(q.error)}</span>
          ) : text ? (
            shown.map((l, i) => (
              <div key={i} className={lineClass(l)}>
                {l || ' '}
              </div>
            ))
          ) : (
            <span className="dimmed">{q.isLoading ? 'Chargement…' : '(aucun log)'}</span>
          )}
        </pre>
      </ScrollArea>
    </Box>
  );
}
