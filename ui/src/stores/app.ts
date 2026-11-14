import { computed, ref } from 'vue'
import { defineStore } from 'pinia'

import {
  fetchAppInfo,
  fetchWorkers,
  type AppInfo,
  type ApplicationSummary,
  type ContainerHeadline,
  type DomainStatus,
  type DomainView,
  type Indicator,
  type IndicatorState,
  type InfraContainer,
  type SettingSection,
  type SettingState,
  type TunnelStatus,
  type WorkerCertificate,
  type WorkerListItem,
  type WorkerStatus,
  type WorkerView,
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
  checking: { state: 'busy', label: 'checking' },
  adding: { state: 'busy', label: 'adding' },
  rebooting: { state: 'busy', label: 'rebooting' },
  removing: { state: 'busy', label: 'removing' },
}

const CONTAINER_INDICATORS: Record<ContainerHeadline, Indicator> = {
  active: { state: 'ok', label: 'active' },
  inactive: { state: 'idle', label: 'inactive' },
  deploying: { state: 'busy', label: 'deploying' },
  stopping: { state: 'busy', label: 'stopping' },
  backup: { state: 'busy', label: 'backup' },
  restoring: { state: 'busy', label: 'restoring' },
  error: { state: 'error', label: 'error' },
  checking: { state: 'busy', label: 'checking' },
}

// What each reason of a worker's state means; `detail` when the manager's detail reads as part of it.
const WORKER_REASONS: Record<string, { text: string; detail?: boolean }> = {
  unreachable: { text: 'It answers neither over the tailnet nor through its tunnel' },
  ssh_unreachable: { text: 'Its SSH server does not answer' },
  tailnet_unreachable: { text: 'It answers through its tunnel but not over the tailnet' },
  manager_offline: { text: 'The manager is not connected to the tailnet' },
  unobserved: { text: 'It has not been looked at yet' },
  owned_elsewhere: { text: 'Another Sage manages it' },
  setup_failed: { text: 'Its setup failed' },
  not_reporting: { text: 'It sends no reports' },
  stack_down: { text: 'Part of its stack is down', detail: true },
  traefik_down: { text: 'Traefik does not answer on it' },
  traefik_unchecked: { text: 'Traefik has not been checked yet' },
}

// What each reason of a Domain's state means; `detail` when its error reads as part of it.
const DOMAIN_REASONS: Record<string, { text: string; detail?: boolean }> = {
  route: { text: 'Its route could not be applied', detail: true },
  dns: { text: 'Its DNS records could not be applied', detail: true },
  drifted: { text: 'Its route or DNS records differ from its configuration' },
  worker_not_ready: { text: 'Its worker is not ready' },
  eligibility_unknown: { text: "Its container's state is not known yet" },
  not_applied: { text: 'It has not been applied yet' },
}

const DOMAIN_INDICATORS: Record<DomainStatus, Indicator> = {
  pending: { state: 'idle', label: 'pending' },
  syncing: { state: 'busy', label: 'syncing' },
  synced: { state: 'ok', label: 'synced' },
  error: { state: 'error', label: 'sync failed' },
}

// The manager's status, which the sidebar, the banner, the restore overlay and the
// settings pages read, and the workers, which the sidebar and the pages of workers and
// applications read. The store follows the manager's `info` and `workers` topics and owns
// the map from each status to the indicator that shows it.
export const useAppStore = defineStore('app', () => {
  const info = ref<AppInfo | null>(null)
  // Null until the first load; the error is the first load failing, until a state arrives.
  const workers = ref<WorkerListItem[] | null>(null)
  const workersError = ref('')

  const workersIndicator = computed<Indicator | undefined>(() => {
    if (!workers.value) return undefined
    const total = workers.value.length
    const online = workers.value.filter(worker => worker.view.status === 'online').length
    if (total === 0) return { state: 'idle', label: 'No workers' }
    return { state: online === total ? 'ok' : 'error', label: `${online} of ${total} online` }
  })

  // The status of the worker in the list, undefined before the list has loaded or for a name that is not in it.
  function workerStatus(hostname: string): WorkerStatus | undefined {
    return workers.value?.find(worker => worker.hostname === hostname)?.view.status
  }

  // The worker's own certificate in the list, as `workerStatus`.
  function workerCertificate(hostname: string): WorkerCertificate | undefined {
    return workers.value?.find(worker => worker.hostname === hostname)?.certificates.worker
  }

  // Whether the worker's internal names have their certificate: issued for them, then synced to the worker.
  function certificateIndicator(certificate: WorkerCertificate): Indicator {
    if (certificate.error && (!certificate.covered || !certificate.synced)) {
      return { state: 'error', label: 'certificate update failed' }
    }
    if (!certificate.covered) return { state: 'busy', label: 'waiting for the certificate' }
    if (!certificate.synced) return { state: 'busy', label: 'syncing the certificate' }
    return { state: certificate.error ? 'warning' : 'ok', label: 'certificate ready' }
  }

  function tunnelIndicator(status: TunnelStatus): Indicator {
    const state: IndicatorState =
      status === 'healthy'
        ? 'ok'
        : status === 'degraded'
          ? 'warning'
          : status === 'down'
            ? 'error'
            : 'idle'
    return { state, label: `tunnel ${status}` }
  }

  function sectionIndicator(section: SettingSection): Indicator | undefined {
    const state = info.value?.settings?.[section]?.state
    return state ? SETTING_INDICATORS[state] : undefined
  }

  // How many of its containers are active: an application has no state of its own.
  function applicationIndicator({ active_count, container_count }: ApplicationSummary): Indicator {
    if (container_count === 0) return { state: 'idle', label: 'No containers' }
    return {
      state: active_count === container_count ? 'ok' : active_count === 0 ? 'idle' : 'warning',
      label: `${active_count} of ${container_count} active`,
    }
  }

  function containerIndicator(headline: ContainerHeadline): Indicator {
    return CONTAINER_INDICATORS[headline]
  }

  function workerIndicator(status: WorkerStatus): Indicator {
    return WORKER_INDICATORS[status]
  }

  // Why the worker is in its state, for people; empty when it is online or busy with a task of its own.
  function workerReason({ reason, detail }: WorkerView): string {
    if (!reason) return ''
    const meaning = WORKER_REASONS[reason]
    if (!meaning) return reason
    return meaning.detail && detail ? `${meaning.text}: ${detail}` : `${meaning.text}.`
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

  function domainIndicator(status: DomainStatus): Indicator {
    return DOMAIN_INDICATORS[status]
  }

  // Why the Domain is in its state, for people; empty when it is synced or syncing.
  function domainReason({ reason, error }: DomainView): string {
    if (!reason) return ''
    const meaning = DOMAIN_REASONS[reason]
    if (!meaning) return reason
    return meaning.detail && error ? `${meaning.text}: ${error}` : `${meaning.text}.`
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
    info.value = { ...next, settings: next.settings ?? info.value?.settings ?? null }
  }

  function showWorkers(next: WorkerListItem[]) {
    workers.value = next
    workersError.value = ''
  }

  async function refresh() {
    try {
      apply(await fetchAppInfo())
    } catch (err) {
      console.error('Failed to refresh the app info:', err)
    }
  }

  // The first info, which the app waits for, and the workers, which it does not; then the manager pushes each change.
  function init() {
    if (!started) {
      watchState('workers', fetchWorkers, showWorkers).first.catch(err => {
        workersError.value = err instanceof Error ? err.message : 'Failed to load workers'
      })
      started = watchState('info', fetchAppInfo, apply).first
    }
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

  function setSettingTesting(section: SettingSection) {
    const entry = info.value?.settings?.[section]
    if (entry) entry.testing = true
  }

  return {
    // State
    info,
    workers,
    workersError,
    // Getters
    workersIndicator,
    workerStatus,
    workerCertificate,
    certificateIndicator,
    tunnelIndicator,
    sectionIndicator,
    applicationIndicator,
    containerIndicator,
    workerIndicator,
    workerReason,
    pathIndicator,
    infraIndicator,
    domainIndicator,
    domainReason,
    taskIndicator,
    // Actions
    init,
    refresh,
    setState,
    setSettingApplying,
    setSettingTesting,
  }
})
