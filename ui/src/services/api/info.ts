import { request } from '@/lib/api'
import type { Notification } from './notifications'
import type { SettingsStatus } from './settings'
import type { WorkerStatus } from './workers'
import type { ContainerHeadline } from './applications'
import type { DomainStatus } from './domains'

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
  projects: number
  applications: number
  workers: Record<WorkerStatus | 'total', number>
  containers: Record<ContainerHeadline | 'total', number>
  domains: Record<DomainStatus | 'total', number>
  deploys: { succeeded: number; failed: number }
  backups: { platform: number; application: number; since: number; latest_at: string | null }
  notices: { error: number; warning: number; events: Notification[] }
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
