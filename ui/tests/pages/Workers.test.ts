import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import Workers from '@/pages/Workers/index.vue'
import WorkerCard from '@/pages/Workers/WorkerCard.vue'
import { fetchWorkerMetrics, type WorkerListItem, type WorkerMinute } from '@/services/api'
import { useAppStore } from '@/stores/app'

vi.mock('vue-router', () => ({ useRouter: () => ({ push: vi.fn() }) }))
vi.mock('@/services/api', async importOriginal => ({
  ...(await importOriginal<typeof import('@/services/api')>()),
  fetchWorkerMetrics: vi.fn(),
}))

const card = {
  props: ['worker'],
  template: '<div data-card>{{ worker.hostname }}:{{ worker.status }}</div>',
}

function worker(hostname: string, overrides: Partial<WorkerListItem> = {}): WorkerListItem {
  return {
    hostname,
    ip: '100.64.0.2',
    online: true,
    status: 'online',
    path: 'direct',
    container_count: 3,
    infra: null,
    metrics: null,
    ...overrides,
  }
}

// The page shows what the app store holds of the workers, which the store follows for the whole app.
async function mountPage(workers: WorkerListItem[] | null = null) {
  const store = useAppStore()
  store.workers = workers
  const wrapper = mount(Workers, {
    global: { stubs: { WorkerCard: card, AddWorkerButton: true } },
  })
  await flushPromises()
  return { wrapper, store }
}

const cards = (wrapper: Awaited<ReturnType<typeof mountPage>>['wrapper']) =>
  wrapper.findAll('[data-card]').map(found => found.text())

beforeEach(() => {
  setActivePinia(createPinia())
  vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval', 'setTimeout', 'clearTimeout'] })
  vi.mocked(fetchWorkerMetrics).mockRejectedValue(new Error('no metrics'))
  vi.spyOn(console, 'error').mockImplementation(() => {})
})

afterEach(() => {
  vi.useRealTimers()
})

describe('timers', () => {
  it('has none: the workers and their metrics come with the list the store follows', async () => {
    const set = vi.spyOn(globalThis, 'setInterval')
    await mountPage([worker('w1')])
    expect(set).not.toHaveBeenCalled()
  })
})

describe('loading', () => {
  it('shows a spinner until the workers have loaded', async () => {
    const { wrapper, store } = await mountPage()
    expect(wrapper.text()).toContain('Loading workers...')

    store.workers = [worker('w1')]
    await flushPromises()

    expect(wrapper.text()).not.toContain('Loading workers...')
    expect(cards(wrapper)).toEqual(['w1:online'])
  })

  it('says why they could not be loaded', async () => {
    const { wrapper, store } = await mountPage()

    store.workersError = 'Failed to fetch workers'
    await flushPromises()

    expect(wrapper.text()).not.toContain('Loading workers...')
    expect(wrapper.text()).toContain('Failed to load workers')
    expect(wrapper.text()).toContain('Failed to fetch workers')
  })

  it('shows the workers that arrive after a failure, and no longer the failure', async () => {
    const { wrapper, store } = await mountPage()
    store.workersError = 'down'
    await flushPromises()

    store.workers = [worker('w1')]
    store.workersError = ''
    await flushPromises()

    expect(wrapper.text()).not.toContain('Failed to load workers')
    expect(cards(wrapper)).toEqual(['w1:online'])
  })

  it('says there are none when there are none', async () => {
    const { wrapper } = await mountPage([])

    expect(wrapper.text()).toContain('No workers found')
  })
})

describe('what the store holds', () => {
  it('shows the workers it holds, and the status each has now', async () => {
    const { wrapper, store } = await mountPage([worker('w1'), worker('w2')])
    expect(cards(wrapper)).toEqual(['w1:online', 'w2:online'])

    store.workers = [worker('w1', { status: 'offline' })]
    await flushPromises()

    expect(cards(wrapper)).toEqual(['w1:offline'])
  })
})

describe('the metrics', () => {
  it('are not asked for, not even when a worker is added', async () => {
    const { store } = await mountPage([worker('w1')])
    store.workers = [worker('w1'), worker('w2')]
    await flushPromises()
    await vi.advanceTimersByTimeAsync(120_000)

    expect(fetchWorkerMetrics).not.toHaveBeenCalled()
  })
})

describe('a worker card', () => {
  function mountCard(listed: WorkerListItem) {
    return mount(WorkerCard, { props: { worker: listed } })
  }

  it('shows the container count of the worker', () => {
    expect(mountCard(worker('w1', { container_count: 4 })).text()).toContain('4')
  })

  it('shows a dash for a worker that has no current report', () => {
    const text = mountCard(worker('w1', { container_count: null })).text()
    expect(text).toMatch(/Containers\s*-/)
  })

  it('shows a worker without containers as none and not as unknown', () => {
    const text = mountCard(worker('w1', { container_count: 0 })).text()
    expect(text).toMatch(/Containers\s*0/)
  })

  // The newest minute of a worker with 4 cores, 8000 MB of memory and 100 GB of disk.
  const minute = (overrides: Partial<WorkerMinute> = {}): WorkerMinute => ({
    ts: '2026-10-29T12:00:00+00:00',
    cpu_pct: 37.5,
    mem_used_mb: 2000,
    mem_cached_mb: 500,
    disk_used_gb: 25,
    load_avg_1m: 0.5,
    load_avg_5m: 0.4,
    load_avg_15m: 0.3,
    net_rx_kbps: 1234,
    net_tx_kbps: 56,
    cpu_cores: 4,
    mem_total_mb: 8000,
    disk_total_gb: 100,
    ...overrides,
  })

  it('shows the newest minute of metrics the list holds, the network in Mbps', () => {
    const text = mountCard(worker('w1', { metrics: minute() })).text()
    expect(text).toContain('4 cores')
    expect(text).toContain('2000 / 8000 MB')
    expect(text).toContain('25 / 100 GB')
    expect(text).toContain('1.2 Mbps')
    expect(text).toContain('0.1 Mbps')
  })

  it('shows no metrics for a worker whose list entry has none', () => {
    const text = mountCard(worker('w1', { metrics: null })).text()
    expect(text).not.toContain('cores')
    expect(text).not.toContain('MB')
  })
})
