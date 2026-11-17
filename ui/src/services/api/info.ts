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
  // The latest migration run of this start of the manager, a boot's or a platform restore's, or none.
  migration: MigrationRunSummary | null
  settings: SettingsStatus | null
}

// A migration run as the app info carries it: what it was, its overall outcome and how many manual actions it left.
export interface MigrationRunSummary {
  run: string
  trigger: 'boot' | 'restore'
  release: string
  outcome: 'migrated' | 'recreated' | 'refused'
  actions: number
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

// A line of a migration run's log: what it logged, or a manual action it left (CRITICAL, its command below it).
export interface MigrationLogLine {
  ts: string
  level: string
  message: string
}

// A migration run, at a start of Sage (`boot`) or a platform restore's install (`restore`), with the release that ran
// it, whether a file was refused, how many manual actions it left and its log, the actions last.
export interface MigrationRun {
  run: string
  trigger: 'boot' | 'restore'
  release: string
  at: string
  refused: boolean
  actions: number
  lines: MigrationLogLine[]
}

// The migration history, newest run first.
export const fetchMigrations = () => request<MigrationRun[]>('GET', '/migrations')
