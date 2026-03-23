import { CRUDAPI } from "@/lib/utils"


const API_ROOT = '/api'

export interface AppInfo {
  org: string
  version: number
  domain: string
  hostname: string
  ip: string
}

export interface Worker {
  hostname: string
  ip: string
  online: boolean
  created_at: Date
  updated_at: Date
}

export interface Project {
  name: string
  description: string | null
  env: string | null
  created_at: Date
  updated_at: Date
}

export interface Container {
  worker: Worker
  status: string
}

export interface Application {
  name: string
  project: string
  description: string | null
  repo: string | null
  env: string | null
  args: string | null
  containers: Container[]
  created_at: Date
  updated_at: Date
}

export async function fetchAppInfo(): Promise<AppInfo> {
  try {
    const response = await fetch(`${API_ROOT}`)
    if (!response.ok) {
      throw new Error('Failed to fetch app info')
    }
    return await response.json()
  } catch (error) {
    console.error('Error fetching app info:', error)
    throw error
  }
}

export const workersAPI = new CRUDAPI({ name: 'Workers', path: `${API_ROOT}/workers/` })
export const projectAPI = new CRUDAPI({ name: 'Project', path: `${API_ROOT}/projects/` })

export function getApplicationAPI(projectName: string) {
  return new CRUDAPI({ 
    name: 'Application', 
    path: `${API_ROOT}/projects/{project}/applications/`,
    params: { project: projectName }
  })
}

export function getContainerAPI(projectName: string, applicationName: string) {
  return new CRUDAPI({
    name: 'Container',
    path: `${API_ROOT}/projects/{project}/applications/{application}/containers/`,
    params: { project: projectName, application: applicationName }
  })
}

export type MetricsPeriod = '1m' | '1h' | '24h' | '1w'

export interface MetricsPoint {
  ts: string
  cpu_pct: number | null
  mem_used_mb: number | null
  mem_cached_mb: number | null
  disk_used_gb: number | null
  load_avg_1m: number | null
  load_avg_5m: number | null
  load_avg_15m: number | null
  net_rx_kbps: number | null
  net_tx_kbps: number | null
}

export interface ContainerMetricsPoint {
  ts: string
  name?: string
  cpu_pct: number | null
  mem_used_mb: number | null
  net_rx_kbps: number | null
  net_tx_kbps: number | null
}

export interface WorkerMeta {
  hostname: string
  cpu_cores: number
  mem_total_mb: number
  disk_total_gb: number
  ip: string
}

export interface WorkerMetricsResponse {
  host: MetricsPoint[]
  containers: Record<string, ContainerMetricsPoint[]>
  meta: WorkerMeta
}

export async function fetchWorkerMetrics(hostname: string, period: MetricsPeriod = '1h'): Promise<WorkerMetricsResponse> {
  try {
    const response = await fetch(`${API_ROOT}/workers/${hostname}/metrics?period=${period}`)
    if (!response.ok) {
      throw new Error('Failed to fetch worker metrics')
    }
    return await response.json()
  } catch (error) {
    console.error('Error fetching worker metrics:', error)
    throw error
  }
}

export interface LogEntry {
  id: number
  hostname: string
  ts: string
  stream: string
  message: string
}

export async function fetchLogs(hostname: string, container: string, search: string = '',  from_ts: string = '', to_ts: string = ''): Promise<LogEntry[]> {
  try {
    const params = new URLSearchParams()
    if (search) params.set('search', search)
    if (from_ts) params.set('from_ts', from_ts)
    if (to_ts) params.set('to_ts', to_ts)
    const response = await fetch(`${API_ROOT}/workers/${hostname}/logs/${container}?${params}`)
    if (!response.ok) {
      const data = await response.json().catch(() => ({}))
      const detail = data.detail || 'Failed to fetch logs'
      throw new Error(detail)
    }
    return await response.json()
  } catch (error) {
    console.error('Error fetching logs:', error)
    throw error
  }
}
