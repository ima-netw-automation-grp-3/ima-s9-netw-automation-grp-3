/*
 * Disposition de la topologie du lab (module pur, sans React ni DOM) : position des équipements, tracé des câbles
 * et place des étiquettes, en pixels, à partir de la largeur réelle du conteneur.
 *
 * - Chaîne (client1 — sonic1 — sonic2 — client2) qui tient en largeur : une ligne, les étiquettes de ports entre
 *   les équipements et la session eBGP en arc.
 * - Chaîne sur un écran étroit : une colonne.
 * - Sinon, des étages : clients en bas, routeurs d'accès au-dessus, cœur/spines plus haut (distance au client le
 *   plus proche). Deux équipements reliés sont au même étage ou à des étages voisins, donc pas de câble long.
 *   L'ordre dans chaque étage limite les croisements, chaque équipement est centré sur ses voisins, et les
 *   étiquettes qui n'ont pas la place ne s'affichent qu'au survol du câble (câble « compact »).
 * - Disposition libre (freeLayout) : les équipements là où on les a déposés à la souris, câbles droits ; le dessin
 *   s'étend à tout son contenu.
 */

import type { LabState, LinkEnd, LinkInfo, NodeCard } from '../types';

export const LINE = 16; // hauteur d'une ligne d'étiquette (mono 12/16)
export const LABEL_GAP = 8; // entre un câble (ou un équipement) et ses étiquettes
const ROUTER_W = 136;
const CLIENT_W = 120;
const MIN_GAP = 132; // une étiquette de port (≈ 16 caractères à chasse fixe) et ses marges
const COLUMN_GAP = 120; // écart vertical entre deux équipements en disposition verticale
const NARROW = 640; // en dessous, une chaîne passe en colonne (mobile)

// disposition par étages
const NODE_GAP = 32; // entre deux équipements voisins non reliés
const PEER_GAP = 200; // entre deux routeurs voisins reliés : étiquettes et pastille eBGP sur le câble
const PARALLEL = 12; // écart entre deux câbles parallèles dans un étage (et entre leurs arcs)
const SLOT = 16; // écart entre les arrivées de câbles le long du bord d'une tuile
const BAND_MIN = 72; // écart vertical minimal entre deux étages
const CABLE_SHOW = 32; // câble visible en plus des étiquettes entre deux étages
const SLANT = 0.3; // partie en biais d'un câble entre étages : hauteur par pixel d'écart horizontal…
const SLANT_MAX = 160; // … plafonnée
const CHAR_W = 7.2; // chasse de la police mono à 12 px
const TAG_W = 132; // pastille « eBGP Established »
const TAG_H = 16;
const ARC_BASE = 56; // hauteur de l'arc le plus bas (lien dans l'étage entre équipements non voisins)
const ARC_STEP = 18; // par équipement enjambé en plus
const PAD = 4; // marge autour du dessin
const MAX_STRETCH = 1.5; // étirement horizontal maximal pour occuper la largeur
const NEW_ROW_GAP = 80; // disposition libre : rangée des nœuds sans position (ajoutés depuis), sous le dessin

export interface Point {
  x: number;
  y: number;
}

interface Box extends Point {
  w: number;
  h: number;
}

export interface Tile extends Box {
  node: NodeCard; // x, y : centre
}

/** Étiquette d'extrémité de câble ; (x, y) est son coin haut-gauche, son coin haut-droit ou son centre. */
export interface LabelSpot extends Point {
  end: LinkEnd;
  anchor: 'start' | 'end' | 'middle';
}

/** Arc (cubique) d'un lien entre deux équipements non voisins du même étage. */
export interface Arc {
  x0: number;
  y0: number;
  x3: number;
  y3: number;
  c: number; // ordonnée des points de contrôle
}

/** Câble de la disposition par étages. */
export interface Wire {
  link: LinkInfo;
  points: Point[]; // tracé de a vers z ; ses bouts partent du bord des tuiles ou passent dessous
  arc: Arc | null; // remplace le tracé droit
  ledA: Point; // port de l'extrémité a, sur le bord de la tuile
  ledZ: Point;
  labels: LabelSpot[];
  tag: Point | null; // pastille eBGP (ou « down »), point eBGP si compact : place réservée entre routeurs
  session: boolean; // premier câble de sa paire d'équipements : c'est lui qui porte la session eBGP
  compact: boolean; // étiquettes au survol seulement
}

export interface Geometry {
  mode: 'row' | 'column' | 'tiered' | 'free';
  width: number; // largeur du dessin
  height: number;
  tiles: Record<string, Tile>;
  rise: number; // hauteur de l'arc eBGP au-dessus du câble (disposition en ligne)
  wires: Wire[]; // dispositions par étages et libre
  shift?: Point; // disposition libre : décalage ajouté aux positions (contenu sorti à gauche ou en haut)
}

interface Rect {
  l: number;
  t: number;
  r: number;
  b: number;
}

export const tileW = (n: NodeCard) => (n.role === 'router' ? ROUTER_W : CLIENT_W);
export const tileH = (n: NodeCard) => (n.role === 'router' ? 56 : 40) + (n.phase === 'ready' ? 0 : LINE);
/** Hauteur réservée par étages : la ligne d'état comptée même quand le nœud est prêt (rien ne bouge au boot). */
const tileMaxH = (n: NodeCard) => (n.role === 'router' ? 56 : 40) + LINE;
export const labelLines = (e: LinkEnd) => 1 + e.addresses.length;

/** Point où un câble parti de `start` (le centre par défaut) sort du bord d'un équipement, en direction de `to`. */
export function exitPoint(from: Box, to: Point, start: Point = from): Point {
  const dx = to.x - start.x;
  const dy = to.y - start.y;
  const tx = dx ? (from.x + (Math.sign(dx) * from.w) / 2 - start.x) / dx : Infinity;
  const ty = dy ? (from.y + (Math.sign(dy) * from.h) / 2 - start.y) / dy : Infinity;
  const t = Math.min(tx, ty);
  return { x: start.x + dx * t, y: start.y + dy * t };
}

export const arcPath = (a: Arc) => `M ${a.x0} ${a.y0} C ${a.x0} ${a.c}, ${a.x3} ${a.c}, ${a.x3} ${a.y3}`;

/** Ordre des nœuds si la topologie est une chaîne (client1 — sonic1 — sonic2 — client2), sinon null. */
export function chainOrder(nodes: NodeCard[], links: LinkInfo[]): string[] | null {
  const names = nodes.map((n) => n.name);
  const adj = new Map<string, Set<string>>(names.map((n) => [n, new Set<string>()]));
  for (const l of links) {
    adj.get(l.a.node)?.add(l.z.node);
    adj.get(l.z.node)?.add(l.a.node);
  }
  const isPath =
    names.length > 1 && links.length === names.length - 1 && [...adj.values()].every((s) => s.size >= 1 && s.size <= 2);
  if (!isPath) return null;
  const ends = names.filter((n) => adj.get(n)!.size === 1);
  const start = ends.find((n) => nodes.find((x) => x.name === n)?.role === 'client') ?? ends[0];
  const order: string[] = [];
  let prev: string | null = null;
  let cur: string | undefined = start;
  while (cur && !order.includes(cur)) {
    order.push(cur);
    const next: string | undefined = [...adj.get(cur)!].find((n) => n !== prev);
    prev = cur;
    cur = next;
  }
  return order.length === names.length ? order : null;
}

/**
 * Chaîne horizontale si elle tient dans la largeur (étiquettes de ports entre les équipements), chaîne verticale
 * sur un écran étroit, sinon disposition par étages.
 */
export function layout(lab: LabState, width: number): Geometry {
  const order = chainOrder(lab.nodes, lab.links);
  if (order) {
    const row = rowLayout(lab, order, width);
    if (row) return row;
    if (width < NARROW) return columnLayout(lab, order, width);
  }
  return tieredLayout(lab, width);
}

function rowLayout(lab: LabState, order: string[], width: number): Geometry | null {
  const byName = new Map(lab.nodes.map((n) => [n.name, n]));
  const chain = order.map((name) => byName.get(name)!);
  const total = chain.reduce((sum, n) => sum + tileW(n), 0);
  const gap = (width - total) / (chain.length - 1);
  if (gap < MIN_GAP) return null;
  const labelH = LINE * Math.max(2, ...lab.links.flatMap((l) => [labelLines(l.a), labelLines(l.z)]));
  const half = Math.max(...chain.map((n) => tileH(n) / 2));
  const side = Math.max(LABEL_GAP + labelH, half);
  const rise = LABEL_GAP + labelH + 24;
  const cy = lab.links.some((l) => l.bgp) ? rise + 20 : side + 12;
  const tiles: Record<string, Tile> = {};
  let x = 0;
  for (const n of chain) {
    tiles[n.name] = { node: n, x: x + tileW(n) / 2, y: cy, w: tileW(n), h: tileH(n) };
    x += tileW(n) + gap;
  }
  return { mode: 'row', width, height: cy + side + 12, tiles, rise, wires: [] };
}

function columnLayout(lab: LabState, order: string[], width: number): Geometry {
  const byName = new Map(lab.nodes.map((n) => [n.name, n]));
  const tiles: Record<string, Tile> = {};
  let y = LINE / 2; // place de la ligne d'état réservée en disposition libre : rien ne saute au premier déplacement
  for (const n of order.map((name) => byName.get(name)!)) {
    tiles[n.name] = { node: n, x: ROUTER_W / 2, y: y + tileH(n) / 2, w: tileW(n), h: tileH(n) };
    y += tileH(n) + COLUMN_GAP;
  }
  return { mode: 'column', width, height: y - COLUMN_GAP + LINE / 2, tiles, rise: 0, wires: [] };
}

// --- disposition par étages -------------------------------------------------------------------------------

interface Skeleton {
  tiers: string[][]; // de haut en bas, dans l'ordre de dessin
  level: Map<string, number>; // étage de chaque nœud (0 = en haut)
  x: Map<string, number>; // abscisse du centre, bord gauche du dessin à 0
}

// La structure (étages, ordre, abscisses) ne dépend que des nœuds et des liens : recalculée seulement s'ils changent.
let lastSkeleton: { key: string; value: Skeleton } | null = null;

function skeleton(nodes: NodeCard[], links: LinkInfo[]): Skeleton {
  const key = JSON.stringify([nodes.map((n) => [n.name, n.role]), links.map((l) => [l.a.node, l.z.node])]);
  if (lastSkeleton?.key !== key) lastSkeleton = { key, value: buildSkeleton(nodes, links) };
  return lastSkeleton.value;
}

/** Étage de chaque nœud : distance au client le plus proche, la plus grande en haut (étage 0). */
function levels(nodes: NodeCard[], nbrs: Map<string, string[]>): Map<string, number> {
  const dist = new Map<string, number>();
  const queue = nodes.filter((n) => n.role !== 'router').map((n) => n.name);
  for (const n of queue) dist.set(n, 0);
  for (let i = 0; i < queue.length; i++) {
    for (const v of nbrs.get(queue[i])!) {
      if (!dist.has(v)) {
        dist.set(v, dist.get(queue[i])! + 1);
        queue.push(v);
      }
    }
  }
  for (const n of nodes) if (!dist.has(n.name)) dist.set(n.name, 1); // routeurs sans client dans leur composante
  const present = [...new Set(dist.values())].sort((a, b) => b - a);
  return new Map([...dist].map(([n, d]) => [n, present.indexOf(d)]));
}

/** Moindres carrés pondérés sous contrainte y[i] <= y[i + 1] (algorithme PAV). */
function isotonic(target: number[], weight: number[]): number[] {
  const blocks: { v: number; w: number; n: number }[] = [];
  target.forEach((v, i) => {
    let b = { v, w: weight[i], n: 1 };
    while (blocks.length && blocks[blocks.length - 1].v > b.v) {
      const p = blocks.pop()!;
      b = { v: (p.v * p.w + b.v * b.w) / (p.w + b.w), w: p.w + b.w, n: p.n + b.n };
    }
    blocks.push(b);
  });
  return blocks.flatMap((b) => Array<number>(b.n).fill(b.v));
}

function buildSkeleton(nodes: NodeCard[], links: LinkInfo[]): Skeleton {
  const fileIdx = new Map(nodes.map((n, i) => [n.name, i]));
  const byName = new Map(nodes.map((n) => [n.name, n]));
  const adj = new Map(nodes.map((n) => [n.name, new Set<string>()]));
  for (const l of links) {
    if (l.a.node === l.z.node) continue;
    const a = adj.get(l.a.node);
    const z = adj.get(l.z.node);
    if (!a || !z) continue;
    a.add(l.z.node);
    z.add(l.a.node);
  }
  const nbrs = new Map([...adj].map(([n, s]) => [n, [...s].sort((p, q) => fileIdx.get(p)! - fileIdx.get(q)!)]));
  const level = levels(nodes, nbrs);
  const lv = (n: string) => level.get(n)!;
  const tiers: string[][] = Array.from({ length: Math.max(...level.values()) + 1 }, () => []);
  for (const n of nodes) tiers[lv(n.name)].push(n.name);

  // Ordre initial : parcours en profondeur des liens internes à l'étage depuis une extrémité de chaîne (un anneau
  // ou une chaîne de routeurs reste contigu), composantes dans l'ordre du fichier.
  const initialOrder = (tier: string[]): string[] => {
    const k = lv(tier[0]);
    const peers = (u: string) => nbrs.get(u)!.filter((v) => lv(v) === k);
    const seen = new Set<string>();
    const out: string[] = [];
    for (const u of tier) {
      if (seen.has(u)) continue;
      const comp = new Set([u]);
      const queue = [u];
      for (let i = 0; i < queue.length; i++) {
        for (const v of peers(queue[i])) {
          if (!comp.has(v)) {
            comp.add(v);
            queue.push(v);
          }
        }
      }
      const stack = [tier.find((v) => comp.has(v) && peers(v).length === 1) ?? u];
      while (stack.length) {
        const v = stack.pop()!;
        if (seen.has(v)) continue;
        seen.add(v);
        out.push(v);
        stack.push(...peers(v).filter((p) => !seen.has(p)).reverse());
      }
    }
    return out;
  };

  // Coût d'un ordre : croisements entre étages voisins + équipements enjambés par les arcs dans un étage.
  const cost = (ts: string[][]) => {
    const pos = new Map<string, number>();
    ts.forEach((t) => t.forEach((n, i) => pos.set(n, i)));
    let c = 0;
    ts.forEach((t, k) => {
      const down: [number, number][] = [];
      for (const u of t) {
        for (const v of nbrs.get(u)!) {
          if (lv(v) === k + 1) down.push([pos.get(u)!, pos.get(v)!]);
          else if (lv(v) === k && pos.get(u)! < pos.get(v)!) c += pos.get(v)! - pos.get(u)! - 1;
        }
      }
      for (let i = 0; i < down.length; i++) {
        for (let j = i + 1; j < down.length; j++) {
          if ((down[i][0] - down[j][0]) * (down[i][1] - down[j][1]) < 0) c++;
        }
      }
    });
    return c;
  };

  // Barycentre : chaque nœud se range à la position moyenne de ses voisins de l'étage de référence.
  const reorder = (ts: string[][], k: number, ref: number) => {
    const pos = new Map<string, number>();
    ts.forEach((t) => t.forEach((n, i) => pos.set(n, (i + 0.5) / t.length)));
    const mean = (vs: string[]) => vs.reduce((s, v) => s + pos.get(v)!, 0) / vs.length;
    const keyed = ts[k].map((n, i) => {
      const outer = nbrs.get(n)!.filter((v) => lv(v) === ref);
      const inner = nbrs.get(n)!.filter((v) => lv(v) === k);
      return { n, i, b: outer.length ? mean(outer) : inner.length ? mean(inner) : pos.get(n)! };
    });
    keyed.sort((p, q) => p.b - q.b || p.i - q.i);
    ts[k] = keyed.map((e) => e.n);
  };

  const cur = tiers.map(initialOrder);
  let best = cur.map((t) => [...t]);
  let bestCost = cost(best);
  for (let it = 0; it < 8 && bestCost > 0; it++) {
    for (let k = 1; k < cur.length; k++) reorder(cur, k, k - 1);
    for (let k = cur.length - 2; k >= 0; k--) reorder(cur, k, k + 1);
    const c = cost(cur);
    if (c < bestCost) {
      best = cur.map((t) => [...t]);
      bestCost = c;
    }
  }
  // échanges de voisins tant que le coût baisse
  for (let improved = true, round = 0; improved && bestCost > 0 && round < 20; round++) {
    improved = false;
    for (const t of best) {
      for (let i = 0; i + 1 < t.length; i++) {
        [t[i], t[i + 1]] = [t[i + 1], t[i]];
        const c = cost(best);
        if (c < bestCost) {
          bestCost = c;
          improved = true;
        } else {
          [t[i], t[i + 1]] = [t[i + 1], t[i]];
        }
      }
    }
  }

  // Abscisses : écart minimal entre voisins d'étage, puis chaque nœud centré sur ses voisins des étages adjacents.
  const w = (n: string) => tileW(byName.get(n)!);
  const gap = (u: string, v: string) => {
    if (!nbrs.get(u)!.includes(v)) return NODE_GAP;
    return byName.get(u)!.role === 'router' && byName.get(v)!.role === 'router' ? PEER_GAP : MIN_GAP;
  };
  const offsets = best.map((t) => {
    const off = [0];
    for (let i = 1; i < t.length; i++) off.push(off[i - 1] + w(t[i - 1]) / 2 + gap(t[i - 1], t[i]) + w(t[i]) / 2);
    return off;
  });
  const x = new Map<string, number>();
  best.forEach((t, k) => t.forEach((n, i) => x.set(n, offsets[k][i] - offsets[k][t.length - 1] / 2)));
  const align = (k: number, refs: number[]) => {
    const tier = best[k];
    const ns = tier.map((n) => nbrs.get(n)!.filter((v) => refs.includes(lv(v))));
    const want = tier.map((n, i) => (ns[i].length ? ns[i].reduce((s, v) => s + x.get(v)!, 0) / ns[i].length : x.get(n)!));
    // Voisins d'étage qui visent le même point (mêmes voisins : les spines d'une fabric) : répartis sur l'étendue
    // de leurs voisins plutôt que tassés au milieu. Les clients d'un même routeur (étendue nulle) restent groupés.
    for (let i = 0; i < tier.length; ) {
      let j = i + 1;
      while (j < tier.length && ns[j].length && Math.abs(want[j] - want[i]) < 0.01) j++;
      if (j - i > 1 && ns[i].length) {
        const xs = ns.slice(i, j).flat().map((v) => x.get(v)!);
        const lo = Math.min(...xs);
        const hi = Math.max(...xs);
        for (let m = i; m < j; m++) want[m] = lo + ((m - i + 0.5) * (hi - lo)) / (j - i);
      }
      i = j;
    }
    const target = want.map((v, i) => v - offsets[k][i]);
    isotonic(target, ns.map((list) => list.length || 0.25)).forEach((y, i) => x.set(tier[i], y + offsets[k][i]));
  };
  // la dernière passe remonte : chaque étage centré sur celui du dessous (le plus large, en général les clients)
  for (let it = 0; it < 4; it++) {
    for (let k = 1; k < best.length; k++) align(k, [k - 1]);
    for (let k = best.length - 2; k >= 0; k--) align(k, [k + 1]);
  }
  const left = Math.min(...nodes.map((n) => x.get(n.name)! - tileW(n) / 2));
  for (const n of nodes) x.set(n.name, x.get(n.name)! - left);
  return { tiers: best, level, x };
}

interface LabelSize {
  w: number;
  h: number; // hauteur réservée : au moins deux lignes, comme en ligne (l'adresse arrive après le boot)
  real: number;
}

function labelSize(e: LinkEnd): LabelSize {
  const first = `${e.port ?? e.interface}${e.port ? ` · ${e.interface}` : ''}${e.admin === 'down' ? ' (shutdown)' : ''}`;
  const chars = Math.max(first.length, ...e.addresses.map((a) => a.length));
  return { w: chars * CHAR_W + 8, h: Math.max(2, labelLines(e)) * LINE, real: labelLines(e) * LINE };
}

const centered = (p: Point, w: number, h: number): Rect => ({ l: p.x - w / 2, t: p.y - h / 2, r: p.x + w / 2, b: p.y + h / 2 });
const overlaps = (a: Rect, b: Rect) => a.l < b.r && b.l < a.r && a.t < b.b && b.t < a.b;

/** Une façon de poser les étiquettes d'un câble (et sa pastille) : positions et rectangles occupés. */
interface Placement {
  labels: LabelSpot[];
  rects: Rect[];
}

// En cours de calcul : place des étiquettes, autres placements possibles (disposition libre : de chaque côté du
// câble), partie libre du câble (où poser la pastille ou le point eBGP).
type Draft = Wire & { rects: Rect[]; options?: Placement[]; along: [Point, Point] | null };

/** Liens dessinables : deux extrémités distinctes, toutes deux dans la topologie. */
const cables = (lab: LabState, byName: Map<string, NodeCard>) =>
  lab.links.filter((l) => l.a.node !== l.z.node && byName.has(l.a.node) && byName.has(l.z.node));

const pairKey = (l: LinkInfo) => [l.a.node, l.z.node].sort().join(' ');

/** Câbles regroupés par paire d'équipements : plusieurs câbles entre les mêmes équipements sont parallèles. */
function parallels(links: LinkInfo[]): Map<string, LinkInfo[]> {
  const groups = new Map<string, LinkInfo[]>();
  for (const l of links) groups.set(pairKey(l), [...(groups.get(pairKey(l)) ?? []), l]);
  return groups;
}

/** Place réservée pour une pastille eBGP : session connue, ou lien entre deux routeurs (elle viendra au boot). */
const reservesTag = (l: LinkInfo, byName: Map<string, NodeCard>) =>
  l.bgp != null || (byName.get(l.a.node)!.role === 'router' && byName.get(l.z.node)!.role === 'router');

/**
 * Étiquettes : placées câble par câble dans l'ordre du fichier, au premier placement possible qui ne recouvre ni une
 * tuile ni une étiquette déjà posée et ne sort pas du dessin (`origin` : ni à gauche ni au-dessus) ; un câble sans
 * place passe en compact, et tous au-delà d'un câble sur trois (et de deux câbles : un petit lab reste lisible). Le
 * point eBGP d'un câble compact se pose au milieu de sa partie libre, ou plus loin le long du câble si une tuile ou
 * une étiquette y est déjà.
 */
function settle(wires: Draft[], tileRects: Rect[], origin?: Point) {
  const placed = [...tileRects];
  const free = (rs: Rect[]) =>
    rs.every((r, i) => !placed.some((p) => overlaps(r, p)) && !rs.slice(0, i).some((p) => overlaps(r, p)));
  const fits = (rs: Rect[]) => !origin || rs.every((r) => r.l >= origin.x && r.t >= origin.y);
  let crowded = 0;
  for (const w of wires) {
    const options = w.options ?? [{ labels: w.labels, rects: w.rects }];
    const pick = w.compact ? undefined : options.find((o) => fits(o.rects) && free(o.rects));
    // compact : les étiquettes du survol au premier placement qui reste dans le dessin
    const chosen = pick ?? options.find((o) => fits(o.rects)) ?? options[0];
    w.labels = chosen.labels;
    w.rects = chosen.rects;
    if (pick) placed.push(...pick.rects);
    else {
      w.compact = true;
      crowded++;
    }
  }
  if (crowded > Math.max(2, wires.length / 3)) for (const w of wires) w.compact = true;
  const shown = [...tileRects, ...wires.filter((w) => !w.compact).flatMap((w) => w.rects)];
  for (const w of wires) {
    if (!w.compact || !w.tag || !w.along) continue;
    const [p, q] = w.along;
    w.tag =
      [0.5, 0.35, 0.65, 0.2, 0.8]
        .map((t) => ({ x: p.x + (q.x - p.x) * t, y: p.y + (q.y - p.y) * t }))
        .find((s) => !shown.some((r) => overlaps(centered(s, 14, 14), r))) ?? w.tag;
  }
}

/**
 * Cadre le dessin sur son contenu (`extent`) en translatant toutes les coordonnées. Par étages : marge PAD tout autour.
 * Disposition libre (`anchors` : les tuiles) : l'origine ne bouge que si une tuile sort à gauche ou en haut, pour que
 * rien ne saute sous la souris ; le dessin s'agrandit vers la droite et le bas.
 */
function frame(tiles: Record<string, Tile>, wires: Draft[], extent: Rect[], anchors?: Rect[]) {
  const grow = anchors != null;
  const minX = Math.min(...(anchors ?? extent).map((r) => r.l));
  const minY = Math.min(...(anchors ?? extent).map((r) => r.t));
  const dx = grow ? Math.max(0, -minX) : PAD - minX;
  const dy = grow ? Math.max(0, -minY) : PAD - minY;
  const mv = (p: Point) => {
    p.x += dx;
    p.y += dy;
  };
  Object.values(tiles).forEach(mv);
  for (const w of wires) {
    [...w.points, w.ledA, w.ledZ, ...w.labels].forEach(mv);
    if (w.tag) mv(w.tag);
    if (w.arc) w.arc = { x0: w.arc.x0 + dx, y0: w.arc.y0 + dy, x3: w.arc.x3 + dx, y3: w.arc.y3 + dy, c: w.arc.c + dy };
    w.rects = w.rects.map((r) => ({ l: r.l + dx, t: r.t + dy, r: r.r + dx, b: r.b + dy }));
  }
  const pad = grow ? 0 : PAD;
  return {
    width: Math.max(...extent.map((r) => r.r)) + dx + pad,
    height: Math.max(...extent.map((r) => r.b)) + dy + pad,
    shift: { x: dx, y: dy },
  };
}

/** Distance à parcourir depuis p le long de u pour qu'une étiquette centrée (w × h) ne touche plus le rectangle R. */
function clearance(R: Rect, p: Point, u: Point, s: { w: number; h: number }): number {
  const g = LABEL_GAP; // dégage aussi la LED du port, posée sur le bord de la tuile
  const tx = u.x > 0 ? (R.r + g + s.w / 2 - p.x) / u.x : u.x < 0 ? (R.l - g - s.w / 2 - p.x) / u.x : Infinity;
  const ty = u.y > 0 ? (R.b + g + s.h / 2 - p.y) / u.y : u.y < 0 ? (R.t - g - s.h / 2 - p.y) / u.y : Infinity;
  return Math.max(0, Math.min(tx, ty));
}

/**
 * Disposition libre : chaque équipement au centre où on l'a déposé (pixels du dessin), câbles droits de centre à
 * centre, étiquettes sur le câble juste hors de chaque tuile, pastille eBGP au milieu. Les nœuds sans position
 * (ajoutés depuis) se rangent en ligne sous le dessin.
 */
export function freeLayout(lab: LabState, positions: Record<string, Point>): Geometry {
  const { nodes } = lab;
  const byName = new Map(nodes.map((n) => [n.name, n]));
  const links = cables(lab, byName);
  const box = (n: NodeCard, p: Point) => centered(p, tileW(n), tileMaxH(n));
  const at = new Map<string, Point>();
  for (const n of nodes) if (positions[n.name]) at.set(n.name, { ...positions[n.name] });
  const known = nodes.filter((n) => at.has(n.name)).map((n) => box(n, at.get(n.name)!));
  const below = (known.length ? Math.max(...known.map((r) => r.b)) : 0) + NEW_ROW_GAP;
  let x = known.length ? Math.min(...known.map((r) => r.l)) : 0;
  for (const n of nodes) {
    if (at.has(n.name)) continue;
    at.set(n.name, { x: x + tileW(n) / 2, y: below + tileMaxH(n) / 2 });
    x += tileW(n) + NODE_GAP;
  }
  const tiles: Record<string, Tile> = {};
  for (const n of nodes) tiles[n.name] = { node: n, ...at.get(n.name)!, w: tileW(n), h: tileH(n) };
  const reserved = (n: string) => box(byName.get(n)!, tiles[n]);

  const groups = parallels(links);
  const degree = new Map<string, number>();
  for (const l of links) for (const n of [l.a.node, l.z.node]) degree.set(n, (degree.get(n) ?? 0) + 1);
  const sessions = new Set<string>();
  const wires: Draft[] = links.map((l) => {
    const group = groups.get(pairKey(l))!;
    const off = (group.indexOf(l) - (group.length - 1) / 2) * PARALLEL;
    const session = !sessions.has(pairKey(l));
    sessions.add(pairKey(l));
    const A = tiles[l.a.node];
    const Z = tiles[l.z.node];
    const len = Math.hypot(Z.x - A.x, Z.y - A.y) || 1;
    const u = { x: (Z.x - A.x) / len, y: (Z.y - A.y) / len };
    const pa = { x: A.x - u.y * off, y: A.y + u.x * off };
    const pz = { x: Z.x - u.y * off, y: Z.y + u.x * off };
    // Partie visible du câble (entre les bords réservés des tuiles) : la pastille eBGP en occupe le milieu.
    const ea = exitPoint({ ...A, h: tileMaxH(A.node) }, pz, pa);
    const ez = exitPoint({ ...Z, h: tileMaxH(Z.node) }, pa, pz);
    const tag = reservesTag(l, byName) ? { x: (ea.x + ez.x) / 2, y: (ea.y + ez.y) / 2 } : null;
    // Étiquette de chaque bout juste hors de sa tuile. D'abord sur le câble, qui la traverse : on voit sans hésiter à
    // quel câble elle appartient. Sinon à côté du câble, du côté où elle reste le plus près de sa tuile ; à égalité
    // (câble droit), comme en ligne : celle de a au-dessus d'un câble vers la droite, celle de z dessous.
    const n = { x: u.y, y: -u.x };
    const sa = labelSize(l.a);
    const sz = labelSize(l.z);
    const spots = (s: LabelSize, from: Point, dir: Point, tile: string, first: number) => {
      const place = (k: number) => {
        const off = k ? (Math.abs(n.x) * s.w) / 2 + (Math.abs(n.y) * s.h) / 2 + LABEL_GAP : 0;
        const o = { x: n.x * off * k, y: n.y * off * k };
        const t = clearance(reserved(tile), { x: from.x + o.x, y: from.y + o.y }, dir, s);
        return { t, center: { x: from.x + dir.x * t + o.x, y: from.y + dir.y * t + o.y } };
      };
      const sides = [place(first), place(-first)].sort((p, q) => (Math.abs(p.t - q.t) < 1 ? 0 : p.t - q.t));
      return [place(0).center, ...sides.map((c) => c.center)]; // sur le câble, puis côté proche, puis l'autre côté
    };
    const back = { x: -u.x, y: -u.y };
    const atA = spots(sa, pa, u, l.a.node, 1);
    const atZ = spots(sz, pz, back, l.z.node, -1);
    // Les deux étiquettes empilées sur le câble près d'un seul bout (comme par étages) : utile quand l'autre bout,
    // un routeur avec beaucoup de câbles, n'a pas la place. D'abord près du bout qui a le moins de câbles.
    const stacked = (nearA: boolean): [Point, Point] => {
      const [from, dir, tile, sNear, sFar]: [Point, Point, string, LabelSize, LabelSize] = nearA
        ? [pa, u, l.a.node, sa, sz]
        : [pz, back, l.z.node, sz, sa];
      const along = (s: LabelSize) => (Math.abs(dir.x) * s.w) / 2 + (Math.abs(dir.y) * s.h) / 2;
      const tNear = clearance(reserved(tile), from, dir, sNear);
      const tFar = tNear + along(sNear) + 4 + along(sFar);
      const near = { x: from.x + dir.x * tNear, y: from.y + dir.y * tNear };
      const far = { x: from.x + dir.x * tFar, y: from.y + dir.y * tFar };
      return nearA ? [near, far] : [far, near];
    };
    const aFirst = (degree.get(l.a.node) ?? 0) < (degree.get(l.z.node) ?? 0);
    const pairs: [Point, Point][] = [
      [atA[0], atZ[0]],
      stacked(aFirst),
      stacked(!aFirst),
      [atA[1], atZ[1]],
      [atA[1], atZ[2]],
      [atA[2], atZ[1]],
      [atA[2], atZ[2]],
    ];
    const options: Placement[] = pairs.map(([ca, cz]) => {
      const rects = [centered(ca, sa.w, sa.h), centered(cz, sz.w, sz.h)];
      if (tag) rects.push(centered(tag, TAG_W, TAG_H));
      return {
        labels: [
          { end: l.a, ...ca, anchor: 'middle' },
          { end: l.z, ...cz, anchor: 'middle' },
        ],
        rects,
      };
    });
    return {
      link: l,
      session,
      compact: false,
      points: [pa, pz],
      arc: null,
      ledA: exitPoint(A, pz, pa),
      ledZ: exitPoint(Z, pa, pz),
      ...options[0],
      options,
      tag,
      along: [ea, ez],
    };
  });

  const tileRects = nodes.map((n) => reserved(n.name));
  // origine du dessin : 0, ou plus loin si une tuile est sortie à gauche ou en haut (frame décale alors tout)
  settle(wires, tileRects, {
    x: Math.min(0, ...tileRects.map((r) => r.l)),
    y: Math.min(0, ...tileRects.map((r) => r.t)),
  });
  const { width, height, shift } = frame(tiles, wires, [...tileRects, ...wires.flatMap((w) => w.rects)], tileRects);
  return { mode: 'free', width, height, tiles, rise: 0, wires, shift };
}

function tieredLayout(lab: LabState, width: number): Geometry {
  const { nodes } = lab;
  if (!nodes.length) return { mode: 'tiered', width, height: 40, tiles: {}, rise: 0, wires: [] };
  const sk = skeleton(nodes, lab.links);
  const byName = new Map(nodes.map((n) => [n.name, n]));
  const links = cables(lab, byName);
  const lv = (n: string) => sk.level.get(n)!;
  const idx = new Map<string, number>();
  sk.tiers.forEach((t) => t.forEach((n, i) => idx.set(n, i)));
  const resH = (n: string) => tileMaxH(byName.get(n)!);
  const span = (l: LinkInfo) => Math.abs(idx.get(l.a.node)! - idx.get(l.z.node)!);
  const isArc = (l: LinkInfo) => lv(l.a.node) === lv(l.z.node) && span(l) > 1;

  // câbles parallèles, nombre de câbles par côté des tuiles
  const groups = parallels(links);
  const fanUp = new Map<string, number>();
  const fanDown = new Map<string, number>();
  for (const l of links) {
    if (lv(l.a.node) === lv(l.z.node)) continue;
    const [up, low] = lv(l.a.node) < lv(l.z.node) ? [l.a.node, l.z.node] : [l.z.node, l.a.node];
    fanDown.set(up, (fanDown.get(up) ?? 0) + 1);
    fanUp.set(low, (fanUp.get(low) ?? 0) + 1);
  }
  const hasTag = (l: LinkInfo) => reservesTag(l, byName);
  const arcHeight = (l: LinkInfo) =>
    ARC_BASE + ARC_STEP * (span(l) - 2) + PARALLEL * groups.get(pairKey(l))!.indexOf(l);

  // étirement horizontal pour occuper la largeur disponible
  const xs = nodes.map((n) => sk.x.get(n.name)!);
  const cL = Math.min(...xs);
  const cR = Math.max(...xs);
  const ends = Math.max(...nodes.map((n) => sk.x.get(n.name)! + tileW(n) / 2)) - (cR - cL);
  const f = cR > cL ? Math.min(MAX_STRETCH, Math.max(1, (width - 2 * PAD - 24 - ends) / (cR - cL))) : 1;
  const X = (n: string) => cL + (sk.x.get(n)! - cL) * f;

  // ordonnées : hauteur des étages, puis entre deux étages la place des étiquettes et des arcs
  const tierH = sk.tiers.map((t) => Math.max(...t.map(resH)));
  const arcMax = sk.tiers.map((_, k) =>
    Math.max(0, ...links.filter((l) => isArc(l) && lv(l.a.node) === k).map((l) => arcHeight(l) + TAG_H / 2)),
  );
  const band = sk.tiers.slice(1).map((_, k) => {
    let need = BAND_MIN;
    for (const l of links) {
      if (Math.min(lv(l.a.node), lv(l.z.node)) !== k || lv(l.a.node) === lv(l.z.node)) continue;
      const block = labelSize(l.a).h + 4 + labelSize(l.z).h;
      const slant = Math.min(SLANT_MAX, SLANT * Math.abs(X(l.a.node) - X(l.z.node)));
      need = Math.max(need, 2 * LABEL_GAP + block + Math.max(slant, (hasTag(l) ? TAG_H + LABEL_GAP : 0) + CABLE_SHOW));
    }
    return need + (k > 0 ? arcMax[k] : 0); // arcs sous l'étage k (au-dessus pour l'étage du haut)
  });
  const ys: number[] = [];
  sk.tiers.forEach((_, k) => ys.push(k ? ys[k - 1] + tierH[k - 1] / 2 + band[k - 1] + tierH[k] / 2 : 0));

  const tiles: Record<string, Tile> = {};
  for (const n of nodes) tiles[n.name] = { node: n, x: X(n.name), y: ys[lv(n.name)], w: tileW(n), h: tileH(n) };
  const reserved = (n: string): Rect => centered(tiles[n], tiles[n].w, resH(n));

  const other = (l: LinkInfo, n: string) => (l.a.node === n ? l.z.node : l.a.node);

  // pied des arcs sur la tuile : les plus grands au plus près du centre, pour qu'ils s'emboîtent
  const foot = new Map<string, number>();
  for (const n of nodes) {
    for (const right of [true, false]) {
      links
        .filter((l) => isArc(l) && (l.a.node === n.name || l.z.node === n.name))
        .filter((l) => (idx.get(n.name)! < idx.get(other(l, n.name))!) === right)
        .sort((p, q) => span(q) - span(p) || (p.id < q.id ? -1 : 1))
        .forEach((l, k) => foot.set(`${l.id}|${n.name}`, Math.min(14 + 12 * k, tileW(n) / 2 - 10)));
    }
  }

  // arrivées des câbles entre étages, réparties le long du bord de chaque tuile dans l'ordre de leurs autres bouts
  const slot = new Map<string, number>(); // `${id}|${nœud}` → décalage horizontal depuis le centre de la tuile
  for (const n of nodes) {
    for (const up of [true, false]) {
      const side = links
        .filter((l) => (l.a.node === n.name || l.z.node === n.name) && lv(l.a.node) !== lv(l.z.node))
        .filter((l) => (lv(other(l, n.name)) < lv(n.name)) === up)
        .sort((p, q) => X(other(p, n.name)) - X(other(q, n.name)) || links.indexOf(p) - links.indexOf(q));
      const step = side.length > 1 ? Math.min(SLOT, (tileW(n) - 16) / (side.length - 1)) : 0;
      side.forEach((l, i) => slot.set(`${l.id}|${n.name}`, (i - (side.length - 1) / 2) * step));
    }
  }

  const sessions = new Set<string>();
  const wires: Draft[] = links.map((l) => {
    const group = groups.get(pairKey(l))!;
    const off = (group.indexOf(l) - (group.length - 1) / 2) * PARALLEL;
    const session = !sessions.has(pairKey(l));
    sessions.add(pairKey(l));
    const ta = tiles[l.a.node];
    const tz = tiles[l.z.node];
    const sa = labelSize(l.a);
    const sz = labelSize(l.z);
    const base = { link: l, session, compact: false };

    if (lv(l.a.node) !== lv(l.z.node)) {
      // Entre deux étages : les deux étiquettes empilées sur la fin verticale du câble, au bout le moins chargé
      // (E) ; la partie en biais, depuis l'autre bout (O), porte la pastille eBGP.
      const aUp = lv(l.a.node) < lv(l.z.node);
      const [U, L] = aUp ? [ta, tz] : [tz, ta];
      const [sU, sL] = aUp ? [sa, sz] : [sz, sa];
      const bottomU = U.y + resH(U.node.name) / 2;
      const topL = L.y - resH(L.node.name) / 2;
      const nearLower = (fanUp.get(L.node.name) ?? 0) <= (fanDown.get(U.node.name) ?? 0);
      const [E, O] = nearLower ? [L, U] : [U, L];
      const ex = E.x + slot.get(`${l.id}|${E.node.name}`)!;
      // départ sur le bord de O tourné vers E (jamais par un flanc, même quand le câble est très en biais)
      const start = { x: O.x + slot.get(`${l.id}|${O.node.name}`)!, y: O.y + ((nearLower ? 1 : -1) * O.h) / 2 };
      const block = sU.h + 4 + sL.h;
      const top = nearLower ? topL - LABEL_GAP - block : bottomU + LABEL_GAP;
      const knee = { x: ex, y: nearLower ? top - LABEL_GAP : top + block + LABEL_GAP };
      const labels: LabelSpot[] = [
        { end: aUp ? l.a : l.z, x: ex, y: top + sU.h / 2, anchor: 'middle' },
        { end: aUp ? l.z : l.a, x: ex, y: top + block - sL.h / 2, anchor: 'middle' },
      ];
      const rects = [centered(labels[0], sU.w, sU.h), centered(labels[1], sL.w, sL.h)];
      const xAt = (y: number) => start.x + ((knee.x - start.x) * (y - start.y)) / (knee.y - start.y);
      const edgeO = nearLower ? bottomU : topL; // bord réservé de O : rien ne bouge quand sa tuile change de hauteur
      const along: [Point, Point] = [{ x: xAt(edgeO), y: edgeO }, { ...knee }];
      let tag: Point | null = null;
      if (hasTag(l)) {
        const ty = (knee.y + edgeO) / 2;
        tag = { x: xAt(ty), y: ty };
        rects.push(centered(tag, TAG_W, TAG_H));
      }
      const ledE = { x: ex, y: E.y + ((nearLower ? -1 : 1) * E.h) / 2 };
      const ledO = { ...start };
      const path = [start, knee, { x: ex, y: E.y }]; // de O vers E (la fin passe sous la tuile E)
      const aIsE = E === ta;
      return {
        ...base,
        points: aIsE ? path.reverse() : path,
        arc: null,
        ledA: aIsE ? ledE : ledO,
        ledZ: aIsE ? ledO : ledE,
        labels,
        tag,
        rects,
        along,
      };
    }

    const aLeft = idx.get(l.a.node)! < idx.get(l.z.node)!;
    const [Lt, Rt] = aLeft ? [ta, tz] : [tz, ta];
    const [eL, eR, sL, sR] = aLeft ? [l.a, l.z, sa, sz] : [l.z, l.a, sz, sa];

    if (!isArc(l)) {
      // voisins dans l'étage : câble horizontal, étiquettes comme en ligne, pastille au milieu
      const y = Lt.y + off;
      const xl = Lt.x + Lt.w / 2 + LABEL_GAP;
      const xr = Rt.x - Rt.w / 2 - LABEL_GAP;
      const from = { x: ta.x, y };
      const to = { x: tz.x, y };
      const tag = hasTag(l) ? { x: (xl + xr) / 2, y } : null;
      const rects: Rect[] = [
        { l: xl, t: y - LABEL_GAP - sL.h, r: xl + sL.w, b: y - LABEL_GAP },
        { l: xr - sR.w, t: y + LABEL_GAP, r: xr, b: y + LABEL_GAP + sR.h },
      ];
      if (tag) rects.push(centered(tag, TAG_W, TAG_H));
      return {
        ...base,
        points: [from, to],
        arc: null,
        ledA: exitPoint(ta, to, from),
        ledZ: exitPoint(tz, from, to),
        labels: [
          { end: eL, x: xl, y: y - LABEL_GAP - sL.real, anchor: 'start' },
          { end: eR, x: xr, y: y + LABEL_GAP, anchor: 'end' },
        ],
        tag,
        rects,
        along: [
          { x: xl - LABEL_GAP, y },
          { x: xr + LABEL_GAP, y },
        ],
      };
    }

    // non voisins dans l'étage : arc au-dessus de l'étage du haut, en dessous des autres
    const k = lv(l.a.node);
    const dir = k === 0 ? -1 : 1;
    const edge = ys[k] + (dir * tierH[k]) / 2; // bord réservé de l'étage
    const x0 = Lt.x + foot.get(`${l.id}|${Lt.node.name}`)!;
    const x3 = Rt.x - foot.get(`${l.id}|${Rt.node.name}`)!;
    const y0 = Lt.y + (dir * Lt.h) / 2;
    const y3 = Rt.y + (dir * Rt.h) / 2;
    const apex = edge + dir * arcHeight(l);
    const ym = (y0 + y3) / 2;
    const arc = { x0, y0, x3, y3, c: ym + (apex - ym) / 0.75 }; // une cubique n'atteint que 3/4 de ses contrôles
    // étiquettes à l'intérieur de l'arc, contre ses pieds
    const rectAt = (x: number, s: LabelSize, right: boolean): Rect => {
      const t = dir < 0 ? edge - LABEL_GAP - s.h : edge + LABEL_GAP;
      return right ? { l: x - s.w, t, r: x, b: t + s.h } : { l: x, t, r: x + s.w, b: t + s.h };
    };
    const tag = hasTag(l) ? { x: (x0 + x3) / 2, y: apex } : null;
    const rects = [rectAt(x0 + 6, sL, false), rectAt(x3 - 6, sR, true)];
    if (tag) rects.push(centered(tag, TAG_W, TAG_H));
    const feet = [
      { x: x0, y: y0 },
      { x: x3, y: y3 },
    ];
    return {
      ...base,
      points: [{ ...feet[aLeft ? 0 : 1] }, { ...feet[aLeft ? 1 : 0] }],
      arc,
      ledA: { ...feet[aLeft ? 0 : 1] },
      ledZ: { ...feet[aLeft ? 1 : 0] },
      labels: [
        { end: eL, x: x0 + 6, y: dir < 0 ? edge - LABEL_GAP - sL.real : edge + LABEL_GAP, anchor: 'start' },
        { end: eR, x: x3 - 6, y: dir < 0 ? edge - LABEL_GAP - sR.real : edge + LABEL_GAP, anchor: 'end' },
      ],
      tag,
      rects,
      along: null, // pastille au sommet de l'arc
    };
  });

  const tileRects = nodes.map((n) => reserved(n.name));
  settle(wires, tileRects);
  // cadrage : tout le dessin (tuiles, étiquettes même au survol, sommets des arcs)
  const extent = [
    ...tileRects,
    ...wires.flatMap((w) => w.rects),
    ...wires.flatMap(({ arc: a }) => (a ? [centered({ x: (a.x0 + a.x3) / 2, y: (a.y0 + a.y3) / 8 + (3 * a.c) / 4 }, 0, 0)] : [])),
  ];
  const drawn = frame(tiles, wires, extent);
  return { mode: 'tiered', width: drawn.width, height: drawn.height, tiles, rise: 0, wires };
}
