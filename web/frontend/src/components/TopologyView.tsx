import {
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type MouseEvent as ReactMouseEvent,
  type PointerEvent as ReactPointerEvent,
  type ReactNode,
} from 'react';
import { Box, Button, Group, Menu, Text, Tooltip } from '@mantine/core';
import { useElementSize, useMergedRef } from '@mantine/hooks';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { IconActivity, IconPlugConnected, IconPlugConnectedX } from '@tabler/icons-react';
import { Link, useNavigate } from 'react-router';

import { api } from '../api';
import type { LabState, LinkEnd, LinkInfo } from '../types';
import { notifyError, notifySuccess, PHASES, stateColor } from '../utils';
import { StatusDot, type Tone } from './common';
import {
  LABEL_GAP,
  LINE,
  arcPath,
  exitPoint,
  freeLayout,
  labelLines,
  layout,
  type Geometry,
  type LabelSpot,
  type Point,
  type Tile,
} from './topologyLayout';

/*
 * Topologie du lab dessinée comme son câblage : les équipements, les câbles branchés sur des ports dont la LED
 * donne l'état, les noms de ports et adresses comme des étiquettes de câble, et la session eBGP comme un arc ou
 * une pastille. Tout est calculé en pixels à partir de la largeur réelle du conteneur (pas de SVG étiré) ; la
 * disposition (ligne, colonne, étages ou libre) est dans topologyLayout.ts.
 *
 * Les équipements se déplacent à la souris : la disposition devient libre et s'enregistre dans le navigateur
 * (localStorage, par lab) ; « Disposition automatique » l'efface.
 */

const MIN_SCALE = 0.6; // réduction maximale d'un grand dessin pour tenir dans la largeur
const DRAG_THRESHOLD = 4; // px : en deçà, l'appui reste un clic (ouvre la page du nœud)
const STORAGE_PREFIX = 'sonic-lab:topologie:'; // + nom du lab

type Positions = Record<string, Point>;

function loadPositions(lab: string): Positions | null {
  try {
    const raw: unknown = JSON.parse(localStorage.getItem(STORAGE_PREFIX + lab) ?? 'null');
    if (!raw || typeof raw !== 'object') return null;
    const out: Positions = {};
    for (const [name, v] of Object.entries(raw)) {
      if (Array.isArray(v) && v.length === 2 && v.every((n) => typeof n === 'number' && Number.isFinite(n))) {
        out[name] = { x: v[0], y: v[1] };
      }
    }
    return Object.keys(out).length ? out : null;
  } catch {
    return null;
  }
}

function savePositions(lab: string, positions: Positions | null) {
  try {
    if (!positions) localStorage.removeItem(STORAGE_PREFIX + lab);
    else {
      const rounded = Object.entries(positions).map(([n, p]) => [n, [Math.round(p.x), Math.round(p.y)]]);
      localStorage.setItem(STORAGE_PREFIX + lab, JSON.stringify(Object.fromEntries(rounded)));
    }
  } catch {
    // stockage indisponible (navigation privée…) : la disposition vaut pour la session
  }
}

const endTone = (e: LinkEnd): Tone =>
  e.admin === 'down' || e.wire_up === false ? 'off' : e.up === true ? 'ok' : e.up === false ? 'err' : 'off';

const CABLE: Record<LinkInfo['state'], { stroke: string; dash?: string }> = {
  up: { stroke: 'var(--app-cable)' },
  down: { stroke: 'var(--state-err)', dash: '6 4' },
  unknown: { stroke: 'var(--app-cable)', dash: '2 4' },
};

const sessionPeers = (link: LinkInfo) =>
  link.bgp!.peers.map((p) => `${p.router} → ${p.peer} (AS ${p.remote_as}) : ${p.state}`).join(' · ');

/** Clic droit sur une interface, son voyant ou un câble : menu (capture, coupure) des extrémités concernées. */
type OnCaptureMenu = (e: ReactMouseEvent, ends: LinkEnd[]) => void;

/** Port d'un équipement où arrive un câble : un carré allumé selon l'état du lien de ce côté. */
function PortLed({ at, end, onMenu }: { at: Point; end: LinkEnd; onMenu: OnCaptureMenu }) {
  const tone = endTone(end);
  const style: CSSProperties = {
    left: at.x - 4,
    top: at.y - 4,
    background: tone === 'off' ? 'var(--app-led-off)' : `var(--state-${tone})`,
  };
  return <span className="topo-led" style={style} aria-hidden onContextMenu={(e) => onMenu(e, [end])} />;
}

const endName = (e: LinkEnd) => `${e.node} · ${e.port ? `${e.port} (${e.interface})` : e.interface}`;

/** Étiquette de câble : port SONiC (ou interface du client), nom containerlab, adresses. */
function EndLabel({
  end,
  style,
  boxed = false,
  onMenu,
}: {
  end: LinkEnd;
  style: CSSProperties;
  boxed?: boolean;
  onMenu?: OnCaptureMenu;
}) {
  return (
    <div className={boxed ? 'topo-label boxed' : 'topo-label'} style={style} onContextMenu={onMenu && ((e) => onMenu(e, [end]))}>
      <div>
        <span style={{ color: 'var(--mantine-color-text)' }}>{end.port ?? end.interface}</span>
        {end.port && <span className="sub"> · {end.interface}</span>}
        {end.admin === 'down' && <span style={{ color: 'var(--state-err)' }}> (shutdown)</span>}
        {end.wire_up === false && <span style={{ color: 'var(--state-err)' }}> (câble coupé)</span>}
      </div>
      {end.addresses.map((a) => (
        <div key={a}>{a}</div>
      ))}
    </div>
  );
}

/** Position CSS d'une étiquette selon son ancrage (coin haut-gauche, coin haut-droit ou centre). */
function spotStyle(s: LabelSpot): CSSProperties {
  if (s.anchor === 'end') return { left: s.x, top: s.y, transform: 'translateX(-100%)', textAlign: 'right' };
  if (s.anchor === 'middle') return { left: s.x, top: s.y, transform: 'translate(-50%, -50%)', textAlign: 'center' };
  return { left: s.x, top: s.y };
}

function SessionTag({ link, style }: { link: LinkInfo; style: CSSProperties }) {
  const bgp = link.bgp!;
  const ok = bgp.state === 'Established';
  return (
    <Tooltip label={sessionPeers(link)} multiline maw={420}>
      <span className="topo-session" style={style}>
        <span className="led" style={{ '--led': ok ? 'var(--state-ok)' : 'var(--state-err)' } as CSSProperties} aria-hidden />
        <span style={{ color: 'var(--mantine-color-dimmed)' }}>eBGP</span>
        <span style={{ color: ok ? 'var(--mantine-color-text)' : 'var(--state-err)' }}>{bgp.state}</span>
      </span>
    </Tooltip>
  );
}

/** Session eBGP d'un câble compact : un point (bleu établie, rouge sinon), le détail en infobulle. */
function SessionDot({ link, at }: { link: LinkInfo; at: Point }) {
  const { state } = link.bgp!;
  const color = state === 'Established' ? 'var(--mantine-color-anchor)' : 'var(--state-err)';
  return (
    <Tooltip label={`eBGP ${state} · ${sessionPeers(link)}`} multiline maw={420}>
      <span
        className="topo-session-dot"
        style={{ left: at.x, top: at.y, '--dot': color } as CSSProperties}
        role="img"
        aria-label={`session eBGP ${state}`}
      />
    </Tooltip>
  );
}

function DownTag({ style }: { style: CSSProperties }) {
  return (
    <span className="topo-session mono" style={{ ...style, color: 'var(--state-err)' }}>
      down
    </span>
  );
}

function NodeTile({
  tile,
  dragging,
  onPointerDown,
  onClick,
}: {
  tile: Tile;
  dragging: boolean;
  onPointerDown?: (e: ReactPointerEvent<HTMLAnchorElement>) => void;
  onClick: (e: ReactMouseEvent<HTMLAnchorElement>) => void;
}) {
  const { node } = tile;
  const meta = PHASES[node.phase] ?? PHASES.down;
  const ready = node.phase === 'ready';
  const style = {
    left: tile.x - tile.w / 2,
    top: tile.y - tile.h / 2,
    width: tile.w,
    height: tile.h,
    '--tile-color': stateColor(meta.color),
  } as CSSProperties;
  const body = (
    <Link
      to={`/nodes/${node.name}`}
      className="topo-tile"
      style={style}
      data-alert={ready ? undefined : true}
      data-dragging={dragging || undefined}
      aria-label={`${node.name} : ${meta.label}`}
      draggable={false} // pas de glisser natif du lien : c'est la tuile qui se déplace
      onDragStart={(e) => e.preventDefault()}
      onPointerDown={onPointerDown}
      onClick={onClick}
    >
      <Group gap={8} wrap="nowrap">
        <StatusDot phase={node.phase} decorative />
        <Text span fw={500} c="var(--mantine-color-text)">
          {node.name}
        </Text>
      </Group>
      {node.role === 'router' && (
        <Text span fz="xs" className="mono muted">
          AS {node.bgp?.as ?? '—'}
        </Text>
      )}
      {!ready && (
        <Text span fz="xs" c={stateColor(meta.color)}>
          {meta.label}
        </Text>
      )}
    </Link>
  );
  return node.detail && !ready ? (
    <Tooltip label={node.detail} multiline maw={360}>
      {body}
    </Tooltip>
  ) : (
    body
  );
}

/** Chaîne en ligne ou en colonne : étiquettes le long des câbles, session eBGP en arc (en ligne). */
function ChainWiring({ lab, geo, onMenu }: { lab: LabState; geo: Geometry; onMenu: OnCaptureMenu }) {
  const { tiles, mode } = geo;
  const lines: ReactNode[] = [];
  const overlays: ReactNode[] = [];

  for (const l of lab.links) {
    const ta = tiles[l.a.node];
    const tz = tiles[l.z.node];
    if (!ta || !tz) continue;
    const cable = CABLE[l.state];
    lines.push(
      <line
        key={`cable-${l.id}`}
        x1={ta.x}
        y1={ta.y}
        x2={tz.x}
        y2={tz.y}
        stroke={cable.stroke}
        strokeWidth={2}
        strokeDasharray={cable.dash}
        strokeLinecap={l.state === 'unknown' ? 'round' : undefined}
      />,
      <line
        key={`hit-${l.id}`}
        x1={ta.x}
        y1={ta.y}
        x2={tz.x}
        y2={tz.y}
        className="topo-cable-hit"
        onContextMenu={(e) => onMenu(e, [l.a, l.z])}
      />,
    );
    overlays.push(<PortLed key={`led-a-${l.id}`} at={exitPoint(ta, tz)} end={l.a} onMenu={onMenu} />);
    overlays.push(<PortLed key={`led-z-${l.id}`} at={exitPoint(tz, ta)} end={l.z} onMenu={onMenu} />);

    if (mode === 'row') {
      // étiquette du côté gauche au-dessus du câble, du côté droit en dessous : elles ne se croisent jamais
      const [left, right, leftEnd, rightEnd] = ta.x <= tz.x ? [ta, tz, l.a, l.z] : [tz, ta, l.z, l.a];
      overlays.push(
        <EndLabel
          key={`lbl-l-${l.id}`}
          end={leftEnd}
          onMenu={onMenu}
          style={{ left: left.x + left.w / 2 + LABEL_GAP, top: left.y - LABEL_GAP - labelLines(leftEnd) * LINE }}
        />,
        <EndLabel
          key={`lbl-r-${l.id}`}
          end={rightEnd}
          onMenu={onMenu}
          style={{ right: `calc(100% - ${right.x - right.w / 2 - LABEL_GAP}px)`, top: right.y + LABEL_GAP, textAlign: 'right' }}
        />,
      );
      const midX = (left.x + right.x) / 2;
      if (l.bgp) {
        const ok = l.bgp.state === 'Established';
        const x0 = left.x + 24;
        const x3 = right.x - 24;
        const y0 = left.y - left.h / 2;
        const y3 = right.y - right.h / 2;
        // sommet visible de l'arc sous l'étiquette : une cubique n'atteint que 3/4 de la hauteur de ses points de contrôle
        const apex = left.y - geo.rise;
        const ctrl = y0 + (apex - y0) / 0.75;
        lines.push(
          <path
            key={`arc-${l.id}`}
            d={`M ${x0} ${y0} C ${x0} ${ctrl}, ${x3} ${ctrl}, ${x3} ${y3}`}
            fill="none"
            stroke={ok ? 'var(--mantine-color-anchor)' : 'var(--state-err)'}
            strokeWidth={1.5}
            strokeDasharray={ok ? undefined : '6 4'}
          />,
        );
        overlays.push(
          <SessionTag key={`bgp-${l.id}`} link={l} style={{ left: midX, top: apex - 8, transform: 'translateX(-50%)' }} />,
        );
      }
      if (l.state === 'down') overlays.push(<DownTag key={`down-${l.id}`} style={{ left: midX, top: left.y - 8, transform: 'translateX(-50%)' }} />);
    } else {
      const [top, bottom, topEnd, bottomEnd] = ta.y <= tz.y ? [ta, tz, l.a, l.z] : [tz, ta, l.z, l.a];
      const x = top.x + 12;
      const midY = (top.y + top.h / 2 + bottom.y - bottom.h / 2) / 2;
      overlays.push(
        <EndLabel key={`lbl-t-${l.id}`} end={topEnd} onMenu={onMenu} style={{ left: x, top: top.y + top.h / 2 + LABEL_GAP }} />,
        <EndLabel
          key={`lbl-b-${l.id}`}
          end={bottomEnd}
          onMenu={onMenu}
          style={{ left: x, top: bottom.y - bottom.h / 2 - LABEL_GAP - labelLines(bottomEnd) * LINE }}
        />,
      );
      if (l.bgp) overlays.push(<SessionTag key={`bgp-${l.id}`} link={l} style={{ left: x - 4, top: midY - 8 }} />);
      if (l.state === 'down') {
        overlays.push(<DownTag key={`down-${l.id}`} style={{ right: `calc(100% - ${top.x - 12}px)`, top: midY - 8 }} />);
      }
    }
  }

  return (
    <>
      <svg className="topo-svg" width="100%" height={geo.height} aria-hidden>
        {lines}
      </svg>
      {overlays}
    </>
  );
}

/**
 * Dispositions par étages et libre : câbles calculés par topologyLayout. Un câble compact (étiquettes sans place)
 * n'affiche ses étiquettes qu'au survol ou au toucher, et sa session eBGP comme un point.
 */
function CableWiring({ geo, onMenu }: { geo: Geometry; onMenu: OnCaptureMenu }) {
  const [active, setActive] = useState<string | null>(null);
  const lines: ReactNode[] = [];
  const hits: ReactNode[] = [];
  const overlays: ReactNode[] = [];

  for (const w of geo.wires) {
    const l = w.link;
    const cable = CABLE[l.state];
    const hot = w.compact && active === l.id;
    const d = w.arc ? arcPath(w.arc) : `M ${w.points.map((p) => `${p.x} ${p.y}`).join(' L ')}`;
    lines.push(
      <path
        key={`cable-${l.id}`}
        d={d}
        fill="none"
        stroke={cable.stroke}
        strokeWidth={hot ? 3 : 2}
        strokeDasharray={cable.dash}
        strokeLinecap={l.state === 'unknown' ? 'round' : undefined}
      />,
    );
    overlays.push(
      <PortLed key={`led-a-${l.id}`} at={w.ledA} end={l.a} onMenu={onMenu} />,
      <PortLed key={`led-z-${l.id}`} at={w.ledZ} end={l.z} onMenu={onMenu} />,
    );
    const bgp = w.session ? l.bgp : null;
    const menu = (e: ReactMouseEvent) => onMenu(e, [l.a, l.z]);
    if (!w.compact) hits.push(<path key={`hit-${l.id}`} d={d} className="topo-cable-hit" onContextMenu={menu} />);
    if (w.compact) {
      hits.push(
        <path
          key={`hit-${l.id}`}
          d={d}
          className="topo-hit"
          onMouseEnter={() => setActive(l.id)}
          onMouseLeave={() => setActive((cur) => (cur === l.id ? null : cur))}
          onClick={() => setActive(l.id)}
          onContextMenu={menu}
        />,
      );
      if (!hot) {
        if (bgp && w.tag) overlays.push(<SessionDot key={`dot-${l.id}`} link={l} at={w.tag} />);
        continue;
      }
    }
    // au survol, les étiquettes passent au-dessus de tout sans capter le pointeur (sinon le câble perd le survol)
    const lift: CSSProperties = hot ? { zIndex: 4, pointerEvents: 'none' } : {};
    w.labels.forEach((s, i) =>
      overlays.push(<EndLabel key={`lbl-${i}-${l.id}`} end={s.end} boxed onMenu={onMenu} style={{ ...spotStyle(s), ...lift }} />),
    );
    if (w.tag) {
      const at: CSSProperties = { left: w.tag.x, top: w.tag.y - 8, transform: 'translateX(-50%)', ...lift };
      if (bgp) overlays.push(<SessionTag key={`bgp-${l.id}`} link={l} style={at} />);
      else if (l.state === 'down') overlays.push(<DownTag key={`down-${l.id}`} style={at} />);
    }
  }

  return (
    <>
      <svg className="topo-svg" width="100%" height={geo.height} aria-hidden>
        {lines}
        {hits}
      </svg>
      {overlays}
    </>
  );
}

export function TopologyView({ lab }: { lab: LabState }) {
  const { ref: sizeRef, width } = useElementSize<HTMLDivElement>();
  const boxRef = useRef<HTMLDivElement>(null);
  const ref = useMergedRef(sizeRef, boxRef);
  const [labName, setLabName] = useState(lab.name);
  const [positions, setPositions] = useState<Positions | null>(() => loadPositions(lab.name));
  if (labName !== lab.name) {
    setLabName(lab.name);
    setPositions(loadPositions(lab.name));
  }
  const custom = positions != null && lab.nodes.some((n) => positions[n.name]);
  const [fit, setFit] = useState(true);
  const [drag, setDrag] = useState<{ name: string; scale: number } | null>(null);
  const geo = useMemo(
    () => (width > 0 ? (positions && custom ? freeLayout(lab, positions) : layout(lab, width)) : null),
    [lab, width, positions, custom],
  );
  const wide = geo != null && geo.width > width + 1;
  // échelle figée pendant un déplacement : le dessin grandit sans rapetisser sous la souris
  const scale = drag?.scale ?? (geo && wide && fit ? Math.max(MIN_SCALE, width / geo.width) : 1);
  const scroll = geo != null && geo.width * scale > width + 0.5;
  // dessin automatique plus étroit que la carte : centré ; disposition libre : calé à gauche, rien ne bouge
  const margin = geo && !custom && geo.width * scale < width ? (width - geo.width * scale) / 2 : 0;
  const compact = geo?.wires.some((w) => w.compact) ?? false;
  const dots = geo?.wires.some((w) => w.compact && w.session && w.link.bgp && w.tag) ?? false;

  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [menu, setMenu] = useState<{ x: number; y: number; ends: LinkEnd[] } | null>(null);
  // nœud arrêté ou état inconnu : pas de coupure proposée
  const wireEnds = menu?.ends.filter((e) => e.wire_up != null) ?? [];
  const wire = useMutation({
    mutationFn: (v: { end: LinkEnd; up: boolean }) => api.setWire(v.end.node, v.end.interface, v.up),
    onSuccess: (r, v) => {
      if (!r.ok) notifyError(new Error(r.output || 'commande en échec'), `Câble ${endName(v.end)}`);
      else notifySuccess(`${endName(v.end)} : câble ${r.up ? 'rétabli' : 'coupé'}`, 'Câble');
      void queryClient.invalidateQueries({ queryKey: ['lab'] });
      void queryClient.invalidateQueries({ queryKey: ['node'] });
    },
    onError: (e, v) => notifyError(e, `Câble ${endName(v.end)}`),
  });
  const openMenu: OnCaptureMenu = (e, ends) => {
    e.preventDefault();
    e.stopPropagation();
    setMenu({ x: e.clientX, y: e.clientY, ends });
  };
  useEffect(() => {
    if (!menu) return;
    const close = () => setMenu(null);
    window.addEventListener('scroll', close, true);
    window.addEventListener('resize', close);
    return () => {
      window.removeEventListener('scroll', close, true);
      window.removeEventListener('resize', close);
    };
  }, [menu]);

  const suppressClick = useRef(false); // clic qui termine un déplacement : n'ouvre pas la page du nœud
  const stopDrag = useRef<(() => void) | null>(null);
  useEffect(() => () => stopDrag.current?.(), []);

  const startDrag = (name: string, e: ReactPointerEvent) => {
    // souris ou stylet ; au doigt, glisser doit continuer à faire défiler la page
    if (!geo || e.button !== 0 || e.pointerType === 'touch') return;
    e.preventDefault(); // pas de sélection de texte pendant le déplacement (le clic, lui, reste émis)
    suppressClick.current = false;
    const s = scale;
    // ce qui est affiché (centrage compris) devient la disposition de départ : rien ne saute au premier mouvement
    const base: Positions = {};
    for (const t of Object.values(geo.tiles)) base[t.node.name] = { x: t.x + margin / s, y: t.y };
    const origin = base[name];
    const box = boxRef.current;
    const start = { x: e.pageX, y: e.pageY, scroll: box?.scrollLeft ?? 0 };
    const snapshot = lab;
    let moved: Positions | null = null;
    const move = (ev: PointerEvent) => {
      // coordonnées de page et défilement de la zone : la tuile suit la souris même quand le dessin défile
      const dx = ev.pageX - start.x + (box?.scrollLeft ?? 0) - start.scroll;
      const dy = ev.pageY - start.y;
      if (!moved && Math.hypot(dx, dy) < DRAG_THRESHOLD) return;
      if (!moved) setDrag({ name, scale: s });
      moved = { ...base, [name]: { x: origin.x + dx / s, y: origin.y + dy / s } };
      setPositions(moved);
    };
    const stop = () => {
      window.removeEventListener('pointermove', move);
      window.removeEventListener('pointerup', end);
      window.removeEventListener('pointercancel', end);
      stopDrag.current = null;
    };
    const end = () => {
      stop();
      if (!moved) return;
      // le clic qui suit le relâchement (même tâche) ne doit pas ouvrir la page du nœud ; les suivants, si
      suppressClick.current = true;
      setTimeout(() => (suppressClick.current = false), 0);
      // contenu sorti à gauche ou en haut : le décalage de l'origine passe dans les positions enregistrées
      const shift = freeLayout(snapshot, moved).shift ?? { x: 0, y: 0 };
      const next: Positions = {};
      for (const [n, p] of Object.entries(moved)) next[n] = { x: p.x + shift.x, y: p.y + shift.y };
      setPositions(next);
      savePositions(snapshot.name, next);
      setDrag(null);
    };
    stopDrag.current = stop;
    window.addEventListener('pointermove', move);
    window.addEventListener('pointerup', end);
    window.addEventListener('pointercancel', end);
  };

  // tuile déplacée au-delà du bord visible : la zone défile pour la suivre (le dessin s'est agrandi)
  useLayoutEffect(() => {
    const box = boxRef.current;
    const tile = drag && box?.querySelector<HTMLElement>('.topo-tile[data-dragging]');
    if (!box || !tile) return;
    const b = box.getBoundingClientRect();
    const t = tile.getBoundingClientRect();
    if (t.right > b.right) box.scrollLeft += t.right - b.right;
    else if (t.left < b.left) box.scrollLeft -= b.left - t.left;
  }, [drag, geo]);

  const onTileClick = (e: ReactMouseEvent) => {
    if (suppressClick.current) {
      e.preventDefault();
      suppressClick.current = false;
    }
  };

  const reset = () => {
    setPositions(null);
    savePositions(lab.name, null);
  };

  const hints = [
    compact && 'Survolez ou touchez un câble pour afficher ses ports et adresses.',
    dots && 'Point sur un câble : session eBGP (bleu : établie, rouge : non établie).',
  ].filter(Boolean);

  return (
    <>
      <Box ref={ref} style={{ overflowX: scroll ? 'auto' : undefined }}>
        {geo ? (
          <Box w={geo.width * scale} h={geo.height * scale} ml={margin || undefined}>
            <Box
              className="topo"
              role="group"
              aria-label={`Topologie du lab ${lab.name}`}
              data-dragging={drag ? true : undefined}
              style={{
                width: geo.width,
                height: geo.height,
                transform: scale < 1 ? `scale(${scale})` : undefined,
                transformOrigin: '0 0',
              }}
            >
              {geo.mode === 'row' || geo.mode === 'column' ? (
                <ChainWiring lab={lab} geo={geo} onMenu={openMenu} />
              ) : (
                <CableWiring geo={geo} onMenu={openMenu} />
              )}
              {Object.values(geo.tiles).map((t) => (
                <NodeTile
                  key={t.node.name}
                  tile={t}
                  dragging={drag?.name === t.node.name}
                  onPointerDown={(e) => startDrag(t.node.name, e)}
                  onClick={onTileClick}
                />
              ))}
            </Box>
          </Box>
        ) : (
          <Box h={120} />
        )}
      </Box>
      <Menu opened={menu != null} onChange={(o) => !o && setMenu(null)} position="bottom-start" offset={2} shadow="md" withinPortal>
        <Menu.Target>
          <div style={{ position: 'fixed', left: menu?.x ?? 0, top: menu?.y ?? 0, width: 0, height: 0 }} aria-hidden />
        </Menu.Target>
        <Menu.Dropdown>
          <Menu.Label>Capture Wireshark</Menu.Label>
          {menu?.ends.map((end) => (
            <Menu.Item
              key={`${end.node}:${end.interface}`}
              leftSection={<IconActivity size={14} />}
              onClick={() => navigate(`/nodes/${end.node}?tab=capture&iface=${encodeURIComponent(end.interface)}`)}
            >
              <span className="mono">{endName(end)}</span>
            </Menu.Item>
          ))}
          {wireEnds.length > 0 && (
            <>
              <Menu.Divider />
              <Menu.Label>Câble (hors configuration du nœud)</Menu.Label>
              {wireEnds.map((end) => {
                const cut = end.wire_up === false;
                return (
                  <Menu.Item
                    key={`${end.node}:${end.interface}`}
                    color={cut ? undefined : 'red'}
                    leftSection={cut ? <IconPlugConnected size={14} /> : <IconPlugConnectedX size={14} />}
                    onClick={() => wire.mutate({ end, up: cut })}
                  >
                    {cut ? 'Rétablir' : 'Couper'} <span className="mono">{endName(end)}</span>
                  </Menu.Item>
                );
              })}
            </>
          )}
        </Menu.Dropdown>
      </Menu>
      {(hints.length > 0 || custom || wide) && (
        <Group justify="space-between" align="flex-start" gap="sm" mt="xs" wrap="nowrap">
          <Text fz="xs" c="dimmed">
            {hints.join(' ')}
          </Text>
          <Group gap={4} wrap="nowrap" style={{ flexShrink: 0 }}>
            {custom && (
              <Button variant="subtle" size="compact-xs" onClick={reset}>
                Disposition automatique
              </Button>
            )}
            {wide && (
              <Button variant="subtle" size="compact-xs" onClick={() => setFit((v) => !v)}>
                {fit ? 'Taille réelle' : 'Ajuster à la largeur'}
              </Button>
            )}
          </Group>
        </Group>
      )}
    </>
  );
}

function LegendCable({ dash, stroke, arc }: { dash?: string; stroke: string; arc?: boolean }) {
  return (
    <svg width={20} height={10} aria-hidden>
      {arc ? (
        <path d="M 1 9 C 1 2, 19 2, 19 9" fill="none" stroke={stroke} strokeWidth={1.5} />
      ) : (
        <line x1={0} y1={5} x2={20} y2={5} stroke={stroke} strokeWidth={2} strokeDasharray={dash} strokeLinecap={dash === '2 4' ? 'round' : undefined} />
      )}
    </svg>
  );
}

/** Légende de la topologie, à placer dans l'en-tête de la carte. */
export function TopologyLegend() {
  const item = (icon: ReactNode, label: string) => (
    <Group gap={8} wrap="nowrap">
      {icon}
      <Text span fz="xs" c="dimmed">
        {label}
      </Text>
    </Group>
  );
  return (
    <Group gap="md" wrap="wrap" justify="flex-end" visibleFrom="xs">
      {item(<LegendCable stroke="var(--app-cable)" />, 'lien up')}
      {item(<LegendCable stroke="var(--state-err)" dash="6 4" />, 'lien down')}
      {item(<LegendCable stroke="var(--app-cable)" dash="2 4" />, 'état inconnu')}
      {item(<LegendCable stroke="var(--mantine-color-anchor)" arc />, 'session eBGP')}
    </Group>
  );
}
