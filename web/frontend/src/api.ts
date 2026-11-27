import type {
  CaptureList,
  Capture,
  ConfigDoc,
  ConnectivityResult,
  DiffResult,
  Domain,
  ExecResult,
  LabState,
  MonitoringInfo,
  NodeStatus,
  Preview,
  PushMode,
  PushResult,
  RestoreResult,
  SaveResult,
  Snapshot,
  WireResult,
} from './types';

export class ApiError extends Error {
  status: number;
  detail?: unknown;

  constructor(message: string, status: number, detail?: unknown) {
    super(message);
    this.status = status;
    this.detail = detail;
  }
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const res = await fetch(path, {
    method,
    headers: body !== undefined ? { 'Content-Type': 'application/json' } : undefined,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  const text = await res.text();
  let data: unknown = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    data = text;
  }
  if (!res.ok) {
    const obj = (data ?? {}) as { error?: string; detail?: unknown };
    let message = obj.error ?? `HTTP ${res.status}`;
    if (!obj.error && Array.isArray(obj.detail)) {
      // erreurs de validation FastAPI (422)
      message = obj.detail.map((d: { msg?: string }) => d.msg ?? '').join(' ; ');
    }
    throw new ApiError(message, res.status, obj.error ? obj.detail : undefined);
  }
  return data as T;
}

const enc = encodeURIComponent;
const node = (name: string) => `/api/nodes/${enc(name)}`;

export const api = {
  lab: () => request<LabState>('GET', '/api/lab'),
  runnerLogs: (tail = 400) => request<{ logs: string }>('GET', `/api/lab/runner/logs?tail=${tail}`),
  redeploy: () => request<{ ok: boolean; message: string }>('POST', '/api/lab/redeploy'),
  startLab: () => request<{ ok: boolean; message: string }>('POST', '/api/lab/start'),
  stopLab: () => request<{ ok: boolean; message: string }>('POST', '/api/lab/stop'),
  connectivity: () => request<ConnectivityResult>('POST', '/api/lab/connectivity', { count: 2 }),
  saveAll: () => request<SaveResult>('POST', '/api/lab/save-all'),
  monitoring: () => request<MonitoringInfo>('GET', '/api/monitoring'),
  resetAll: () => request<{ ok: boolean }>('POST', '/api/lab/reset-all'),

  node: (name: string) => request<NodeStatus>('GET', node(name)),
  exec: (name: string, command: string, timeout = 60) =>
    request<ExecResult>('POST', `${node(name)}/exec`, { command, timeout }),
  ping: (name: string, target: string, count = 3) =>
    request<ExecResult>('POST', `${node(name)}/ping`, { target, count }),
  traceroute: (name: string, target: string) =>
    request<ExecResult>('POST', `${node(name)}/traceroute`, { target }),
  logs: (name: string, lines = 300) => request<{ logs: string }>('GET', `${node(name)}/logs?lines=${lines}`),
  bootstrap: (name: string) => request<ExecResult>('POST', `${node(name)}/bootstrap`),

  getConfig: (name: string, domain: Domain, source: string) =>
    request<ConfigDoc>('GET', `${node(name)}/config?domain=${domain}&source=${enc(source)}`),
  preview: (name: string, domain: Domain, mode: PushMode, content: string) =>
    request<Preview>('POST', `${node(name)}/config/preview`, { domain, mode, content }),
  push: (name: string, domain: Domain, mode: PushMode, content: string) =>
    request<PushResult>('POST', `${node(name)}/config/push`, { domain, mode, content }),
  save: (name: string) => request<SaveResult>('POST', `${node(name)}/config/save`),
  restore: (name: string, source: string, domains?: Domain[]) =>
    request<RestoreResult>('POST', `${node(name)}/config/restore`, { source, domains }),
  diff: (name: string, against = 'startup') =>
    request<DiffResult>('GET', `${node(name)}/config/diff?against=${enc(against)}`),

  snapshots: (name: string) => request<{ snapshots: Snapshot[] }>('GET', `${node(name)}/snapshots`),
  createSnapshot: (name: string, label: string) => request<Snapshot>('POST', `${node(name)}/snapshots`, { label }),
  getSnapshot: (name: string, id: string) => request<Snapshot>('GET', `${node(name)}/snapshots/${enc(id)}`),
  deleteSnapshot: (name: string, id: string) => request<{ ok: boolean }>('DELETE', `${node(name)}/snapshots/${enc(id)}`),

  setWire: (name: string, iface: string, up: boolean) =>
    request<WireResult>('POST', `/api/nodes/${enc(name)}/wires/${enc(iface)}`, { up }),
  captures: () => request<CaptureList>('GET', '/api/captures'),
  captureInterfaces: (name: string) =>
    request<{ node: string; interfaces: string[] }>('GET', `/api/captures/interfaces?node=${enc(name)}`),
  startCapture: (name: string, iface: string, filter: string) =>
    request<Capture>('POST', '/api/captures', { node: name, interface: iface, filter }),
  stopCapture: (id: string) => request<Capture>('POST', `/api/captures/${enc(id)}/stop`),
  deleteCapture: (id: string) => request<{ status: string }>('DELETE', `/api/captures/${enc(id)}`),
};
