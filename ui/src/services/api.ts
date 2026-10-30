// Every call to the manager's API, each a function over `request`, and the shapes it answers with.
import { request } from '@/lib/api'
import { startDownload } from '@/lib/utils'

// The fields a create or an update sends; the manager checks them.
type Fields = Record<string, unknown>

export interface AppInfo {
  version: string
  latest_version: string | null
  domain: string
  name: string
  ip: string
  start_time: string
  state: 'running' | 'paused' | 'restoring' | 'stopping'
  restoring_from: string | null
  settings: SettingsStatus | null
}

export interface Release {
  version: string
  name: string | null
  body: string | null
  html_url: string | null
  published_at: string | null
  fetched_at: string | null
}

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
  build_secrets: string | null
  command: string | null
  domains: Domain[]
  containers: Container[]
  volumes: Volume[]
  container_count: number
  status: 'active' | 'inactive' | 'deploying' | 'stopping' | 'backup' | 'restoring' | 'error'
  domains_synced: boolean
  deployed_at: Date | null
  deployed_hash: string | null
  config_dirty?: boolean
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
  type: 'internal' | 'public' | 'tcp'
  port: number
  application: Application
  created_at: Date
  updated_at: Date
}

// A container's part of a task: its lines are the task's `application_task_id` and part `container_task_id`.
export interface Event {
  application_task_id: string
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
  application: Application
  created_at: Date
  updated_at: Date
}

export const fetchAppInfo = () => request<AppInfo>('GET', '')

// Cached release detail (version + notes + fetched_at), populated by the
// scheduled/on-demand refresh job. It is a topic of its own and not part of the
// app info, since the notes body is large.
export const fetchRelease = () => request<Release>('GET', '/release')

// Fire the refresh job (re-checks GitHub and updates the cache). Returns
// immediately; the release topic pushes the result, with a new `fetched_at`.
export const refreshRelease = () => request<void>('POST', '/release/refresh')

export const fetchWorkers = () => request<WorkerListItem[]>('GET', '/workers/')

export const fetchWorker = (hostname: string) => request<Worker>('GET', `/workers/${hostname}`)

// Adds a machine of the tailnet, by hostname or Tailscale IP, as a worker. It is set up in the
// background and shows in the list once the manager has reached it.
export const addWorker = (host: string) =>
  request<{ hostname: string }>('POST', '/workers/', { body: { host } })

export const rebootWorker = (hostname: string) =>
  request<unknown>('POST', `/workers/${hostname}/reboot`)

export const deleteWorker = (hostname: string) => request<unknown>('DELETE', `/workers/${hostname}`)

export const fetchProjects = () => request<Project[]>('GET', '/projects/')

export const fetchProject = (name: string) => request<Project>('GET', `/projects/${name}`)

export const createProject = (data: Fields) =>
  request<Project>('POST', '/projects/', { body: data })

export const updateProject = (name: string, data: Fields) =>
  request<Project>('PUT', `/projects/${name}`, { body: data })

export const deleteProject = (name: string) => request<unknown>('DELETE', `/projects/${name}`)

const applicationPath = (project: string, application: string) =>
  `/projects/${project}/applications/${application}`

export const fetchApplication = (project: string, name: string) =>
  request<Application>('GET', applicationPath(project, name))

export const createApplication = (project: string, data: Fields) =>
  request<Application>('POST', `/projects/${project}/applications/`, { body: data })

export const updateApplication = (project: string, name: string, data: Fields) =>
  request<Application>('PUT', applicationPath(project, name), { body: data })

export const deleteApplication = (project: string, name: string) =>
  request<unknown>('DELETE', applicationPath(project, name))

export const deployApplication = (project: string, name: string) =>
  request<unknown>('POST', `${applicationPath(project, name)}/deploy`)

export const stopApplication = (project: string, name: string) =>
  request<unknown>('POST', `${applicationPath(project, name)}/stop`)

export const createContainer = (project: string, application: string, data: Fields) =>
  request<Container>('POST', `${applicationPath(project, application)}/containers/`, {
    body: data,
  })

export const updateContainer = (
  project: string,
  application: string,
  hostname: string,
  data: Fields
) =>
  request<Container>('PUT', `${applicationPath(project, application)}/containers/${hostname}`, {
    body: data,
  })

// `force` removes the container from Sage without cleaning up after it on an offline worker.
export const deleteContainer = (
  project: string,
  application: string,
  hostname: string,
  force = false
) =>
  request<unknown>('DELETE', `${applicationPath(project, application)}/containers/${hostname}`, {
    query: { force: force || null },
  })

export const createVolume = (project: string, application: string, data: Fields) =>
  request<Volume>('POST', `${applicationPath(project, application)}/volumes/`, { body: data })

export const updateVolume = (project: string, application: string, name: string, data: Fields) =>
  request<Volume>('PUT', `${applicationPath(project, application)}/volumes/${name}`, {
    body: data,
  })

export const deleteVolume = (project: string, application: string, name: string) =>
  request<unknown>('DELETE', `${applicationPath(project, application)}/volumes/${name}`)

// A domain is named by its type and name, `<type>:<name>`.
export const createDomain = (project: string, application: string, data: Fields) =>
  request<Domain>('POST', `${applicationPath(project, application)}/domains/`, { body: data })

export const updateDomain = (project: string, application: string, key: string, data: Fields) =>
  request<Domain>('PUT', `${applicationPath(project, application)}/domains/${key}`, {
    body: data,
  })

export const deleteDomain = (project: string, application: string, key: string) =>
  request<unknown>('DELETE', `${applicationPath(project, application)}/domains/${key}`)

export interface Setting {
  key: string
  value: Record<string, unknown> | null
  created_at: Date
  updated_at: Date
}

export type SettingSection = 'tailscale' | 'cloudflare' | 's3' | 'notifications'
export type SettingState = 'not_configured' | 'applying' | 'ok' | 'retrying' | 'error'

// What a StateIndicator can show, whatever it describes.
export type IndicatorState = 'idle' | 'busy' | 'ok' | 'warning' | 'error'
export interface Indicator {
  state: IndicatorState
  label: string
}

export interface SettingSectionStatus {
  state: SettingState
  message: string | null
  task_id: string | null
  checked_at: string | null
  // Cloudflare: account_id, tunnel_id, tunnel_status, *_certificate_expiry.
  details: Record<string, string | null> | null
  // Notifications: why each failing channel failed, by settings field.
  channels: Record<string, string> | null
}

export type SettingsStatus = Record<SettingSection, SettingSectionStatus>

export const fetchSetting = (section: SettingSection) =>
  request<Setting>('GET', `/settings/${section}`)

export const updateSetting = (section: SettingSection, value: Record<string, string | null>) =>
  request<Setting>('PUT', `/settings/${section}`, { body: { value } })

// Checks the values given, saved or not, against the provider.
export const testSetting = (section: SettingSection, value: Record<string, string | null>) =>
  request<{ message: string }>('POST', `/settings/${section}/test`, { body: { value } })

export const pauseSage = () => request<{ state: AppInfo['state'] }>('POST', '/settings/pause')

export const resumeSage = () => request<{ state: AppInfo['state'] }>('POST', '/settings/resume')

export const resyncTraefik = () => request<unknown>('POST', '/settings/resync_traefik')

export const resyncWorkers = () => request<unknown>('POST', '/settings/resync_workers')

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
  cores: number | null
}

export const METRICS_POLL_MS = 60_000

export const fetchWorkerMetrics = (hostname: string, period: MetricsPeriod = '1h') =>
  request<WorkerMetricsResponse>('GET', `/workers/${hostname}/metrics`, { query: { period } })

// The metrics of each container of an application, by worker.
export interface ApplicationContainerMetrics {
  hostname: string
  metrics: ContainerMetricsPoint[]
}

export const fetchApplicationMetrics = (
  project: string,
  application: string,
  period: MetricsPeriod
) =>
  request<ApplicationContainerMetrics[]>(
    'GET',
    `${applicationPath(project, application)}/metrics`,
    {
      query: { period },
    }
  )

// A line one of an application's containers wrote.
export interface LogEntry {
  id: number
  hostname: string
  ts: string
  stream: string
  message: string
}

// A worker (all if empty), text anywhere in a line in any case, and a time at or after `from_ts` and before `to_ts`;
// only the lines after `after_id`, if given.
export type LogQuery = {
  hostname?: string
  search?: string
  from_ts?: string
  to_ts?: string
  after_id?: number | null
}

// The newest lines that match, in the order they were stored, and whether there were more.
export const fetchApplicationLogs = (project: string, application: string, query: LogQuery = {}) =>
  request<{ lines: LogEntry[]; more: boolean }>(
    'GET',
    `/projects/${project}/applications/${application}/logs`,
    { query }
  )

// A request one of the workers' Traefik served; `duration_ms` is from when it arrived to when it was answered.
export interface ServedRequest {
  id: number
  ts: string
  worker: string
  host: string
  method: string
  path: string
  status: number
  duration_ms: number
  router: string
  service: string
  client: string
  size: number
}

export type StatusClass = '2xx' | '3xx' | '4xx' | '5xx'

// A worker and a method exactly, text anywhere in the host or the path, a status class, as slow or slower, and a start
// at or after `from_ts` and before `to_ts`; only the requests after `after_id`, if given.
export type RequestQuery = {
  worker?: string
  host?: string
  method?: string
  status?: StatusClass | ''
  path?: string
  min_duration_ms?: number | null
  from_ts?: string
  to_ts?: string
  after_id?: number | null
}

// The newest requests that match, newest first, and whether there were more.
export const fetchRequests = (query: RequestQuery = {}) =>
  request<{ requests: ServedRequest[]; more: boolean }>('GET', '/requests/', { query })

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

export const fetchHomeSummary = () => request<HomeSummary>('GET', '/summary')

export const fetchNotifications = ({
  limit = null,
  from_ts = null,
  to_ts = null,
}: {
  limit?: number | null
  from_ts?: string | null
  to_ts?: string | null
} = {}) => request<Notification[]>('GET', '/notifications/', { query: { limit, from_ts, to_ts } })

interface TaskFields {
  task_id: string
  name: string
  scopes: string[]
  params: Record<string, any>
  executor: string
  status: string
  started_at: string | null
}

// A task running or waiting.
export interface QueueTask extends TaskFields {
  queued_at: string
  quiet: boolean
}

// A task that has finished: `created_at` is when it was queued, and it has no `started_at` if it was cancelled first.
export interface FinishedTask extends TaskFields {
  created_at: string
  updated_at: string
  finished_at: string
}

export interface BackupStatus {
  platform_backup_in_progress: boolean
  discovery_in_progress: boolean
}

export interface QueueResponse {
  running: QueueTask[]
  queued: QueueTask[]
}

// The tasks that are running or waiting, held in memory by the manager.
export const fetchTasks = () => request<QueueResponse>('GET', '/tasks/')

// Which finished tasks: a name, executor and status match exactly; a scope matches itself and its children (`app`
// matches `app:demo-web`); `from_ts` and `to_ts` match a task active at any point between them.
export type TaskHistoryQuery = {
  name?: string
  scope?: string
  executor?: string
  status?: string
  from_ts?: string
  to_ts?: string
  limit?: number
  offset?: number
}

export interface TaskHistoryPage {
  tasks: FinishedTask[]
  total: number
}

// A page of the finished tasks, newest finished first, and how many match.
export const fetchTaskHistory = (query: TaskHistoryQuery = {}) =>
  request<TaskHistoryPage>('GET', '/tasks/history', { query })

// The choices of the finished tasks' filters.
export interface TaskHistoryFilters {
  names: string[]
  executors: string[]
  statuses: string[]
}

export const fetchTaskHistoryFilters = () =>
  request<TaskHistoryFilters>('GET', '/tasks/history/filters')

// A line a queued task logged; `sub_task_id` is the container's part it belongs to, if any.
export interface TaskLogLine {
  id: number
  task_id: string
  sub_task_id: string
  ts: string
  level: string
  logger: string
  message: string
}

// The newest lines, in the order they were written, and whether there were more.
export interface TaskLogs {
  lines: TaskLogLine[]
  more: boolean
}

// A task's lines, or only those of one container's part of it; after the line `afterId`, if given.
export const fetchTaskLogs = (taskId: string, subTaskId = '', afterId = 0) =>
  request<TaskLogs>('GET', `/tasks/${taskId}/logs`, {
    query: { sub_task_id: subTaskId, after_id: afterId || null },
  })

export interface Backup {
  id: number
  s3_path: string
  source_volume_name: string | null
  type: string
  created_at: string
}

// The platform backups.
export const fetchBackups = () => request<Backup[]>('GET', '/backups/')

export const fetchBackupStatus = () =>
  request<BackupStatus>('GET', '/backups/platform_backup_status')

export const createBackup = () => request<unknown>('POST', '/backups/')

// Lists the backups in S3 that are not listed yet.
export const discoverBackups = () => request<unknown>('POST', '/backups/discover')

export const deleteBackup = (id: number) => request<unknown>('DELETE', `/backups/${id}`)

export const restoreBackup = (id: number) => request<unknown>('POST', `/backups/${id}/restore`)

// A short-lived link to the file in S3, which the browser then saves.
async function download(path: string) {
  const { url } = await request<{ url: string }>('GET', path)
  startDownload(url)
}

export const downloadBackup = (id: number) => download(`/backups/${id}/download`)

// The backups of a volume of an application.
const volumeBackupsPath = (project: string, application: string, volume: string) =>
  `${applicationPath(project, application)}/volumes/${volume}/backups`

export const fetchVolumeBackups = (project: string, application: string, volume: string) =>
  request<Backup[]>('GET', `${volumeBackupsPath(project, application, volume)}/`)

export const createVolumeBackup = (project: string, application: string, volume: string) =>
  request<unknown>('POST', `${volumeBackupsPath(project, application, volume)}/`)

export const restoreVolumeBackup = (
  project: string,
  application: string,
  volume: string,
  id: number,
  targetWorker: string
) =>
  request<unknown>('POST', `${volumeBackupsPath(project, application, volume)}/${id}/restore`, {
    body: { target_worker: targetWorker },
  })

export const downloadVolumeBackup = (
  project: string,
  application: string,
  volume: string,
  id: number
) => download(`${volumeBackupsPath(project, application, volume)}/${id}/download`)
