import { Box, Group, Text, Tooltip } from '@mantine/core';

import type { PortInfo } from '../types';
import { formatSpeed, plural } from '../utils';
import type { Tone } from './common';

/*
 * Façade du routeur : les ports sur deux rangées comme sur un switch 32 ports (ports impairs en haut, pairs en
 * bas, Ethernet0 en haut à gauche). Chaque cage a sa LED : verte si le lien est up, rouge si le port est activé
 * mais sans lien, éteinte s'il est désactivé (shutdown). Une cage où un câble du lab est branché est pleine.
 */

const PAD = 12;
const CAGE_W = 24;
const CAGE_H = 16;
const PITCH = 28;
const GAP = 4;
const LED = 6;

const portIndex = (name: string) => Number(/(\d+)$/.exec(name)?.[1] ?? Number.NaN);
const portTone = (p: PortInfo): Tone => (p.oper_status === 'up' ? 'ok' : p.admin_status === 'up' ? 'err' : 'off');
const LED_FILL: Partial<Record<Tone, string>> = { ok: 'var(--state-ok)', err: 'var(--state-err)' };

function PortTip({ port }: { port: PortInfo }) {
  return (
    <div className="mono" style={{ lineHeight: '16px' }}>
      <div>
        {port.name}
        {port.alias ? ` · ${port.alias}` : ''}
      </div>
      {port.wire && (
        <div>
          câble {port.wire}
          {port.description ? ` → ${port.description}` : ''}
        </div>
      )}
      <div>
        admin {port.admin_status} · oper {port.oper_status} · {formatSpeed(port.speed)}
      </div>
    </div>
  );
}

export function FrontPanel({ ports, node }: { ports: PortInfo[]; node: string }) {
  const sorted = [...ports].sort((a, b) => portIndex(a.name) - portIndex(b.name));
  const cols = Math.ceil(sorted.length / 2);
  const width = PAD * 2 + cols * PITCH - GAP;
  const height = PAD * 2 + LED * 2 + CAGE_H * 2 + GAP * 3;
  const wired = sorted.filter((p) => p.wire).length;
  const up = sorted.filter((p) => p.oper_status === 'up').length;
  const shutdown = sorted.filter((p) => p.admin_status !== 'up').length;
  const down = sorted.filter((p) => p.admin_status === 'up' && p.oper_status !== 'up').length;

  return (
    <Group gap="lg" wrap="wrap" align="center">
      <Box style={{ flex: `0 1 ${Math.round(width * 1.2)}px`, minWidth: 0 }}>
        <svg
          className="front-panel"
          viewBox={`0 0 ${width} ${height}`}
          role="img"
          aria-label={`Façade de ${node} : ${sorted.length} ports, ${wired} câblés, ${up} up, ${shutdown} shutdown`}
        >
          <rect x={0.5} y={0.5} width={width - 1} height={height - 1} rx={6} fill="var(--app-deep)" stroke="var(--app-border)" />
          {sorted.map((p, i) => {
            const top = i % 2 === 0;
            const x = PAD + Math.floor(i / 2) * PITCH;
            const cageY = PAD + LED + GAP + (top ? 0 : CAGE_H + GAP);
            const ledY = top ? PAD : cageY + CAGE_H + GAP;
            const tone = portTone(p);
            return (
              <Tooltip key={p.name} label={<PortTip port={p} />}>
                <g>
                  <rect x={x} y={cageY} width={CAGE_W} height={CAGE_H} rx={2} fill="var(--app-raised)" stroke="var(--app-border)" />
                  <rect
                    x={x + 4}
                    y={cageY + 4}
                    width={CAGE_W - 8}
                    height={CAGE_H - 8}
                    rx={1}
                    fill={p.wire ? 'var(--mantine-color-dimmed)' : 'var(--app-deep)'}
                  />
                  <rect
                    x={x + (CAGE_W - LED) / 2}
                    y={ledY}
                    width={LED}
                    height={LED}
                    rx={1.5}
                    fill={LED_FILL[tone] ?? 'var(--app-led-off)'}
                  />
                </g>
              </Tooltip>
            );
          })}
        </svg>
      </Box>
      <Text size="sm" c="dimmed">
        <span className="mono">{plural(wired, 'câblé', 'câblés')}</span> · <span className="mono">{up} up</span> ·{' '}
        <span className="mono">{shutdown} shutdown</span>
        {down > 0 && (
          <>
            {' '}
            · <span className="mono" style={{ color: 'var(--state-err)' }}>{down} down</span>
          </>
        )}
      </Text>
    </Group>
  );
}
