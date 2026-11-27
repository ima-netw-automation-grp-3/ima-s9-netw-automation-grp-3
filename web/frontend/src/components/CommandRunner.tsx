import { useState } from 'react';
import { Button, Card, Group, Stack, Text, TextInput } from '@mantine/core';
import { IconPlayerPlay } from '@tabler/icons-react';
import { useMutation } from '@tanstack/react-query';

import { api } from '../api';
import type { ExecResult } from '../types';
import { notifyError } from '../utils';
import { Led, OutputBlock } from './common';

export interface QuickCommand {
  label: string;
  command: string;
}

export const SONIC_COMMANDS: QuickCommand[] = [
  { label: 'show version', command: 'show version' },
  { label: 'interfaces status', command: 'show interfaces status' },
  { label: 'interfaces description', command: 'show interfaces description' },
  { label: 'ip interfaces', command: 'show ip interfaces' },
  { label: 'ip route', command: 'show ip route' },
  { label: 'bgp summary', command: 'show ip bgp summary' },
  { label: 'bgp neighbors', command: 'show ip bgp neighbors' },
  { label: 'arp', command: 'show arp' },
  { label: 'compteurs', command: 'show interfaces counters' },
  { label: 'vlan brief', command: 'show vlan brief' },
  { label: 'running (CONFIG_DB)', command: 'show runningconfiguration all' },
  { label: 'running FRR', command: "vtysh -c 'show running-config'" },
  { label: 'supervisorctl', command: 'supervisorctl status' },
];

export const LINUX_COMMANDS: QuickCommand[] = [
  { label: 'ip addr', command: 'ip -o addr' },
  { label: 'ip route', command: 'ip route' },
  { label: 'ip neigh', command: 'ip neigh' },
  { label: 'ip link', command: 'ip -o link' },
  { label: 'compteurs', command: 'cat /proc/net/dev' },
];

interface Entry {
  id: number;
  command: string;
  result: ExecResult;
}

export function CommandRunner({ node, quick }: { node: string; quick: QuickCommand[] }) {
  const [command, setCommand] = useState('');
  const [entries, setEntries] = useState<Entry[]>([]);
  const run = useMutation({
    mutationFn: (cmd: string) => api.exec(node, cmd),
    onSuccess: (result, cmd) => setEntries((e) => [{ id: Date.now(), command: cmd, result }, ...e].slice(0, 8)),
    onError: (err) => notifyError(err, 'Commande'),
  });
  const submit = (cmd: string) => {
    if (cmd.trim()) run.mutate(cmd.trim());
  };

  return (
    <Stack gap="md">
      <Group gap={8}>
        {quick.map((q) => (
          <button
            key={q.command}
            type="button"
            className="chip"
            title={q.command}
            onClick={() => {
              setCommand(q.command);
              submit(q.command);
            }}
          >
            {q.label}
          </button>
        ))}
      </Group>
      <Group align="flex-end" wrap="nowrap" gap="sm">
        <TextInput
          style={{ flex: 1 }}
          label="Commande"
          description={`Exécutée dans ${node} via docker exec`}
          placeholder="ex. show ip route"
          styles={{ input: { fontFamily: 'var(--mantine-font-family-monospace)' } }}
          value={command}
          onChange={(e) => setCommand(e.currentTarget.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') submit(command);
          }}
        />
        <Button leftSection={<IconPlayerPlay size={16} />} loading={run.isPending} onClick={() => submit(command)}>
          Exécuter
        </Button>
      </Group>
      {entries.length === 0 && (
        <Text c="dimmed" size="sm">
          Les 8 derniers résultats s'affichent ici
        </Text>
      )}
      {entries.map((e) => {
        const ok = e.result.exit_code === 0;
        return (
          <Card key={e.id} padding="md">
            <Group justify="space-between" mb={8} gap="sm">
              <Text size="sm" className="mono">
                <span className="muted">$</span> {e.command}
              </Text>
              <Group gap="md">
                {e.result.timed_out && (
                  <Text span size="sm" c="var(--state-warn)">
                    délai dépassé
                  </Text>
                )}
                <span className="state-label">
                  <Led tone={ok ? 'ok' : 'err'} />
                  <Text span size="sm" className="mono" c={ok ? 'dimmed' : 'var(--state-err)'}>
                    code {e.result.exit_code}
                  </Text>
                </span>
                <Text span size="xs" className="mono muted">
                  {e.result.duration.toFixed(2)} s
                </Text>
              </Group>
            </Group>
            <OutputBlock text={e.result.output ?? `${e.result.stdout}${e.result.stderr}`} />
          </Card>
        );
      })}
    </Stack>
  );
}
