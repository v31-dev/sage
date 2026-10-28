import { createPinia, setActivePinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  fetchAppInfo,
  type AppInfo,
  type Application,
  type Container,
  type SettingSection,
  type SettingsStatus,
  type SettingState,
} from '@/services/api'
import { watchState } from '@/lib/updates'
import { useAppStore } from '@/stores/app'

vi.mock('@/services/api', async importOriginal => ({
  ...(await importOriginal<typeof import('@/services/api')>()),
  fetchAppInfo: vi.fn(),
}))
vi.mock('@/lib/updates', () => ({ watchState: vi.fn() }))

const fetchInfo = vi.mocked(fetchAppInfo)
const watch = vi.mocked(watchState)

const last = <T>(items: T[]) => items[items.length - 1]!

// The topic as it is when the first state arrives at once. `onData()` is what the store gave to be called with each state.
function onData() {
  return last(watch.mock.calls)![2] as unknown as (info: AppInfo) => void
}

function settings(state: SettingState = 'ok'): SettingsStatus {
  const section = { state, message: null, task_id: null, checked_at: null, details: null }
  return {
    tailscale: { ...section, channels: null },
    cloudflare: { ...section, channels: null },
    s3: { ...section, channels: null },
    notifications: { ...section, channels: null },
  }
}

function appInfo(overrides: Partial<AppInfo> = {}): AppInfo {
  return {
    version: '1.0.0',
    latest_version: null,
    domain: 'example.test',
    name: 'manager',
    ip: '100.64.0.1',
    start_time: '2026-10-24T10:00:00Z',
    state: 'running',
    restoring_from: null,
    workers: { total: 2, online: 2 },
    settings: settings(),
    ...overrides,
  }
}

function container(hostname: string, status: Application['status'] = 'active'): Container {
  return { worker: { hostname }, status } as Container
}

beforeEach(() => {
  setActivePinia(createPinia())
  vi.spyOn(console, 'error').mockImplementation(() => {})
  watch.mockImplementation(((
    _topic: string,
    load: () => Promise<AppInfo>,
    receive: (info: AppInfo) => void
  ) => ({
    stop: vi.fn(),
    first: load().then(receive),
  })) as unknown as typeof watchState)
})

afterEach(() => {
  vi.useRealTimers()
})

describe('workersIndicator', () => {
  it('shows nothing until the info has arrived or when the manager sent no workers', async () => {
    const store = useAppStore()
    expect(store.workersIndicator).toBeUndefined()

    fetchInfo.mockResolvedValue(appInfo({ workers: null }))
    await store.init()
    expect(store.workersIndicator).toBeUndefined()
  })

  it.each([
    [
      { total: 0, online: 0 },
      { state: 'idle', label: 'No workers' },
    ],
    [
      { total: 3, online: 3 },
      { state: 'ok', label: '3 of 3 online' },
    ],
    [
      { total: 3, online: 2 },
      { state: 'error', label: '2 of 3 online' },
    ],
    [
      { total: 2, online: 0 },
      { state: 'error', label: '0 of 2 online' },
    ],
  ])('shows %j as %j', async (workers, indicator) => {
    fetchInfo.mockResolvedValue(appInfo({ workers }))
    const store = useAppStore()

    await store.init()

    expect(store.workersIndicator).toEqual(indicator)
  })
})

describe('sectionIndicator', () => {
  it.each([
    ['not_configured', { state: 'idle', label: 'Not configured' }],
    ['applying', { state: 'busy', label: 'Applying' }],
    ['ok', { state: 'ok', label: 'OK' }],
    ['retrying', { state: 'warning', label: 'Retrying' }],
    ['error', { state: 'error', label: 'Error' }],
  ] as const)('shows the state %s', async (state, indicator) => {
    fetchInfo.mockResolvedValue(appInfo({ settings: settings(state) }))
    const store = useAppStore()

    await store.init()

    expect(store.sectionIndicator('s3')).toEqual(indicator)
  })

  it('shows nothing before the info arrives or without settings', async () => {
    const store = useAppStore()
    expect(store.sectionIndicator('s3')).toBeUndefined()

    fetchInfo.mockResolvedValue(appInfo({ settings: null }))
    await store.init()
    expect(store.sectionIndicator('s3')).toBeUndefined()
  })

  it('reads the section it is asked for', async () => {
    const mixed = settings('ok')
    mixed.cloudflare.state = 'error'
    fetchInfo.mockResolvedValue(appInfo({ settings: mixed }))
    const store = useAppStore()

    await store.init()

    const sections: SettingSection[] = ['tailscale', 'cloudflare', 's3', 'notifications']
    expect(sections.map(section => store.sectionIndicator(section)?.label)).toEqual([
      'OK',
      'Error',
      'OK',
      'OK',
    ])
  })
})

describe('the indicators by status', () => {
  it.each([
    ['active', 'ok'],
    ['error', 'error'],
    ['deploying', 'busy'],
    ['stopping', 'busy'],
    ['backup', 'busy'],
    ['restoring', 'busy'],
    ['inactive', 'idle'],
  ] as const)('applicationIndicator: %s is %s', (status, state) => {
    expect(useAppStore().applicationIndicator(status)).toEqual({ state, label: status })
  })

  it.each([
    ['online', 'ok'],
    ['not ready', 'warning'],
    ['offline', 'error'],
    ['adding', 'busy'],
    ['rebooting', 'busy'],
    ['removing', 'busy'],
  ] as const)('workerIndicator: %s is %s', (status, state) => {
    expect(useAppStore().workerIndicator(status)).toEqual({ state, label: status })
  })

  it.each([
    ['direct', 'ok'],
    ['relay', 'warning'],
    ['relay blr', 'warning'],
  ])('pathIndicator: %s is %s', (path, state) => {
    expect(useAppStore().pathIndicator(path)).toEqual({ state, label: path })
  })

  it.each([
    ['running', 'ok'],
    ['restarting', 'warning'],
    ['paused', 'warning'],
    ['exited', 'error'],
    ['missing', 'error'],
  ] as const)('infraIndicator: a stack container that is %s is %s', (status, state) => {
    expect(useAppStore().infraIndicator({ name: 'traefik', status })).toEqual({
      state,
      label: `traefik: ${status}`,
    })
  })

  it('domainIndicator: synced and syncing', () => {
    const store = useAppStore()
    expect(store.domainIndicator(true)).toEqual({ state: 'ok', label: 'synced' })
    expect(store.domainIndicator(false)).toEqual({ state: 'busy', label: 'syncing' })
  })

  it.each([
    ['completed', 'ok'],
    ['failed', 'error'],
    ['cancelled', 'warning'],
    ['running', 'busy'],
    ['queued', 'idle'],
    ['anything else', 'idle'],
  ])('taskIndicator: %s is %s', (status, state) => {
    expect(useAppStore().taskIndicator(status)).toEqual({ state, label: status })
  })
})

describe('init and the info topic', () => {
  it('follows the info topic and has the info when it returns', async () => {
    fetchInfo.mockResolvedValue(appInfo({ version: '2.0.0' }))
    const store = useAppStore()

    await store.init()

    expect(watch).toHaveBeenCalledWith('info', fetchAppInfo, expect.anything())
    expect(store.info?.version).toBe('2.0.0')
  })

  it('lets the failure of the first load reach the caller', async () => {
    fetchInfo.mockRejectedValue(new Error('Failed to fetch app info'))

    await expect(useAppStore().init()).rejects.toThrow('Failed to fetch app info')
  })

  it('follows the topic once however often init is called', async () => {
    fetchInfo.mockResolvedValue(appInfo())
    const store = useAppStore()

    await store.init()
    await store.init()

    expect(watch).toHaveBeenCalledTimes(1)
  })

  it('takes each state the manager pushes', async () => {
    fetchInfo.mockResolvedValue(appInfo({ version: '1.0.0' }))
    const store = useAppStore()
    await store.init()

    onData()(appInfo({ version: '1.1.0', state: 'paused' }))

    expect(store.info?.version).toBe('1.1.0')
    expect(store.info?.state).toBe('paused')
  })

  it('keeps the last workers and settings when the manager could not build them', async () => {
    fetchInfo.mockResolvedValue(appInfo({ workers: { total: 3, online: 3 } }))
    const store = useAppStore()
    await store.init()

    onData()(appInfo({ workers: null, settings: null, version: '1.1.0' }))

    expect(store.info?.version).toBe('1.1.0')
    expect(store.info?.workers).toEqual({ total: 3, online: 3 })
    expect(store.info?.settings).toEqual(settings())
  })
})

describe('refresh', () => {
  it('loads the info once more', async () => {
    fetchInfo.mockResolvedValueOnce(appInfo({ version: '1.0.0' }))
    const store = useAppStore()
    await store.init()

    fetchInfo.mockResolvedValueOnce(appInfo({ version: '1.2.0' }))
    await store.refresh()

    expect(store.info?.version).toBe('1.2.0')
  })

  it('keeps the last info when the manager does not answer', async () => {
    fetchInfo.mockResolvedValueOnce(appInfo({ version: '1.0.0' }))
    const store = useAppStore()
    await store.init()

    fetchInfo.mockRejectedValueOnce(new Error('down'))
    await store.refresh()

    expect(console.error).toHaveBeenCalledWith('Failed to refresh the app info:', expect.any(Error))
    expect(store.info?.version).toBe('1.0.0')
  })

  it('keeps the last workers and settings when the manager could not build them', async () => {
    fetchInfo.mockResolvedValueOnce(appInfo({ workers: { total: 3, online: 3 } }))
    const store = useAppStore()
    await store.init()

    fetchInfo.mockResolvedValueOnce(appInfo({ workers: null, settings: null }))
    await store.refresh()

    expect(store.info?.workers).toEqual({ total: 3, online: 3 })
    expect(store.info?.settings).toEqual(settings())
  })
})

describe('the changes an action makes at once', () => {
  it('setState changes the state of the info, and does nothing before the info is there', async () => {
    const store = useAppStore()
    store.setState('paused')
    expect(store.info).toBeNull()

    fetchInfo.mockResolvedValue(appInfo())
    await store.init()
    store.setState('paused')
    expect(store.info?.state).toBe('paused')
  })

  it('setSettingApplying marks only that section', async () => {
    fetchInfo.mockResolvedValue(appInfo())
    const store = useAppStore()
    await store.init()

    store.setSettingApplying('s3')

    expect(store.sectionIndicator('s3')).toEqual({ state: 'busy', label: 'Applying' })
    expect(store.sectionIndicator('cloudflare')).toEqual({ state: 'ok', label: 'OK' })
  })

  it('setSettingApplying does nothing without the info', () => {
    const store = useAppStore()
    store.setSettingApplying('s3')
    expect(store.info).toBeNull()
  })

  it('updateApplicationDeployStatus sets the status and the containers it names', () => {
    const store = useAppStore()
    expect(store.applicationDeployStatus).toBe('inactive')

    store.updateApplicationDeployStatus('deploying', ['w1', 'w2'])

    expect(store.applicationDeployStatus).toBe('deploying')
    expect(store.containerStatus(container('w1', 'active'))).toBe('deploying')
    expect(store.containerStatus(container('w2', 'inactive'))).toBe('deploying')
    expect(store.containerStatus(container('w3', 'active'))).toBe('active')
  })

  it('updateApplicationDeployStatus forgets the containers of the earlier action', () => {
    const store = useAppStore()
    store.updateApplicationDeployStatus('deploying', ['w1'])

    store.updateApplicationDeployStatus('stopping')

    expect(store.containerStatus(container('w1', 'active'))).toBe('active')
  })

  it('updateContainerStatus changes one container and keeps the others', () => {
    const store = useAppStore()
    store.updateApplicationDeployStatus('deploying', ['w1', 'w2'])

    store.updateContainerStatus('w1', 'error')

    expect(store.containerStatus(container('w1', 'active'))).toBe('error')
    expect(store.containerStatus(container('w2', 'active'))).toBe('deploying')
  })
})
