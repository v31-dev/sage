import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import Workers from '@/pages/Workers/index.vue'
import WorkerCard from '@/pages/Workers/WorkerCard.vue'
import { fetchWorkerMetrics, METRICS_POLL_MS, type WorkerListItem } from '@/services/api'
import { useAppStore } from '@/stores/app'

vi.mock('vue-router', () => ({ useRouter: () => ({ push: vi.fn() }) }))
vi.mock('@/services/api', async importOriginal => ({
  ...(await importOriginal<typeof import('@/services/api')>()),
  fetchWorkerMetrics: vi.fn(),
}))

const card = {
  props: ['worker', 'metrics'],
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
  it('has none for the workers, only the one for the metrics, and leaves it behind no more', async () => {
    const set = vi.spyOn(globalThis, 'setInterval')
    const clear = vi.spyOn(globalThis, 'clearInterval')
    const { wrapper } = await mountPage([worker('w1')])
    expect(set).toHaveBeenCalledTimes(1)
    expect(set.mock.calls[0]![1]).toBe(METRICS_POLL_MS)

    wrapper.unmount()

    expect(clear).toHaveBeenCalledWith(set.mock.results[0]!.value)
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
  it('are asked for every worker once a minute, and the workers are not', async () => {
    await mountPage([worker('w1'), worker('w2')])
    vi.mocked(fetchWorkerMetrics).mockClear()

    await vi.advanceTimersByTimeAsync(60_000)

    expect(vi.mocked(fetchWorkerMetrics).mock.calls.map(call => call[0])).toEqual(['w1', 'w2'])
  })

  it('are asked for when the workers are first known and when one is added, not when only a status changes', async () => {
    const { store } = await mountPage()
    expect(fetchWorkerMetrics).not.toHaveBeenCalled()

    store.workers = [worker('w1')]
    await flushPromises()
    expect(vi.mocked(fetchWorkerMetrics).mock.calls.map(call => call[0])).toEqual(['w1'])

    store.workers = [worker('w1', { status: 'rebooting' })]
    await flushPromises()
    expect(fetchWorkerMetrics).toHaveBeenCalledTimes(1)

    store.workers = [worker('w1'), worker('w2')]
    await flushPromises()
    expect(
      vi
        .mocked(fetchWorkerMetrics)
        .mock.calls.slice(1)
        .map(call => call[0])
    ).toEqual(['w1', 'w2'])
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
})
