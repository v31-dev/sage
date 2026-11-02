import { request } from '@/lib/api'
import { applicationPath } from './_common'

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
