import { flushPromises, mount, RouterLinkStub } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import Domains from '@/pages/Domains'
import {
  type AppInfo,
  type Domain,
  type DomainsResponse,
  type ContainerReference,
  type DomainStatus,
} from '@/services/api'
import { useAppStore } from '@/stores/app'
import { followTopics } from '../helpers/topics'

vi.mock('@/lib/updates', () => ({ watchState: vi.fn(), watchFeed: vi.fn() }))
vi.mock('vue-sonner', () => ({ toast: { success: vi.fn(), error: vi.fn() } }))
vi.mock('@/services/api', async importOriginal => ({
  ...(await importOriginal<typeof import('@/services/api')>()),
  fetchDomains: vi.fn(),
}))

const application = { name: 'web', label: 'Web', project: { name: 'demo', label: 'Demo' } }

const linkable = (id: number, hostname: string, subdomain: string): ContainerReference => ({
  id: String(id),
  worker: { hostname, subdomain },
  application,
  view: { headline: 'active', config: 'current', reason: null, detail: null },
})

// A domain, with its state's `status` and `error` given apart.
const domain = (
  id: number,
  {
    status = 'synced',
    error = null,
    ...overrides
  }: Partial<Domain> & { status?: DomainStatus; error?: string | null }
): Domain => ({
  id: String(id),
  name: 'shop',
  type: 'public',
  port: 80,
  service: null,
  pool: null,
  cors_origins: null,
  host: 'shop.example.test',
  address: 'https://shop.example.test',
  worker: 'w1',
  container: { id: '1', worker: 'w1', application },
  view: { status, conforms: status === 'synced', reason: status === 'error' ? 'dns' : null, error },
  created_at: '2026-01-01T00:00:00',
  updated_at: '2026-01-01T00:00:00',
  ...overrides,
})

beforeEach(() => {
  setActivePinia(createPinia())
  useAppStore().info = { domain: 'example.test' } as AppInfo
})

describe('the Domains page', () => {
  function shown(state: DomainsResponse) {
    const topics = followTopics()
    const wrapper = mount(Domains, {
      global: {
        stubs: {
          DomainDialog: true,
          ConfirmationButton: true,
          CopyButton: true,
          RouterLink: RouterLinkStub,
        },
      },
    })
    topics.topic('domains').push(state)
    return wrapper
  }

  it('updates the record sync state from the domains topic and retains the container state', async () => {
    const topics = followTopics()
    const wrapper = mount(Domains, {
      global: {
        stubs: {
          DomainDialog: true,
          ConfirmationButton: true,
          CopyButton: true,
          RouterLink: RouterLinkStub,
        },
      },
    })
    topics.topic('domains').push({
      domains: [domain(1, { status: 'syncing' })],
      containers: [linkable(1, 'w1', 'red')],
    })
    await flushPromises()
    expect(wrapper.get('[data-domain]').text()).toContain('syncing')

    topics.topic('domains').push({
      domains: [domain(1, { status: 'error', error: 'Traefik: unavailable' })],
      containers: [linkable(1, 'w1', 'red')],
    })
    await flushPromises()
    expect(wrapper.get('[data-domain]').text()).toContain('sync failed')
    expect(wrapper.get('[data-domain]').text()).toContain('active')
    expect(wrapper.get('[data-reason]').text()).toBe(
      'Its DNS records could not be applied: Traefik: unavailable'
    )
  })

  it.each([
    ['worker_not_ready', 'Its worker is not ready.'],
    ['not_applied', 'It has not been applied yet.'],
  ])('says why a pending domain is pending: %s', async (reason, text) => {
    const pending = domain(1, { status: 'pending' })
    const wrapper = shown({
      domains: [{ ...pending, view: { ...pending.view, reason } }],
      containers: [linkable(1, 'w1', 'red')],
    })
    await flushPromises()

    expect(wrapper.get('[data-reason]').text()).toBe(text)
  })

  it('shows the linked container’s state from the containers it holds', async () => {
    const stopped = {
      ...linkable(1, 'w1', 'red'),
      view: {
        headline: 'inactive' as const,
        config: 'current' as const,
        reason: null,
        detail: null,
      },
    }
    const wrapper = shown({ domains: [domain(1, {})], containers: [stopped] })
    await flushPromises()

    expect(wrapper.get('[data-domain]').text()).toContain('inactive')
    expect(wrapper.find('[data-reason]').exists()).toBe(false)
  })

  it('labels the linked container’s state by its reason', async () => {
    const failed = {
      ...linkable(1, 'w1', 'red'),
      view: {
        headline: 'error' as const,
        config: 'current' as const,
        reason: 'failed',
        detail: 'boom',
      },
    }
    const wrapper = shown({ domains: [domain(1, {})], containers: [failed] })
    await flushPromises()

    expect(wrapper.get('[data-domain]').text()).toContain('failed')
    expect(wrapper.get('[data-domain]').text()).not.toContain('boom')
  })

  it('follows the domains topic and gives each namespace and service its own group', async () => {
    const wrapper = shown({
      domains: [
        domain(1, {
          name: 'b',
          host: 'b.example.test',
          address: 'https://b.example.test',
          service: 'shop',
          pool: 'eu',
        }),
        domain(2, {
          name: 'a',
          host: 'a.example.test',
          address: 'https://a.example.test',
          service: 'shop',
        }),
        domain(3, { name: 'c', host: 'c.example.test', address: 'https://c.example.test' }),
        domain(4, {
          name: 'e',
          host: 'e.example.test',
          address: 'https://e.example.test',
          service: 'blog',
        }),
        domain(5, {
          name: 'd',
          type: 'tcp',
          host: 'd.red.int.example.test',
          address: 'd.red.int.example.test:8443',
          service: 'shop',
        }),
      ],
      containers: [],
    })
    await flushPromises()

    const layout = wrapper
      .findAll('[data-group]')
      .map(group => [
        group.get('[data-slot=card-title]').text(),
        group.findAll('[data-domain]').map(card => card.get('[data-host]').text()),
      ])
    expect(layout).toEqual([
      ['Public · Service blog', ['e.example.test']],
      ['Public · Service shop', ['b.example.test', 'a.example.test']],
      ['Public · No service', ['c.example.test']],
      ['Internal · Service shop', ['d.red.int.example.test']],
    ])
  })

  it("links a named service to its namespace's discovery, and says one without a service is not listed", async () => {
    const wrapper = shown({
      domains: [
        domain(1, { service: 'shop' }),
        domain(2, { type: 'internal', service: 'shop' }),
        domain(3, {}),
      ],
      containers: [],
    })
    await flushPromises()

    const groups = wrapper.findAll('[data-group]')
    expect(
      groups.map(group => group.findAll('[data-discovery]').map(link => link.attributes('href')))
    ).toEqual([
      ['https://discovery.example.test/?service=shop'],
      [],
      ['https://discovery.int.example.test/?service=shop'],
    ])
    expect(groups[1]!.text()).toContain('Not listed at discovery')
  })

  it('links a domain to its application and worker pages, and shows its pool only when it has one', async () => {
    const wrapper = shown({
      domains: [domain(1, { pool: 'eu' }), domain(2, {})],
      containers: [linkable(1, 'w1', 'red')],
    })
    await flushPromises()

    const [pooled, plain] = wrapper.findAll('[data-domain]')
    expect(pooled!.getComponent<typeof RouterLinkStub>('[data-application]').props('to')).toBe(
      '/applications/demo/web'
    )
    expect(pooled!.get('[data-application]').text()).toBe('Demo / Web')
    expect(pooled!.getComponent<typeof RouterLinkStub>('[data-worker]').props('to')).toBe(
      '/workers/w1'
    )
    expect(pooled!.get('[data-pool]').text()).toBe('eu')
    expect(plain!.find('[data-pool]').exists()).toBe(false)
  })

  it('opens an HTTP address and shows a TCP one as text', async () => {
    const wrapper = shown({
      domains: [
        domain(1, {}),
        domain(2, {
          type: 'tcp',
          host: 'd.red.int.example.test',
          address: 'd.red.int.example.test:8443',
        }),
      ],
      containers: [],
    })
    await flushPromises()

    const addresses = wrapper.findAll('[data-address]')
    expect(addresses.map(address => [address.element.tagName, address.text()])).toEqual([
      ['A', 'https://shop.example.test'],
      ['SPAN', 'd.red.int.example.test:8443'],
    ])
    expect(addresses[0]!.attributes('href')).toBe('https://shop.example.test')
  })

  it('offers no Add: a domain is added on its container', async () => {
    const wrapper = shown({ domains: [], containers: [] })
    await flushPromises()

    expect(wrapper.findComponent({ name: 'DomainDialog' }).exists()).toBe(false)
    expect(wrapper.text()).toContain("Add one on a container's card")
  })
})
