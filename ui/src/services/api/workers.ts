import { request } from '@/lib/api'
import type { Fields } from './_common'
import type { Container } from './applications'
import type { MetricsPoint } from './metrics'
import type { TunnelStatus } from './settings'

export interface InfraContainer {
  name: string
  status: string
}

// Validity and application of one certificate on a worker.
export interface WorkerCertificate {
  covered: boolean
  synced: boolean
  expiry: string | null
  error: string | null
}

export type WorkerStatus =
  | 'online'
  | 'not ready'
  | 'offline'
  | 'checking'
  | 'adding'
  | 'rebooting'
  | 'removing'

// A worker's state as the manager decides it, with why it is not online; `ready` while its containers can be reached.
export interface WorkerView {
  status: WorkerStatus
  reason: string | null
  detail: string | null
  ready: boolean | null
}

// What the worker's reports say: how current the newest is, whether Traefik answered on it, each service of its stack
// with its status (`missing` when it is not listed), those that do not run, and how many containers it lists.
export interface WorkerReport {
  knowledge: 'unobserved' | 'known' | 'stale'
  traefik_reachable: boolean | null
  stack_down: string[]
  stack: [string, string][]
  containers: number | null
}

export interface WorkerListItem {
  hostname: string
  // Its internal names are under `<subdomain>.int.<domain>`.
  subdomain: string | null
  view: WorkerView
  // What the manager last found of its SSH server.
  ssh: 'unobserved' | 'reachable' | 'unreachable' | 'unknown'
  // How its last setup since the manager started went; none before one ran.
  setup: string | null
  report: WorkerReport
  // The worker tasks queued or running on it (adding, removing, rebooting).
  tasks: string[]
  // Its address in the tailnet, and how the manager's last ping reached it: "direct", or "relay" with its region.
  address: string | null
  path: string | null
  certificates: { worker: WorkerCertificate; discovery: WorkerCertificate }
  // The health of its own tunnel at the last Cloudflare check, and that tunnel's name.
  tunnel: TunnelStatus | null
  tunnel_name: string
  metrics: WorkerMinute | null
  created_at: string
  updated_at: string
}

export interface Worker extends WorkerListItem {
  containers: Container[]
  // Why the manager would refuse its removal now (containers or internal domains on it); null when it would not.
  removal_refusal: string | null
}

// A worker's newest minute of metrics, as the worker list holds it while it is recent: the minute's peaks and the
// machine's size.
export interface WorkerMinute extends MetricsPoint {
  cpu_cores: number | null
  mem_total_mb: number | null
  disk_total_gb: number | null
}

export const fetchWorkers = () => request<WorkerListItem[]>('GET', '/workers/')

export const fetchWorker = (hostname: string) => request<Worker>('GET', `/workers/${hostname}`)

// Adds a machine of the tailnet, by `host` (its hostname or Tailscale IP), as a worker, with its `subdomain`. It is set
// up in the background; the answer names the worker under its hostname, in lowercase.
export const addWorker = (data: Fields) =>
  request<{ task_id: string; hostname: string }>('POST', '/workers/', { body: data })

export const updateWorker = (hostname: string, data: Fields) =>
  request<Worker>('PUT', `/workers/${hostname}`, { body: data })

export const rebootWorker = (hostname: string) =>
  request<{ task_id: string }>('POST', `/workers/${hostname}/reboot`)

// The worker's forced setup, which writes again everything Sage keeps on it.
export const resyncWorker = (hostname: string) =>
  request<{ task_id: string }>('POST', `/workers/${hostname}/resync`)

export const deleteWorker = (hostname: string) =>
  request<{ task_id: string }>('DELETE', `/workers/${hostname}`)
