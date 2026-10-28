import { computed, ref } from 'vue'
import { defineStore } from 'pinia'

import {
  APPLICATION_BUSY_STATUSES,
  fetchAppInfo,
  type AppInfo,
  type Application,
  type Container,
  type Indicator,
  type IndicatorState,
  type InfraContainer,
  type SettingSection,
  type SettingState,
  type WorkerStatus,
} from '@/services/api'
import { watchState } from '@/lib/updates'

const SETTING_INDICATORS: Record<SettingState, Indicator> = {
  not_configured: { state: 'idle', label: 'Not configured' },
  applying: { state: 'busy', label: 'Applying' },
  ok: { state: 'ok', label: 'OK' },
  retrying: { state: 'warning', label: 'Retrying' },
  error: { state: 'error', label: 'Error' },
}

const WORKER_INDICATORS: Record<WorkerStatus, Indicator> = {
  online: { state: 'ok', label: 'online' },
  'not ready': { state: 'warning', label: 'not ready' },
  offline: { state: 'error', label: 'offline' },
  adding: { state: 'busy', label: 'adding' },
  rebooting: { state: 'busy', label: 'rebooting' },
  removing: { state: 'busy', label: 'removing' },
}

// The manager's status, which the sidebar, the banner, the restore overlay and the
// settings pages read. The store follows the manager's `info` topic and owns the map from
// each status to the indicator that shows it.
export const useAppStore = defineStore('app', () => {
  const info = ref<AppInfo | null>(null)

  // Immediate updates to block UI button till the next push gives the real update
  const applicationDeployStatus = ref('inactive' as Application['status'])
  // The open application's containers by worker hostname, as the last action set them.
  const containerStatuses = ref<Record<string, Application['status']>>({})

  const workersIndicator = computed<Indicator | undefined>(() => {
    const workers = info.value?.workers
    if (!workers) return undefined
    const { total, online } = workers
    if (total === 0) return { state: 'idle', label: 'No workers' }
    return { state: online === total ? 'ok' : 'error', label: `${online} of ${total} online` }
  })

  function sectionIndicator(section: SettingSection): Indicator | undefined {
    const state = info.value?.settings?.[section]?.state
    return state ? SETTING_INDICATORS[state] : undefined
  }

  // Applications and containers share one set of statuses.
  function applicationIndicator(status: Application['status']): Indicator {
    const state: IndicatorState =
      status === 'active'
        ? 'ok'
        : status === 'error'
          ? 'error'
          : APPLICATION_BUSY_STATUSES.includes(status)
            ? 'busy'
            : 'idle'
    return { state, label: status }
  }

  function workerIndicator(status: WorkerStatus): Indicator {
    return WORKER_INDICATORS[status]
  }

  function pathIndicator(path: string): Indicator {
    return { state: path === 'direct' ? 'ok' : 'warning', label: path }
  }

  function infraIndicator(container: InfraContainer): Indicator {
    const { name, status } = container
    const state: IndicatorState =
      status === 'running'
        ? 'ok'
        : status === 'restarting' || status === 'paused'
          ? 'warning'
          : 'error'
    return { state, label: `${name}: ${status}` }
  }

  function domainIndicator(synced: boolean): Indicator {
    return synced ? { state: 'ok', label: 'synced' } : { state: 'busy', label: 'syncing' }
  }

  function taskIndicator(status: string): Indicator {
    const state: IndicatorState =
      status === 'completed'
        ? 'ok'
        : status === 'failed'
          ? 'error'
          : status === 'cancelled'
            ? 'warning'
            : status === 'running'
              ? 'busy'
              : 'idle'
    return { state, label: status }
  }

  let started: Promise<void> | null = null

  // A part the manager couldn't build comes as null; the last value stays.
  function apply(next: AppInfo) {
    info.value = {
      ...next,
      workers: next.workers ?? info.value?.workers ?? null,
      settings: next.settings ?? info.value?.settings ?? null,
    }
  }

  async function refresh() {
    try {
      apply(await fetchAppInfo())
    } catch (err) {
      console.error('Failed to refresh the app info:', err)
    }
  }

  // The first info, which the app waits for; then the manager pushes each change.
  function init() {
    started ??= watchState('info', fetchAppInfo, apply).first
    return started
  }

  // Immediate updates after an action; the next push confirms them.
  function setState(state: AppInfo['state']) {
    if (info.value) info.value.state = state
  }

  function setSettingApplying(section: SettingSection) {
    const entry = info.value?.settings?.[section]
    if (entry) entry.state = 'applying'
  }

  // The containers named are shown with the status too; the others keep the pushed one.
  function updateApplicationDeployStatus(status: Application['status'], hostnames: string[] = []) {
    applicationDeployStatus.value = status
    containerStatuses.value = Object.fromEntries(hostnames.map(hostname => [hostname, status]))
  }

  function updateContainerStatus(hostname: string, status: Application['status']) {
    containerStatuses.value = { ...containerStatuses.value, [hostname]: status }
  }

  function containerStatus(container: Container): Application['status'] {
    return containerStatuses.value[container.worker.hostname] ?? container.status
  }

  return {
    // State
    info,
    applicationDeployStatus,
    // Getters
    workersIndicator,
    sectionIndicator,
    applicationIndicator,
    workerIndicator,
    pathIndicator,
    infraIndicator,
    domainIndicator,
    taskIndicator,
    containerStatus,
    // Actions
    init,
    refresh,
    setState,
    setSettingApplying,
    updateApplicationDeployStatus,
    updateContainerStatus,
  }
})
