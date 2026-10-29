import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { watchState } from '@/lib/updates'
import Application from '@/pages/Application/index.vue'
import { getApplicationAPI } from '@/services/api'
import { useAppStore } from '@/stores/app'
import { followTopics } from '../helpers/topics'

vi.mock('@/lib/updates', () => ({ watchState: vi.fn(), watchFeed: vi.fn() }))
vi.mock('vue-router', () => ({
  useRoute: () => ({ params: { projectId: 'demo', appId: 'web' } }),
  useRouter: () => ({ push: vi.fn() }),
}))

const fetchOne = vi.fn()
vi.mock('@/services/api', async importOriginal => ({
  ...(await importOriginal<typeof import('@/services/api')>()),
  getApplicationAPI: vi.fn(() => ({ fetchOne })),
  getContainerAPI: vi.fn(() => ({})),
  getDomainAPI: vi.fn(() => ({})),
  getVolumeAPI: vi.fn(() => ({})),
}))

const header = {
  props: ['application', 'loadApplication'],
  template:
    '<button data-header @click="loadApplication()">{{ application.name }}:{{ application.status }}:{{ application.config_dirty }}</button>',
}
const list = (attribute: string, key: string) => ({
  props: ['container', 'volume', 'domain'],
  template: `<div ${attribute}>{{ ($props.container ?? $props.volume ?? $props.domain).${key} }}</div>`,
})

const application = (overrides: Record<string, unknown> = {}) => ({
  name: 'web',
  status: 'inactive',
  container_count: 0,
  updated_at: '2026-10-29T10:00:00',
  domains_synced: true,
  deployed_at: null,
  config_dirty: false,
  containers: [],
  volumes: [{ name: 'data' }],
  domains: [{ name: 'web.example.test' }],
  ...overrides,
})

const stubs = {
  ApplicationHeader: header,
  AddContainerButton: true,
  AddDomainButton: true,
  AddVolumeButton: true,
  SectionTitle: true,
  ContainerCard: list('data-container', 'status'),
  VolumeCard: list('data-volume', 'name'),
  DomainCard: list('data-domain', 'name'),
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
  fetchOne.mockReset()
  fetchOne.mockResolvedValue(application())
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
      expect.any(Function)
    )
    expect(getApplicationAPI).toHaveBeenCalledWith('demo')
    await topics.topic('application:demo/web').load()
    expect(fetchOne).toHaveBeenCalledWith('web')

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
  it('shows a spinner until the application has loaded, then it with its volumes and domains', async () => {
    const { wrapper, topics } = await mountPage()
    expect(wrapper.text()).toContain('Loading application...')

    await loaded(topics)

    expect(wrapper.text()).not.toContain('Loading application...')
    expect(wrapper.find('[data-header]').text()).toBe('web:inactive:false')
    expect(texts(wrapper, 'data-volume')).toEqual(['data'])
    expect(texts(wrapper, 'data-domain')).toEqual(['web.example.test'])
  })

  it('gives the deploy status to the app store', async () => {
    const { topics } = await mountPage()
    await loaded(topics, application({ status: 'deploying' }))
    expect(useAppStore().applicationDeployStatus).toBe('deploying')
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
  it('shows the status, the containers and the rest that change as it runs', async () => {
    const { wrapper, topics } = await mountPage()
    await loaded(topics)

    topics.topic('application:demo/web').push(
      application({
        status: 'active',
        config_dirty: true,
        containers: [{ status: 'active', worker: { hostname: 'w1' } }],
      })
    )
    await flushPromises()

    expect(wrapper.find('[data-header]').text()).toBe('web:active:true')
    expect(texts(wrapper, 'data-container')).toEqual(['active'])
    expect(useAppStore().applicationDeployStatus).toBe('active')
  })

  it('leaves the volumes and the domains as they were loaded, for a reload after an action to change', async () => {
    const { wrapper, topics } = await mountPage()
    await loaded(topics)

    topics
      .topic('application:demo/web')
      .push(application({ volumes: [], domains: [{ name: 'other.example.test' }] }))
    await flushPromises()

    expect(texts(wrapper, 'data-volume')).toEqual(['data'])
    expect(texts(wrapper, 'data-domain')).toEqual(['web.example.test'])
  })

  it('reloads all of it after an action', async () => {
    const { wrapper, topics } = await mountPage()
    await loaded(topics)
    fetchOne.mockResolvedValue(application({ volumes: [], status: 'stopping' }))

    await wrapper.find('[data-header]').trigger('click')
    await flushPromises()

    expect(texts(wrapper, 'data-volume')).toEqual([])
    expect(wrapper.find('[data-header]').text()).toBe('web:stopping:false')
  })
})
