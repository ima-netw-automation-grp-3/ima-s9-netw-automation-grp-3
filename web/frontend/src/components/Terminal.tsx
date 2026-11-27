import { useEffect, useRef, useState } from 'react';
import { ActionIcon, Box, Group, SegmentedControl, Text, Tooltip } from '@mantine/core';
import { IconExternalLink, IconRefresh } from '@tabler/icons-react';

import { closeTerminalSession, getTerminalSession, type TerminalStatus } from '../terminalSessions';
import { openTerminalWindow } from '../utils';
import { Led, type Tone } from './common';

const STATUS: Record<TerminalStatus, { tone: Tone; label: string }> = {
  connecting: { tone: 'busy', label: 'connexion…' },
  open: { tone: 'ok', label: 'connecté' },
  closed: { tone: 'off', label: 'session terminée' },
  error: { tone: 'err', label: 'erreur' },
};

export const SHELL_LABELS: Record<string, string> = {
  bash: 'CLI SONiC (bash : show / config)',
  vtysh: 'FRR (vtysh)',
  sh: 'Shell (sh)',
};

interface Props {
  node: string;
  shells: string[];
  initialShell?: string;
  height?: number | string;
  popout?: boolean;
}

export function Terminal({ node, shells, initialShell, height = 520, popout = true }: Props) {
  const [shell, setShell] = useState(initialShell && shells.includes(initialShell) ? initialShell : shells[0]);
  const [status, setStatus] = useState<TerminalStatus>('connecting');
  const [session, setSession] = useState(0);
  const container = useRef<HTMLDivElement>(null);

  // la session (xterm + WebSocket) vit hors du composant : on ne fait que l'afficher ici
  useEffect(() => {
    const el = container.current;
    if (!el || !shell) return;
    const term = getTerminalSession(node, shell);
    setStatus(term.status);
    const unsubscribe = term.subscribe(setStatus);
    term.attach(el);
    const observer = new ResizeObserver(() => term.refit());
    observer.observe(el);
    return () => {
      observer.disconnect();
      unsubscribe();
      term.detach(el);
    };
  }, [node, shell, session]);

  const restart = () => {
    closeTerminalSession(node, shell);
    setSession((n) => n + 1);
  };

  const st = STATUS[status];
  return (
    <Box>
      <Group justify="space-between" mb="xs" wrap="wrap" gap="xs">
        <Group gap="md">
          {shells.length > 1 ? (
            <SegmentedControl
              size="xs"
              value={shell}
              onChange={setShell}
              data={shells.map((s) => ({ value: s, label: SHELL_LABELS[s] ?? s }))}
            />
          ) : (
            <Text size="sm" fw={500}>
              {SHELL_LABELS[shell] ?? shell}
            </Text>
          )}
          <span className="state-label">
            <Led tone={st.tone} />
            <Text span size="sm" c={st.tone === 'err' ? 'var(--state-err)' : 'dimmed'}>
              {st.label}
            </Text>
          </span>
        </Group>
        <Group gap={4}>
          <Tooltip label="Nouvelle session">
            <ActionIcon aria-label="Nouvelle session" onClick={restart}>
              <IconRefresh size={16} />
            </ActionIcon>
          </Tooltip>
          {popout && (
            <Tooltip label="Ouvrir dans une nouvelle fenêtre">
              <ActionIcon aria-label="Ouvrir dans une nouvelle fenêtre" onClick={() => openTerminalWindow(node, shell)}>
                <IconExternalLink size={16} />
              </ActionIcon>
            </Tooltip>
          )}
        </Group>
      </Group>
      <Box ref={container} className="terminal-box" style={{ height }} />
    </Box>
  );
}
