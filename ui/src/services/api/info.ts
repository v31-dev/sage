import { request } from '@/lib/api'
import type { Notification } from './notifications'
import type { SettingsStatus } from './settings'

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

export const fetchAppInfo = () => request<AppInfo>('GET', '')

// Cached release detail (version + notes + fetched_at), populated by the
// scheduled/on-demand refresh job. It is a topic of its own and not part of the
// app info, since the notes body is large.
export const fetchRelease = () => request<Release>('GET', '/release')

// Fire the refresh job (re-checks GitHub and updates the cache). Returns
// immediately; the release topic pushes the result, with a new `fetched_at`.
export const refreshRelease = () => request<void>('POST', '/release/refresh')

export const fetchHomeSummary = () => request<HomeSummary>('GET', '/summary')
