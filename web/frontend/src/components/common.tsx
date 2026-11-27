import type { CSSProperties, ReactNode } from 'react';
import {
  ActionIcon,
  Alert,
  Box,
  Card,
  Code,
  CopyButton,
  Group,
  ScrollArea,
  Table,
  Text,
  Tooltip,
} from '@mantine/core';
import {
  IconAlertTriangle,
  IconCheck,
  IconCircle,
  IconCircleFilled,
  IconCircleHalf2,
  IconCircleX,
  IconClockExclamation,
  IconCopy,
} from '@tabler/icons-react';

import type { Change, ChangeSummary, Phase } from '../types';
import { PHASES, errorMessage, stateColor } from '../utils';

const PHASE_MARKS: Record<Phase, typeof IconCircleFilled> = {
  ready: IconCircleFilled,
  booting: IconCircleHalf2,
  bootstrap: IconCircleHalf2,
  warning: IconAlertTriangle,
  error: IconCircleX,
  down: IconCircle,
};

/** Indicateur d'état compact : la forme distingue l'état sans dépendre de la couleur. */
export function StatusDot({ phase, decorative = false }: { phase: Phase; decorative?: boolean }) {
  const meta = PHASES[phase] ?? PHASES.down;
  const Mark = PHASE_MARKS[phase] ?? IconCircle;
  const a11y = decorative ? { 'aria-hidden': true } : { role: 'img', 'aria-label': meta.label, title: meta.label };
  return (
    <span className="state-mark" {...a11y}>
      <Mark size={14} color={stateColor(meta.color)} />
    </span>
  );
}

/** État d'un nœud : marque et libellé. Un nœud prêt reste discret ; les autres états colorent leur texte. */
export function PhaseLabel({ phase, detail }: { phase: Phase; detail?: string }) {
  const meta = PHASES[phase] ?? PHASES.down;
  const label = (
    <span className="state-label">
      <StatusDot phase={phase} decorative />
      <Text span size="sm" c={phase === 'ready' ? 'dimmed' : stateColor(meta.color)}>
        {meta.label}
      </Text>
    </span>
  );
  return detail ? (
    <Tooltip label={detail} multiline maw={360}>
      {label}
    </Tooltip>
  ) : (
    label
  );
}

const GOOD = ['up', 'running', 'established', 'reachable'];
const BAD = ['down', 'fatal', 'exited', 'backoff', 'failed'];

export type Tone = 'ok' | 'warn' | 'err' | 'busy' | 'info' | 'off';

const TONES: Record<string, Tone> = { green: 'ok', yellow: 'busy', blue: 'info', orange: 'warn', red: 'err', gray: 'off' };

/** Nom de couleur des mappings Grafana (PHASES, RUNNER_STATES) → ton d'une LED. */
export function toneOf(color?: string | null): Tone {
  return TONES[color ?? 'gray'] ?? 'off';
}

/** LED carrée d'un port de switch : la signature de l'interface pour les ports, liens et sessions. */
export function Led({ tone }: { tone: Tone }) {
  const style = { '--led': tone === 'off' ? undefined : `var(--state-${tone})` } as CSSProperties;
  return <span className="led" style={style} aria-hidden />;
}

/** Valeur d'état (port, session, interface) : LED et texte ; seul un état en défaut colore son texte. */
export function StateLabel({ value, good = GOOD }: { value?: string | null; good?: string[] }) {
  if (!value) return <Text span c="dimmed">—</Text>;
  const v = value.toLowerCase();
  const tone: Tone = good.includes(v) ? 'ok' : BAD.includes(v) ? 'err' : 'off';
  return (
    <span className="state-label">
      <Led tone={tone} />
      <Text span inherit c={tone === 'err' ? 'var(--state-err)' : tone === 'off' ? 'dimmed' : undefined}>
        {value}
      </Text>
    </span>
  );
}

/**
 * Données périmées : la dernière réponse est conservée à l'écran, mais l'API ne répond plus depuis.
 * Distinct de « aucune donnée » (rien reçu) et de « indisponible » (le nœud lui-même est arrêté).
 */
export function StaleDataAlert({ error, updatedAt, mb }: { error: unknown; updatedAt: number; mb?: string }) {
  const at = updatedAt ? new Date(updatedAt).toLocaleTimeString('fr-FR') : '—';
  return (
    <Alert
      color="orange"
      variant="light"
      mb={mb}
      icon={<IconClockExclamation size={16} />}
      title={`Données périmées : dernière réponse de l'API à ${at}`}
    >
      <Text size="sm">
        Les valeurs affichées ne sont plus rafraîchies. Erreur : <Code>{errorMessage(error)}</Code>
      </Text>
    </Alert>
  );
}

/** Carte de section : titre, compteur discret à côté du titre, actions à droite. */
export function SectionCard({
  title,
  count,
  actions,
  children,
}: {
  title: ReactNode;
  count?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
}) {
  return (
    <Card>
      <Group justify="space-between" mb="sm" wrap="wrap" gap="sm" mih={28}>
        <Group gap={8} wrap="nowrap" align="baseline">
          <Text component="h3" m={0} fz="md" fw={500}>
            {title}
          </Text>
          {count !== undefined && (
            <Text span fz="xs" className="mono muted">
              {count}
            </Text>
          )}
        </Group>
        {actions}
      </Group>
      {children}
    </Card>
  );
}

export function CopyIcon({ value }: { value: string }) {
  return (
    <CopyButton value={value} timeout={1500}>
      {({ copied, copy }) => (
        <Tooltip label={copied ? 'Copié' : 'Copier'}>
          <ActionIcon onClick={copy} size={24} aria-label={`Copier ${value}`}>
            {copied ? <IconCheck size={16} color="var(--state-ok)" /> : <IconCopy size={16} />}
          </ActionIcon>
        </Tooltip>
      )}
    </CopyButton>
  );
}

/** Identifiant de diagnostic (IP, conteneur, router-id…) : texte à chasse fixe sélectionnable, avec bouton copier. */
export function CopyValue({ value, empty = '—' }: { value?: string | null; empty?: string }) {
  if (!value) return <Text span c="dimmed">{empty}</Text>;
  return (
    <Group component="span" gap={4} wrap="nowrap" display="inline-flex" style={{ verticalAlign: 'middle' }}>
      <span className="mono" style={{ color: 'var(--mantine-color-text)' }}>
        {value}
      </span>
      <CopyIcon value={value} />
    </Group>
  );
}

/** Sortie de commande en police à chasse fixe, avec bouton copier. */
export function OutputBlock({ text, maxHeight = 420, empty = '(aucune sortie)' }: { text: string; maxHeight?: number; empty?: string }) {
  return (
    <Box pos="relative">
      {text && (
        <Box pos="absolute" top={4} right={4} style={{ zIndex: 1 }}>
          <CopyIcon value={text} />
        </Box>
      )}
      <ScrollArea.Autosize mah={maxHeight} type="auto">
        <Code block className="output-block">
          {text || empty}
        </Code>
      </ScrollArea.Autosize>
    </Box>
  );
}

/** Diff au format unifié, lignes colorées. */
export function DiffView({ diff, maxHeight = 460, empty = 'Aucune différence' }: { diff?: string; maxHeight?: number; empty?: string }) {
  if (!diff) {
    return (
      <Text c="dimmed" size="sm">
        {empty}
      </Text>
    );
  }
  return (
    <Box pos="relative">
      <Box pos="absolute" top={4} right={4} style={{ zIndex: 1 }}>
        <CopyIcon value={diff} />
      </Box>
      <ScrollArea.Autosize mah={maxHeight} type="auto">
        <pre className="diff-view">
          {diff.split('\n').map((line, i) => {
            let cls = '';
            if (line.startsWith('+++') || line.startsWith('---')) cls = 'diff-file';
            else if (line.startsWith('@@')) cls = 'diff-hunk';
            else if (line.startsWith('+')) cls = 'diff-add';
            else if (line.startsWith('-')) cls = 'diff-del';
            return (
              <div key={i} className={cls}>
                {line || ' '}
              </div>
            );
          })}
        </pre>
      </ScrollArea.Autosize>
    </Box>
  );
}

function Count({ n, sign, one, many, color }: { n: number; sign: string; one: string; many: string; color: string }) {
  return (
    <Text span size="sm" c="dimmed">
      <Text span inherit className="mono" fw={500} c={n ? color : undefined}>
        {sign}
        {n}
      </Text>{' '}
      {n >= 2 ? many : one}
    </Text>
  );
}

/** Bilan d'un changement CONFIG_DB : les nombres portent la couleur, uniquement quand ils ne sont pas nuls. */
export function SummaryLine({ summary }: { summary?: ChangeSummary }) {
  if (!summary) return null;
  return (
    <Group gap={16}>
      <Count n={summary.add} sign="+" one="ajout" many="ajouts" color="var(--state-ok)" />
      <Count n={summary.modify} sign="~" one="modification" many="modifications" color="var(--state-info)" />
      <Count n={summary.delete} sign="−" one="suppression" many="suppressions" color="var(--state-err)" />
    </Group>
  );
}

const ACTIONS: Record<Change['action'], { color: string; label: string }> = {
  add: { color: 'var(--state-ok)', label: 'ajout' },
  modify: { color: 'var(--state-info)', label: 'modif.' },
  delete: { color: 'var(--state-err)', label: 'suppr.' },
};

const fmt = (v: unknown) => (v === null || v === undefined ? '∅' : Array.isArray(v) ? v.join(',') : String(v));

/** Liste des changements CONFIG_DB (table|clé et champs avant → après). */
export function ChangesTable({ changes, maxHeight = 360 }: { changes?: Change[]; maxHeight?: number }) {
  if (!changes?.length) {
    return (
      <Text c="dimmed" size="sm">
        Aucun changement
      </Text>
    );
  }
  return (
    <ScrollArea.Autosize mah={maxHeight} type="auto">
      <Table fz="xs" verticalSpacing={4} horizontalSpacing={8}>
        <Table.Thead>
          <Table.Tr>
            <Table.Th w={72}>Action</Table.Th>
            <Table.Th>Table | clé</Table.Th>
            <Table.Th>Champs</Table.Th>
          </Table.Tr>
        </Table.Thead>
        <Table.Tbody>
          {changes.map((c) => (
            <Table.Tr key={`${c.action}-${c.table}-${c.key}`}>
              <Table.Td className="mono" c={ACTIONS[c.action].color}>
                {ACTIONS[c.action].label}
              </Table.Td>
              <Table.Td ff="monospace">
                {c.table}|{c.key}
              </Table.Td>
              <Table.Td ff="monospace">
                {c.fields.length === 0 ? (
                  <Text span c="dimmed" fz="xs">
                    (entrée sans champ)
                  </Text>
                ) : (
                  c.fields.map((f) => (
                    <div key={f.name}>
                      {f.name}: {c.action === 'add' ? fmt(f.after) : c.action === 'delete' ? fmt(f.before) : `${fmt(f.before)} → ${fmt(f.after)}`}
                    </div>
                  ))
                )}
              </Table.Td>
            </Table.Tr>
          ))}
        </Table.Tbody>
      </Table>
    </ScrollArea.Autosize>
  );
}
