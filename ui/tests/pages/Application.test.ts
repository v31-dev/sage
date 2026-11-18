import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { watchState } from '@/lib/updates'
import Application from '@/pages/Application/index.vue'
import { fetchApplication, type Application as ApplicationState } from '@/services/api'
import { container } from '../helpers/containers'
import { followTopics } from '../helpers/topics'

vi.mock('@/lib/updates', () => ({ watchState: vi.fn(), watchFeed: vi.fn() }))
vi.mock('vue-router', () => ({
  useRoute: () => ({ params: { projectId: 'demo', appId: 'web' } }),
  useRouter: () => ({ push: vi.fn() }),
}))

vi.mock('@/services/api', async importOriginal => ({
  ...(await importOriginal<typeof import('@/services/api')>()),
  fetchApplication: vi.fn(),
  getContainerAPI: vi.fn(() => ({})),
  getVolumeAPI: vi.fn(() => ({})),
}))

const header = {
  props: ['application', 'loadApplication'],
  template:
    '<button data-header @click="loadApplication()">{{ application.name }}:{{ application.active_count }}/{{ application.container_count }}</button>',
}
const list = (attribute: string, path: string) => ({
  props: ['container', 'volume'],
  template: `<div ${attribute}>{{ ($props.container ?? $props.volume).${path} }}</div>`,
})

// The fields the page shows; the rest of an application is left out.
const application = (overrides: Record<string, unknown> = {}) =>
  ({
    name: 'web',
    container_count: 0,
    active_count: 0,
    updated_at: '2026-10-29T10:00:00',
    containers: [],
    volumes: [{ name: 'data' }],
    ...overrides,
  }) as unknown as ApplicationState

const stubs = {
  ApplicationHeader: header,
  AddContainerButton: true,
  AddVolumeButton: true,
  SectionTitle: true,
  ContainerCard: list('data-container', 'view.headline'),
  VolumeCard: list('data-volume', 'name'),
}

async function mountPage() {
  const topics = followTopics({ pending: true })
  const wrapper = mount(Application, { global: { stubs } })
  await flushPromises()
  return { wrapper, topics }
}

async function loaded(
  topics: Awaited<ReturnType<typeof mountPage>>['topics'],
  state = application()
) {
  topics.topic('application:demo/web').push(state)
  topics.topic('application:demo/web').loaded()
  await flushPromises()
}

const texts = (wrapper: Awaited<ReturnType<typeof mountPage>>['wrapper'], attribute: string) =>
  wrapper.findAll(`[${attribute}]`).map(found => found.text())

beforeEach(() => {
  setActivePinia(createPinia())
  vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval', 'setTimeout', 'clearTimeout'] })
  vi.mocked(fetchApplication).mockReset()
  vi.mocked(fetchApplication).mockResolvedValue(application())
  vi.spyOn(console, 'error').mockImplementation(() => {})
})

afterEach(() => {
  vi.useRealTimers()
})

describe('what the page follows', () => {
  it('follows the application of its project and name, loaded from its route, and stops when it goes away', async () => {
    const { wrapper, topics } = await mountPage()
    expect(watchState).toHaveBeenCalledWith(
      'application:demo/web',
      expect.any(Function),
      expect.any(Function),
      expect.any(Function)
    )
    await topics.topic('application:demo/web').load()
    expect(fetchApplication).toHaveBeenCalledWith('demo', 'web')

    wrapper.unmount()

    expect(topics.topic('application:demo/web').stop).toHaveBeenCalledOnce()
  })

  it('has no timer of its own', async () => {
    const set = vi.spyOn(globalThis, 'setInterval')
    await mountPage()
    expect(set).not.toHaveBeenCalled()
  })
})

describe('loading', () => {
  it('shows a spinner until the application has loaded, then it with its volumes', async () => {
    const { wrapper, topics } = await mountPage()
    expect(wrapper.text()).toContain('Loading application...')

    await loaded(topics)

    expect(wrapper.text()).not.toContain('Loading application...')
    expect(wrapper.find('[data-header]').text()).toBe('web:0/0')
    expect(texts(wrapper, 'data-volume')).toEqual(['data'])
  })

  it('says an application that is not there is not found, and why any other failure happened', async () => {
    const { wrapper, topics } = await mountPage()
    topics
      .topic('application:demo/web')
      .failed(Object.assign(new Error('Application not found'), { status: 404 }))
    await flushPromises()
    expect(wrapper.text()).not.toContain('Failed to load application')

    const other = await mountPage()
    other.topics.topic('application:demo/web').failed(new Error('Failed to fetch application'))
    await flushPromises()
    expect(other.wrapper.text()).toContain('Failed to fetch application')
  })
})

describe('what the manager pushes', () => {
  it('says an application that is deleted while the page is open is not found', async () => {
    const { wrapper, topics } = await mountPage()
    await loaded(topics)

    topics.topic('application:demo/web').gone()
    await flushPromises()

    expect(wrapper.find('[data-header]').exists()).toBe(false)
    expect(wrapper.text()).toContain('Application web not found')
  })

  it('shows the status, the containers and the rest that change as it runs', async () => {
    const { wrapper, topics } = await mountPage()
    await loaded(topics)

    topics
      .topic('application:demo/web')
      .push(application({ container_count: 1, active_count: 1, containers: [container()] }))
    await flushPromises()

    expect(wrapper.find('[data-header]').text()).toBe('web:1/1')
    expect(texts(wrapper, 'data-container')).toEqual(['active'])
  })

  it('shows the volumes it pushes, changed elsewhere', async () => {
    const { wrapper, topics } = await mountPage()
    await loaded(topics)

    topics.topic('application:demo/web').push(application({ volumes: [] }))
    await flushPromises()

    expect(texts(wrapper, 'data-volume')).toEqual([])
  })

  it('reloads all of it after an action, keeping the page shown while it loads', async () => {
    const { wrapper, topics } = await mountPage()
    await loaded(topics)
    let answer: (value: ApplicationState) => void = () => {}
    vi.mocked(fetchApplication).mockReturnValue(new Promise(resolve => (answer = resolve)))

    await wrapper.find('[data-header]').trigger('click')
    await flushPromises()
    expect(wrapper.text()).not.toContain('Loading application...')
    expect(wrapper.find('[data-header]').exists()).toBe(true)

    answer(
      application({
        volumes: [],
        container_count: 1,
        containers: [
          container({ live: 'stop', view: { ...container().view, headline: 'stopping' } }),
        ],
      })
    )
    await flushPromises()

    expect(texts(wrapper, 'data-volume')).toEqual([])
    expect(texts(wrapper, 'data-container')).toEqual(['stopping'])
    expect(wrapper.find('[data-header]').text()).toBe('web:0/1')
  })
})
