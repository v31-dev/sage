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
import { useAppStore } from '@/stores/app'

vi.mock('@/services/api', async importOriginal => ({
  ...(await importOriginal<typeof import('@/services/api')>()),
  fetchAppInfo: vi.fn(),
}))

const fetchInfo = vi.mocked(fetchAppInfo)

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
    worker_tag: 'worker',
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

  it('workerIndicator: online and offline', () => {
    const store = useAppStore()
    expect(store.workerIndicator(true)).toEqual({ state: 'ok', label: 'online' })
    expect(store.workerIndicator(false)).toEqual({ state: 'error', label: 'offline' })
  })

  it('workerIndicator: online but not ready is a warning, and unknown readiness is online', () => {
    const store = useAppStore()
    expect(store.workerIndicator(true, false)).toEqual({ state: 'warning', label: 'not ready' })
    expect(store.workerIndicator(true, true)).toEqual({ state: 'ok', label: 'online' })
    expect(store.workerIndicator(true, null)).toEqual({ state: 'ok', label: 'online' })
  })

  it('workerIndicator: an offline worker is offline whatever its readiness says', () => {
    const store = useAppStore()
    expect(store.workerIndicator(false, true)).toEqual({ state: 'error', label: 'offline' })
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

describe('init and the polling', () => {
  beforeEach(() => {
    vi.useFakeTimers()
  })

  it('loads the info before it returns', async () => {
    fetchInfo.mockResolvedValue(appInfo({ version: '2.0.0' }))
    const store = useAppStore()

    await store.init()

    expect(fetchInfo).toHaveBeenCalledTimes(1)
    expect(store.info?.version).toBe('2.0.0')
  })

  it('lets the failure of the first load reach the caller', async () => {
    fetchInfo.mockRejectedValue(new Error('Failed to fetch app info'))

    await expect(useAppStore().init()).rejects.toThrow('Failed to fetch app info')
  })

  it('polls every 30 seconds', async () => {
    fetchInfo.mockResolvedValue(appInfo())
    await useAppStore().init()

    await vi.advanceTimersByTimeAsync(29999)
    expect(fetchInfo).toHaveBeenCalledTimes(1)

    await vi.advanceTimersByTimeAsync(1)
    expect(fetchInfo).toHaveBeenCalledTimes(2)

    await vi.advanceTimersByTimeAsync(30000)
    expect(fetchInfo).toHaveBeenCalledTimes(3)
  })

  it('polls every 2 seconds while a restore runs, and goes back to 30 when it is over', async () => {
    fetchInfo.mockResolvedValue(appInfo({ state: 'restoring' }))
    await useAppStore().init()
    await vi.advanceTimersByTimeAsync(30000)
    expect(fetchInfo).toHaveBeenCalledTimes(2)

    await vi.advanceTimersByTimeAsync(2000)
    await vi.advanceTimersByTimeAsync(2000)
    expect(fetchInfo).toHaveBeenCalledTimes(4)

    fetchInfo.mockResolvedValue(appInfo({ state: 'running' }))
    await vi.advanceTimersByTimeAsync(2000)
    expect(fetchInfo).toHaveBeenCalledTimes(5)

    await vi.advanceTimersByTimeAsync(2000)
    expect(fetchInfo).toHaveBeenCalledTimes(5)
    await vi.advanceTimersByTimeAsync(28000)
    expect(fetchInfo).toHaveBeenCalledTimes(6)
  })

  it('does not start a second refresh while one is waiting for the manager', async () => {
    fetchInfo.mockResolvedValue(appInfo())
    const listen = vi.spyOn(window, 'addEventListener')
    await useAppStore().init()
    const onFocus = listen.mock.calls.find(([type]) => type === 'focus')![1] as () => Promise<void>

    await vi.advanceTimersByTimeAsync(20000)
    let answer: () => void = () => {}
    fetchInfo.mockImplementationOnce(
      () => new Promise(resolve => (answer = () => resolve(appInfo())))
    )
    const refreshing = onFocus()
    expect(fetchInfo).toHaveBeenCalledTimes(2)

    await vi.advanceTimersByTimeAsync(15000)
    expect(fetchInfo).toHaveBeenCalledTimes(2)

    answer()
    await refreshing
    await vi.advanceTimersByTimeAsync(30000)
    expect(fetchInfo).toHaveBeenCalledTimes(3)
  })

  it('keeps polling after a failed refresh and keeps the last info', async () => {
    fetchInfo.mockResolvedValueOnce(appInfo({ version: '1.0.0' }))
    const store = useAppStore()
    await store.init()

    fetchInfo.mockRejectedValueOnce(new Error('down'))
    await vi.advanceTimersByTimeAsync(30000)
    expect(console.error).toHaveBeenCalledWith('Failed to refresh the app info:', expect.any(Error))
    expect(store.info?.version).toBe('1.0.0')

    fetchInfo.mockResolvedValueOnce(appInfo({ version: '1.1.0' }))
    await vi.advanceTimersByTimeAsync(30000)
    expect(store.info?.version).toBe('1.1.0')
  })

  it('keeps the last workers and settings when the manager could not build them', async () => {
    fetchInfo.mockResolvedValueOnce(appInfo({ workers: { total: 3, online: 3 } }))
    const store = useAppStore()
    await store.init()

    fetchInfo.mockResolvedValueOnce(appInfo({ workers: null, settings: null, version: '1.1.0' }))
    await vi.advanceTimersByTimeAsync(30000)

    expect(store.info?.version).toBe('1.1.0')
    expect(store.info?.workers).toEqual({ total: 3, online: 3 })
    expect(store.info?.settings).toEqual(settings())
  })

  it('starts the polling once however often init is called', async () => {
    fetchInfo.mockResolvedValue(appInfo())
    const listen = vi.spyOn(window, 'addEventListener')
    const store = useAppStore()

    await store.init()
    await store.init()
    expect(fetchInfo).toHaveBeenCalledTimes(2)

    fetchInfo.mockClear()
    await vi.advanceTimersByTimeAsync(30000)
    expect(fetchInfo).toHaveBeenCalledTimes(1)
    expect(listen.mock.calls.filter(([type]) => type === 'focus')).toHaveLength(1)
  })

  it('refreshes at once when the window gets focus and restarts the wait', async () => {
    fetchInfo.mockResolvedValue(appInfo())
    const listen = vi.spyOn(window, 'addEventListener')
    await useAppStore().init()
    const onFocus = listen.mock.calls.find(([type]) => type === 'focus')![1] as () => Promise<void>

    await vi.advanceTimersByTimeAsync(20000)
    await onFocus()
    expect(fetchInfo).toHaveBeenCalledTimes(2)

    await vi.advanceTimersByTimeAsync(20000)
    expect(fetchInfo).toHaveBeenCalledTimes(2)
    await vi.advanceTimersByTimeAsync(10000)
    expect(fetchInfo).toHaveBeenCalledTimes(3)
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
