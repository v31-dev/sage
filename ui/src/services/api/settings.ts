import { request } from '@/lib/api'
import type { AppInfo } from './info'

export interface Setting {
  key: string
  value: Record<string, unknown> | null
  created_at: string
  updated_at: string
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
  details: CloudflareDetails | null
  // Notifications: why each failing channel failed, by settings field.
  channels: Record<string, string> | null
  // Whether its Test (`test_<section>`) is queued or running, apart from its state.
  testing: boolean
}

export type TunnelStatus = 'healthy' | 'degraded' | 'down' | 'inactive'

export interface CertificateInfo {
  id: string
  names: string[]
  valid: boolean
  expiry: string | null
  error: string | null
}

export interface CloudflareDetails {
  account_id: string | null
  tunnel_id: string | null
  tunnel_name: string
  tunnel_status: TunnelStatus | null
  certificates: CertificateInfo[]
}

export type SettingsStatus = Record<SettingSection, SettingSectionStatus>

export const fetchSetting = (section: SettingSection) =>
  request<Setting>('GET', `/settings/${section}`)

export const updateSetting = (section: SettingSection, value: Record<string, string | null>) =>
  request<Setting>('PUT', `/settings/${section}`, { body: { value } })

// Queues a check of the values given, saved or not, against the provider: the task `test_<section>`, whose outcome
// the section's `setting-tests` feed pushes.
export const testSetting = (section: SettingSection, value: Record<string, string | null>) =>
  request<{ task_id: string }>('POST', `/settings/${section}/test`, { body: { value } })

// A Test's outcome, as `setting-tests:<section>` pushes it when the Test ends; the manager keeps none.
export interface SettingTestOutcome {
  ok: boolean
  message: string
  at: string
}

export const pauseSage = () => request<{ state: AppInfo['state'] }>('POST', '/settings/pause')

export const resumeSage = () => request<{ state: AppInfo['state'] }>('POST', '/settings/resume')

export const resyncWorkers = () =>
  request<{ task_ids: string[] }>('POST', '/settings/resync_workers')
