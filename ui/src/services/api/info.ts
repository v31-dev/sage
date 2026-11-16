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

// A manual action a migration left, in the order to take it: where to take it (a worker, Cloudflare, the store, Sage),
// the exact command when there is one, and why.
export interface MigrationAction {
  where: string
  command: string | null
  why: string
}

// One file a migration run changed or refused: from its version towards `to` (none for a file that wasn't a sound
// database), with why it was started again empty or refused (`detail`).
export interface MigrationFile {
  file: string
  from: number | null
  to: number | null
  outcome: 'migrated' | 'recreated' | 'refused'
  detail: string | null
  at: string
  actions: MigrationAction[]
}

// A migration run, at a start of Sage (`boot`) or a platform restore's install (`restore`), with the release that ran
// it; refused when any of its files was, else recreated when one was started again empty.
export interface MigrationRun {
  run: string
  trigger: 'boot' | 'restore'
  release: string
  at: string
  outcome: MigrationFile['outcome']
  files: MigrationFile[]
}

// The migration history, newest run first.
export const fetchMigrations = () => request<MigrationRun[]>('GET', '/migrations')
