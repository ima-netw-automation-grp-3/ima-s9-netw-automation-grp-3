// Types des réponses de l'API (miroir du backend FastAPI).

export type Phase = 'ready' | 'booting' | 'bootstrap' | 'warning' | 'error' | 'down';
export type Role = 'router' | 'client';
export type Domain = 'configdb' | 'frr';
export type PushMode = 'merge' | 'replace';

/** Action sur le service clab lancée depuis l'interface (le démarrage est immédiat, jamais en cours). */
export type RunnerAction = 'start' | 'stop' | 'redeploy';

export interface RunnerState {
  /** loading-image | starting | deploying | deployed | failed | destroying | stopped | absent | unknown */
  state: string;
  container: string | null;
  container_state: string | null;
  container_status: string | null;
  /** action en cours (arrêt, redéploiement) */
  action: RunnerAction | null;
  /** échec de la dernière action */
  error: string | null;
}

export interface PortInfo {
  name: string;
  alias?: string | null;
  lanes?: string;
  speed?: string | null;
  mtu?: string | null;
  description?: string;
  admin_status: string;
  oper_status: string;
  wire?: string | null;
}

export interface IfaceStats {
  rx_bytes?: number;
  rx_packets?: number;
  rx_errors?: number;
  rx_dropped?: number;
  tx_bytes?: number;
  tx_packets?: number;
  tx_errors?: number;
  tx_dropped?: number;
}

export interface IfaceInfo {
  operstate?: string | null;
  up?: boolean;
  mtu?: number | null;
  mac?: string | null;
  addresses: string[];
  stats?: IfaceStats;
}

export interface BgpPeer {
  afi: string;
  peer: string;
  hostname?: string | null;
  remote_as?: number;
  state?: string;
  uptime?: string;
  pfx_rcd?: number;
  pfx_snt?: number;
  msg_rcvd?: number;
  msg_sent?: number;
  desc?: string | null;
  router?: string;
}

export interface BgpSummary {
  as: number | null;
  router_id: string | null;
  peers: BgpPeer[];
}

export interface NodeCard {
  name: string;
  kind: string;
  role: Role;
  image: string | null;
  container: string;
  shells: string[];
  container_state: string;
  container_status: string | null;
  mgmt_ipv4: string | null;
  phase: Phase;
  detail: string;
  progress: number | null;
  started_at: number | null;
  addresses: Record<string, string[]>;
  bgp?: BgpSummary;
  ports?: PortInfo[];
}

export interface LinkEnd {
  node: string;
  interface: string;
  port: string | null;
  up: boolean | null;
  admin?: string;
  /** Interface ethN administrativement active (false : câble coupé depuis l'interface web). */
  wire_up: boolean | null;
  addresses: string[];
}

export interface BgpSession {
  a: string;
  z: string;
  state: string;
  peers: BgpPeer[];
}

export interface LinkInfo {
  id: string;
  a: LinkEnd;
  z: LinkEnd;
  state: 'up' | 'down' | 'unknown';
  bgp: BgpSession | null;
}

/** Infra en GitOps : état publié par l'agent du lab (lab/.gitops/state.json). */
export interface GitOpsState {
  enabled: boolean;
  project?: string | null;
  project_url?: string | null;
  default_branch?: string | null;
  applied_sha?: string | null;
  status?: string | null; // deployed | deploying | failed | interrupted | inactive
  message?: string | null;
  updated_at?: string | null;
}

/** Résultat de « Sauvegarder running → startup » : fichiers locaux, ou merge request en GitOps. */
export interface SaveResult {
  ok: boolean;
  gitops?: boolean;
  changed?: boolean;
  message?: string;
  paths?: string[];
  files?: string[];
  merge_request?: { iid: number; web_url: string };
}

export interface LabState {
  name: string;
  runner: RunnerState;
  mgmt: { network: string | null; subnet: string | null };
  /** fichier de topologie, depuis la racine du projet (lab/sonic-lab.clab.yml) */
  topology_file: string;
  /** fichier illisible (nœuds et liens vides), ou nom de lab que le runner refusera */
  topology_error: string | null;
  nodes: NodeCard[];
  links: LinkInfo[];
  bgp_sessions: BgpSession[];
  /** conteneurs du lab dont le nœud n'est plus dans le fichier (topologie changée lab démarré) */
  stale_nodes: string[];
  gitops?: GitOpsState;
}

export interface Service {
  name: string;
  state: string;
  detail: string;
}

export interface RouterRoute {
  prefix: string;
  protocol: string;
  selected: boolean;
  installed: boolean;
  distance?: number;
  metric?: number;
  uptime?: string;
  nexthops: { ip?: string | null; interface?: string | null; active: boolean; connected: boolean }[];
}

export interface ClientRoute {
  prefix: string;
  via: string | null;
  dev: string | null;
  src: string | null;
  proto: string | null;
}

export interface Neighbor {
  ip: string;
  dev: string;
  mac: string | null;
  state: string;
}

export interface LldpNeighbor {
  local_port: string;
  system_name?: string | null;
  port_id?: string | null;
  port_desc?: string | null;
  mgmt_ip?: string | null;
}

interface NodeStatusBase {
  name: string;
  kind: string;
  image: string | null;
  container: string;
  shells: string[];
  container_state: string;
  container_status: string | null;
  mgmt_ipv4: string | null;
  started_at?: number | null;
  phase: Phase;
  detail: string;
  progress?: number;
  errors?: Record<string, string>;
  interfaces?: Record<string, IfaceInfo>;
  ts?: number;
}

export interface RouterStatus extends NodeStatusBase {
  role: 'router';
  ready?: boolean;
  services?: Service[];
  key_services?: string[];
  ports?: PortInfo[];
  bgp?: BgpSummary;
  routes?: RouterRoute[];
  neighbors?: Neighbor[];
  lldp?: LldpNeighbor[];
  version?: Record<string, string>;
}

export interface ClientStatus extends NodeStatusBase {
  role: 'client';
  routes?: ClientRoute[];
}

export type NodeStatus = RouterStatus | ClientStatus;

export interface ConfigDoc {
  node: string;
  domain: Domain;
  source: string;
  content: string;
  raw?: string;
}

export interface FieldChange {
  name: string;
  before: unknown;
  after: unknown;
}

export interface Change {
  action: 'add' | 'modify' | 'delete';
  table: string;
  key: string;
  before: Record<string, unknown> | null;
  after: Record<string, unknown> | null;
  fields: FieldChange[];
}

export interface ChangeSummary {
  add: number;
  modify: number;
  delete: number;
}

export interface ReplacePlan {
  commands: string[];
  removed: string[];
  resets: string[];
}

export interface Preview {
  domain: Domain;
  mode: PushMode;
  valid: boolean;
  errors?: string;
  summary?: ChangeSummary;
  changes?: Change[];
  before?: string;
  after?: string | null;
  diff?: string;
  replace?: ReplacePlan;
  additions?: string;
}

export interface PushStep {
  step: string;
  exit_code: number;
  output: string;
  commands: string;
}

export interface PushResult {
  domain: Domain;
  mode: PushMode;
  ok: boolean;
  message?: string;
  summary?: ChangeSummary;
  changes?: Change[];
  applied?: number;
  failed?: { op: string; table: string; key: string; error?: string }[];
  steps?: PushStep[];
  resets?: string[];
  diff?: string;
}

export interface RestoreResult {
  ok: boolean;
  source: string;
  results: Partial<Record<Domain, PushResult>>;
}

export interface DiffResult {
  against: string;
  configdb: { changes: Change[]; count: number; diff: string };
  frr: { diff: string };
  dirty: boolean;
}

export interface Snapshot {
  id: string;
  node: string;
  label?: string;
  created?: string;
  source?: string;
  domains: Domain[];
  contents?: Partial<Record<Domain, string>>;
}

export interface PingSummary {
  transmitted: number;
  received: number;
  loss: number | null;
  rtt_min: number | null;
  rtt_avg: number | null;
  rtt_max: number | null;
  ok: boolean;
}

export interface ExecResult {
  command?: string;
  exit_code: number;
  stdout: string;
  stderr: string;
  timed_out: boolean;
  duration: number;
  output?: string;
  summary?: PingSummary;
}

/** ok : une adresse a répondu ; fail : aucune réponse ; na : aucune adresse de la cible n'est routée
 *  par le lab depuis la source (VRF ou plateforme différente), rien n'a été pingué. */
export type ConnectivityStatus = 'ok' | 'fail' | 'na';

export interface ConnectivityCell {
  source: string;
  node: string;
  status: ConnectivityStatus;
  ok: boolean;
  /** adresse retenue (la dernière essayée), null si non routée */
  interface: string | null;
  ip: string | null;
  /** interface de sortie de la source */
  via: string | null;
  loss: number | null;
  rtt_avg: number | null;
  output: string;
}

export interface ConnectivityTarget {
  node: string;
  role: Role;
  addresses: { interface: string; ip: string }[];
}

export interface ConnectivityResult {
  sources: string[];
  targets: ConnectivityTarget[];
  results: ConnectivityCell[];
  totals: Record<ConnectivityStatus, number>;
}

export type DashboardKey = 'overview' | 'router' | 'client' | 'interfaces' | 'probes' | 'infra' | 'flows';

export interface MonitoringInfo {
  grafana: { url: string | null; port: number; available: boolean };
  dashboards: Record<DashboardKey, string>;
}

export interface Capture {
  id: string;
  node: string;
  interface: string;
  filter: string;
  filename: string;
  created: number;
  state: 'running' | 'stopped';
  /** fin de la sonde (epoch), si arrêtée */
  ended: number | null;
  viewer: 'starting' | 'ready' | 'stopped';
  viewer_ready: boolean;
  size: number;
  url: string;
  download: string;
}

export interface CaptureList {
  captures: Capture[];
  max_sessions: number;
  max_mb: number;
}

export interface WireResult {
  ok: boolean;
  node: string;
  interface: string;
  up: boolean;
  peer: { node: string; interface: string };
  output: string;
}
