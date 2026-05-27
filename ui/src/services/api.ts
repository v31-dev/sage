import { CRUDAPI } from '@/lib/api'

const API_ROOT = '/api'

function getLoadDelay() {
  const rawDelay = import.meta.env.VITE_LOAD_DELAY
  const parsedDelay = rawDelay ? Number(rawDelay) : 300

  return Number.isFinite(parsedDelay) && parsedDelay >= 0 ? parsedDelay : 300
}

const LOAD_DELAY = getLoadDelay()

export interface AppInfo {
  version: string
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
  containers: Container[]
}

export interface Project {
  name: string
  label: string
  description: string | null
  env: string | null
  applications: Application[]
  application_count: number
  created_at: Date
  updated_at: Date
}

export interface Application {
  name: string
  label: string
  project: Project
  description: string | null
  type: 'docker' | 'git'
  repo: string | null
  path: string | null
  image: string | null
  env: string | null
  args: string | null
  domains: Domain[]
  containers: Container[]
  volumes: Volume[]
  container_count: number
  status: 'active' | 'inactive' | 'deploying' | 'stopping' | 'backup' | 'restoring' | 'error'
  domains_synced: boolean
  created_at: Date
  updated_at: Date
}

export const APPLICATION_BUSY_STATUSES: Application['status'][] = [
  'deploying',
  'stopping',
  'backup',
  'restoring',
]

export const APPLICATION_STOP_ELIGIBLE_STATUSES: Application['status'][] = ['active', 'error']

export interface Domain {
  name: string
  type: 'internal' | 'public'
  port: number
  application: Application
  created_at: Date
  updated_at: Date
}

export interface Event {
  container_task_id: string
  type: 'deploy' | 'stop' | 'delete' | 'backup' | 'restore'
  created_at: Date
}

export interface Container {
  worker: Worker
  status: 'active' | 'inactive' | 'deploying' | 'stopping' | 'backup' | 'restoring' | 'error'
  domain_tag: string | null
  events: Event[]
  created_at: Date
  updated_at: Date
  application: Application
}

export interface Volume {
  name: string
  path: string
  backup_cron: string | null
  project: Project
  application: Application
  created_at: Date
  updated_at: Date
}

export async function fetchAppInfo(): Promise<AppInfo> {
  try {
    await new Promise(resolve => setTimeout(resolve, LOAD_DELAY))
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

export const workersAPI = new CRUDAPI({
  name: 'Workers',
  path: `${API_ROOT}/workers`,
  load_delay: LOAD_DELAY,
})

export const projectAPI = new CRUDAPI({
  name: 'Project',
  path: `${API_ROOT}/projects`,
  load_delay: LOAD_DELAY,
})

export function getApplicationAPI(projectName: string) {
  return new CRUDAPI({
    name: 'Application',
    path: `${API_ROOT}/projects/{project}/applications`,
    params: { project: projectName },
    load_delay: LOAD_DELAY,
  })
}

export function getContainerAPI(projectName: string, applicationName: string) {
  return new CRUDAPI({
    name: 'Container',
    path: `${API_ROOT}/projects/{project}/applications/{application}/containers`,
    params: { project: projectName, application: applicationName },
    load_delay: LOAD_DELAY,
  })
}

export function getVolumeAPI(projectName: string, applicationName: string) {
  return new CRUDAPI({
    name: 'Volume',
    path: `${API_ROOT}/projects/{project}/applications/{application}/volumes`,
    params: { project: projectName, application: applicationName },
    load_delay: LOAD_DELAY,
  })
}

export function getDomainAPI(projectName: string, applicationName: string) {
  return new CRUDAPI({
    name: 'Domain',
    path: `${API_ROOT}/projects/{project}/applications/{application}/domains`,
    params: { project: projectName, application: applicationName },
    load_delay: LOAD_DELAY,
  })
}

export interface Setting {
  key: string
  value: Record<string, unknown> | null
  created_at: Date
  updated_at: Date
}

export const settingsAPI = new CRUDAPI({
  name: 'Settings',
  path: `${API_ROOT}/settings`,
  load_delay: LOAD_DELAY,
})

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

export interface WorkerMetricSummary {
  cpu: number | null
  mem: number | null
  memTotal: number | null
  disk: number | null
  diskTotal: number | null
  netRx: number | null
  netTx: number | null
  containers: number | null
  cores: number | null
}

export async function fetchWorkerMetrics(
  hostname: string,
  period: MetricsPeriod = '1h'
): Promise<WorkerMetricsResponse> {
  try {
    await new Promise(resolve => setTimeout(resolve, LOAD_DELAY))
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

export async function fetchLogs(
  hostname: string,
  container: string,
  search: string = '',
  from_ts: string = '',
  to_ts: string = ''
): Promise<LogEntry[]> {
  try {
    const params = new URLSearchParams()
    if (search) params.set('search', search)
    if (from_ts) params.set('from_ts', from_ts)
    if (to_ts) params.set('to_ts', to_ts)

    await new Promise(resolve => setTimeout(resolve, LOAD_DELAY))
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

export interface Notification {
  id: number
  type: 'info' | 'success' | 'warning' | 'error'
  content: string
  link: string | null
  created_at: string
  updated_at: string
}

export interface HomeSummary {
  generated_at: string
  workers_total: number
  workers_online: number
  workers_offline: number
  projects_total: number
  applications_total: number
  applications_active: number
  applications_inactive: number
  applications_error: number
  applications_deploying: number
  applications_stopping: number
  applications_backup: number
  applications_restoring: number
  deployments_last_24h: number
  containers_total: number
  containers_active: number
  containers_inactive: number
  containers_error: number
  containers_deploying: number
  containers_stopping: number
  containers_backup: number
  containers_restoring: number
  domains_total: number
  domains_active: number
  domains_inactive: number
  backups_total: number
  backups_system: number
  backups_application: number
  backups_last_24h: number
  latest_backup_at: string | null
  critical_error_count_last_24h: number
  critical_warning_count_last_24h: number
  critical_events_last_24h: Notification[]
}

export async function fetchHomeSummary(): Promise<HomeSummary> {
  try {
    await new Promise(resolve => setTimeout(resolve, LOAD_DELAY))
    const response = await fetch(`${API_ROOT}/summary`)
    if (!response.ok) {
      const data = await response.json().catch(() => ({}))
      const detail = data.detail || 'Failed to fetch home summary'
      throw new Error(detail)
    }
    return await response.json()
  } catch (error) {
    console.error('Error fetching home summary:', error)
    throw error
  }
}

export async function fetchNotifications({
  limit = null,
  from_ts = null,
  to_ts = null,
}: {
  limit?: number | null
  from_ts?: string | null
  to_ts?: string | null
} = {}): Promise<Notification[]> {
  try {
    const params = new URLSearchParams()
    if (limit) params.set('limit', limit.toString())
    if (from_ts) params.set('from_ts', from_ts)
    if (to_ts) params.set('to_ts', to_ts)

    await new Promise(resolve => setTimeout(resolve, LOAD_DELAY))
    const response = await fetch(`${API_ROOT}/notifications/?${params}`)
    if (!response.ok) {
      const data = await response.json().catch(() => ({}))
      const detail = data.detail || 'Failed to fetch notifications'
      throw new Error(detail)
    }
    return await response.json()
  } catch (error) {
    console.error('Error fetching notifications:', error)
    throw error
  }
}

export interface Backup {
  id: number
  s3_path: string
  source_volume_name: string | null
  type: string
  created_at: string
}

export const backupAPI = new CRUDAPI({
  name: 'Backup',
  path: `${API_ROOT}/backups`,
  load_delay: LOAD_DELAY,
})

export function getVolumeBackupAPI(
  projectName: string,
  applicationName: string,
  volumeName: string
) {
  return new CRUDAPI({
    name: 'Volume Backup',
    path: `${API_ROOT}/projects/{project}/applications/{application}/volumes/{volume}/backups`,
    params: { project: projectName, application: applicationName, volume: volumeName },
    load_delay: LOAD_DELAY,
  })
}
