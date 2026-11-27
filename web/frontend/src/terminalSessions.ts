import { Terminal as XTerm } from '@xterm/xterm';
import { FitAddon } from '@xterm/addon-fit';

import { TERMINAL_THEME } from './theme';

/**
 * Sessions de terminal gardées hors de React : changer d'onglet ou de page ne détruit ni le terminal xterm
 * (contenu, historique) ni le WebSocket (shell dans le nœud). Le composant Terminal ne fait que rattacher
 * l'élément DOM de la session. Une session ne disparaît que sur demande (nouvelle session) ou à la fermeture
 * de l'onglet du navigateur.
 */

export type TerminalStatus = 'connecting' | 'open' | 'closed' | 'error';

// police du terminal : celle de l'interface pour le code ; xterm mesure les glyphes à l'ouverture,
// donc on démarre sur la police système et on bascule une fois JetBrains Mono chargée
const FONT = '"JetBrains Mono Variable", "JetBrains Mono", ui-monospace, SFMono-Regular, Menlo, monospace';
const FALLBACK_FONT = 'ui-monospace, SFMono-Regular, Menlo, monospace';
const FONT_SPEC = '13px "JetBrains Mono Variable"';

export class TerminalSession {
  status: TerminalStatus = 'connecting';
  private readonly host = document.createElement('div');
  private readonly term: XTerm;
  private readonly fit = new FitAddon();
  private ws: WebSocket | null = null;
  private readonly listeners = new Set<(status: TerminalStatus) => void>();
  private disposed = false;
  private started = false;

  constructor(
    readonly node: string,
    readonly shell: string,
  ) {
    const fontReady = document.fonts?.check?.(FONT_SPEC) ?? true;
    this.host.style.height = '100%';
    this.term = new XTerm({
      cursorBlink: true,
      fontFamily: fontReady ? FONT : FALLBACK_FONT,
      fontSize: 13,
      scrollback: 5000,
      theme: TERMINAL_THEME,
    });
    this.term.loadAddon(this.fit);
    if (!fontReady) {
      void document.fonts.load(FONT_SPEC).then(() => {
        if (this.disposed) return;
        this.term.options.fontFamily = FONT;
        this.refit();
      });
    }
    this.term.onData((data) => this.send({ type: 'input', data }));
    this.term.onResize(({ cols, rows }) => this.send({ type: 'resize', cols, rows }));
  }

  subscribe(fn: (status: TerminalStatus) => void): () => void {
    this.listeners.add(fn);
    return () => this.listeners.delete(fn);
  }

  /** Affiche la session dans `container` (la retire de l'endroit précédent) ; la connexion démarre au premier rattachement. */
  attach(container: HTMLElement): void {
    container.appendChild(this.host);
    if (!this.started) {
      this.started = true;
      this.term.open(this.host);
      this.refit();
      this.connect();
    } else {
      this.refit();
      this.term.refresh(0, this.term.rows - 1);
    }
    this.term.focus();
  }

  /** Libère l'élément du conteneur sans toucher à la session. */
  detach(container: HTMLElement): void {
    if (this.host.parentElement === container) container.removeChild(this.host);
  }

  /** Ajuste le terminal à son conteneur ; ignoré tant que celui-ci est masqué (taille nulle). */
  refit(): void {
    const parent = this.host.parentElement;
    if (!parent || parent.clientWidth === 0 || parent.clientHeight === 0) return;
    try {
      this.fit.fit();
    } catch {
      /* conteneur pas encore dimensionné */
    }
  }

  dispose(): void {
    this.disposed = true;
    this.ws?.close();
    this.term.dispose();
    this.host.remove();
    this.listeners.clear();
  }

  private setStatus(status: TerminalStatus): void {
    this.status = status;
    this.listeners.forEach((fn) => fn(status));
  }

  private send(payload: object): void {
    if (this.ws?.readyState === WebSocket.OPEN) this.ws.send(JSON.stringify(payload));
  }

  private connect(): void {
    const proto = window.location.protocol === 'https:' ? 'wss' : 'ws';
    const params = new URLSearchParams({ shell: this.shell, cols: String(this.term.cols), rows: String(this.term.rows) });
    const ws = new WebSocket(`${proto}://${window.location.host}/ws/nodes/${encodeURIComponent(this.node)}/terminal?${params}`);
    ws.binaryType = 'arraybuffer';
    this.ws = ws;
    let ended = false;

    ws.onopen = () => {
      this.setStatus('open');
      this.term.focus();
    };
    ws.onmessage = (ev) => {
      if (typeof ev.data === 'string') {
        const msg = JSON.parse(ev.data) as { type: string; code?: number; message?: string };
        if (msg.type === 'exit') {
          ended = true;
          this.term.write(`\r\n\x1b[33m[session terminée, code ${msg.code ?? '?'}]\x1b[0m\r\n`);
          this.setStatus('closed');
        } else if (msg.type === 'error') {
          ended = true;
          this.term.write(`\r\n\x1b[31m${msg.message ?? 'erreur'}\x1b[0m\r\n`);
          this.setStatus('error');
        }
        return;
      }
      this.term.write(new Uint8Array(ev.data as ArrayBuffer));
    };
    ws.onclose = () => {
      if (this.disposed) return;
      if (!ended) {
        this.term.write('\r\n\x1b[33m[connexion fermée]\x1b[0m\r\n');
        this.setStatus(this.status === 'error' ? 'error' : 'closed');
      }
    };
    ws.onerror = () => this.setStatus('error');
  }
}

const sessions = new Map<string, TerminalSession>();

const keyOf = (node: string, shell: string) => `${node}\u0000${shell}`;

/** Session existante du couple nœud/shell, ou une nouvelle. */
export function getTerminalSession(node: string, shell: string): TerminalSession {
  const key = keyOf(node, shell);
  let session = sessions.get(key);
  if (!session) {
    session = new TerminalSession(node, shell);
    sessions.set(key, session);
  }
  return session;
}

/** Ferme la session du couple nœud/shell ; la suivante repart d'un terminal vierge. */
export function closeTerminalSession(node: string, shell: string): void {
  const key = keyOf(node, shell);
  sessions.get(key)?.dispose();
  sessions.delete(key);
}
