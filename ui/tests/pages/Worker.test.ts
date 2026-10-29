import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { watchState } from '@/lib/updates'
import Worker from '@/pages/Worker/index.vue'
import { fetchWorkerMetrics, workersAPI } from '@/services/api'
import { followTopics } from '../helpers/topics'

vi.mock('@/lib/updates', () => ({ watchState: vi.fn(), watchFeed: vi.fn() }))
vi.mock('vue-router', () => ({
  useRoute: () => ({ params: { hostname: 'w1' } }),
  useRouter: () => ({ push: vi.fn() }),
}))
vi.mock('@/services/api', async importOriginal => ({
  ...(await importOriginal<typeof import('@/services/api')>()),
  fetchWorkerMetrics: vi.fn(),
  workersAPI: { fetchOne: vi.fn() },
}))

const header = {
  props: ['worker', 'workerMetrics'],
  template: '<div data-header>{{ worker.hostname }}:{{ worker.status }}</div>',
}
const containerCard = {
  props: ['container', 'worker', 'loadWorkerStatus'],
  template:
    '<button data-container @click="loadWorkerStatus()">{{ container.application.name }}</button>',
}

const worker = (overrides: Record<string, unknown> = {}) => ({
  hostname: 'w1',
  ip: '100.64.0.2',
  online: true,
  status: 'online',
  path: 'direct',
  infra: null,
  containers: [{ application: { name: 'web', project: { name: 'demo' } } }],
  ...overrides,
})

async function mountPage(options: { pending?: boolean } = {}) {
  const topics = followTopics({ pending: true, ...options })
  const wrapper = mount(Worker, {
    global: {
      stubs: { WorkerHeader: header, WorkerContainerCard: containerCard, SectionTitle: true },
    },
  })
  await flushPromises()
  return { wrapper, topics }
}

// The first load is done: the state is given to the page, and then the load settles.
async function loaded(topics: Awaited<ReturnType<typeof mountPage>>['topics'], state = worker()) {
  topics.topic('worker:w1').push(state)
  topics.topic('worker:w1').loaded()
  await flushPromises()
}

beforeEach(() => {
  vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval', 'setTimeout', 'clearTimeout'] })
  vi.mocked(fetchWorkerMetrics).mockResolvedValue({ host: [] } as never)
  vi.mocked(workersAPI.fetchOne).mockResolvedValue(worker() as never)
  vi.spyOn(console, 'error').mockImplementation(() => {})
})

afterEach(() => {
  vi.useRealTimers()
})

describe('what the page follows', () => {
  it('follows the worker of its hostname, loaded from its route, and stops when it goes away', async () => {
    const { wrapper, topics } = await mountPage()
    expect(watchState).toHaveBeenCalledWith(
      'worker:w1',
      expect.any(Function),
      expect.any(Function),
      expect.any(Function)
    )
    await topics.topic('worker:w1').load()
    expect(workersAPI.fetchOne).toHaveBeenCalledWith('w1')

    wrapper.unmount()

    expect(topics.topic('worker:w1').stop).toHaveBeenCalledOnce()
  })

  it('has no timer of its own', async () => {
    const set = vi.spyOn(globalThis, 'setInterval')
    await mountPage()
    expect(set).not.toHaveBeenCalled()
  })
})

describe('loading', () => {
  it('shows a spinner until the worker has loaded, then the worker and its containers', async () => {
    const { wrapper, topics } = await mountPage()
    expect(wrapper.text()).toContain('Loading worker...')

    await loaded(topics)

    expect(wrapper.text()).not.toContain('Loading worker...')
    expect(wrapper.find('[data-header]').text()).toBe('w1:online')
    expect(wrapper.findAll('[data-container]').map(found => found.text())).toEqual(['web'])
  })

  it('asks for the metrics once the worker has loaded, and not again for what is pushed', async () => {
    const { topics } = await mountPage()
    expect(fetchWorkerMetrics).not.toHaveBeenCalled()

    await loaded(topics)
    topics.topic('worker:w1').push(worker({ status: 'rebooting' }))
    await flushPromises()

    expect(fetchWorkerMetrics).toHaveBeenCalledTimes(1)
    expect(fetchWorkerMetrics).toHaveBeenCalledWith('w1', '1m')
  })

  it('says a worker that is not there is not found', async () => {
    const { wrapper, topics } = await mountPage()

    topics.topic('worker:w1').failed(Object.assign(new Error('Worker not found'), { status: 404 }))
    await flushPromises()

    expect(wrapper.text()).toContain('Worker w1 not found')
  })

  it('says why any other failure happened', async () => {
    const { wrapper, topics } = await mountPage()

    topics.topic('worker:w1').failed(new Error('Failed to fetch worker'))
    await flushPromises()

    expect(wrapper.text()).toContain('Failed to load worker')
    expect(wrapper.text()).toContain('Failed to fetch worker')
  })
})

describe('what the manager pushes', () => {
  it('says a worker that is removed while the page is open is not found', async () => {
    const { wrapper, topics } = await mountPage()
    await loaded(topics)

    topics.topic('worker:w1').gone()
    await flushPromises()

    expect(wrapper.find('[data-header]').exists()).toBe(false)
    expect(wrapper.text()).toContain('Worker w1 not found')
  })

  it('shows the worker it pushes, with its status and its containers now', async () => {
    const { wrapper, topics } = await mountPage()
    await loaded(topics)

    topics.topic('worker:w1').push(
      worker({
        status: 'rebooting',
        containers: [
          { application: { name: 'web', project: { name: 'demo' } } },
          { application: { name: 'api', project: { name: 'demo' } } },
        ],
      })
    )
    await flushPromises()

    expect(wrapper.find('[data-header]').text()).toBe('w1:rebooting')
    expect(wrapper.findAll('[data-container]').map(found => found.text())).toEqual(['web', 'api'])
  })

  it('shows what an action on a container changed at once, without waiting for the push', async () => {
    const { wrapper, topics } = await mountPage()
    await loaded(topics)
    vi.mocked(workersAPI.fetchOne).mockResolvedValue(worker({ containers: [] }) as never)

    await wrapper.find('[data-container]').trigger('click')
    await flushPromises()

    expect(wrapper.findAll('[data-container]')).toHaveLength(0)
  })
})
