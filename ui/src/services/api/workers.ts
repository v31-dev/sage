import { request } from '@/lib/api'
import type { Fields } from './_common'
import type { Container } from './applications'
import type { MetricsPoint } from './metrics'

export interface InfraContainer {
  name: string
  status: string
}

export type WorkerStatus =
  | 'online'
  | 'not ready'
  | 'offline'
  | 'checking'
  | 'adding'
  | 'rebooting'
  | 'removing'

export interface Worker {
  hostname: string
  ip: string
  tag: string | null
  pool: string | null
  online: boolean | null
  status: WorkerStatus
  // How the manager's last ping reached the worker: "direct", or "relay" with its region.
  path: string | null
  infra?: InfraContainer[] | null
  metrics: WorkerMinute | null
  created_at: Date
  updated_at: Date
  containers: Container[]
}

// What the Workers page shows of a worker; `container_count` is the containers in its current report.
export interface WorkerListItem {
  hostname: string
  ip: string
  tag: string | null
  pool: string | null
  online: boolean | null
  status: WorkerStatus
  path: string | null
  container_count: number | null
  // The state of the containers of its stack in its current report.
  infra: InfraContainer[] | null
  metrics: WorkerMinute | null
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

// Adds a machine of the tailnet, by `host` (its hostname or Tailscale IP), as a worker, with its `tag` (random when
// none is given) and `pool`. It is set up in the background and shows in the list once the manager has reached it.
export const addWorker = (data: Fields) =>
  request<{ hostname: string }>('POST', '/workers/', { body: data })

export const updateWorker = (hostname: string, data: Fields) =>
  request<Worker>('PUT', `/workers/${hostname}`, { body: data })

export const rebootWorker = (hostname: string) =>
  request<unknown>('POST', `/workers/${hostname}/reboot`)

export const deleteWorker = (hostname: string) => request<unknown>('DELETE', `/workers/${hostname}`)
