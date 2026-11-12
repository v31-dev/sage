import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import Domains from '@/pages/Domains'
import DomainDialog from '@/pages/Domains/DomainDialog.vue'
import {
  createDomain,
  updateDomain,
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
  createDomain: vi.fn(async () => ({})),
  updateDomain: vi.fn(async () => ({})),
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
  created_at: new Date('2026-01-01T00:00:00Z'),
  updated_at: new Date('2026-01-01T00:00:00Z'),
  ...overrides,
})

beforeEach(() => {
  setActivePinia(createPinia())
  useAppStore().info = { domain: 'example.test' } as AppInfo
  vi.mocked(createDomain).mockClear()
  vi.mocked(updateDomain).mockClear()
})

describe('the Domains page', () => {
  function shown(state: DomainsResponse) {
    const topics = followTopics()
    const wrapper = mount(Domains, {
      global: { stubs: { DomainDialog: true, ConfirmationButton: true, CopyButton: true } },
    })
    topics.topic('domains').push(state)
    return wrapper
  }

  it('updates the record sync state from the domains topic and retains the container state', async () => {
    const topics = followTopics()
    const wrapper = mount(Domains, {
      global: { stubs: { DomainDialog: true, ConfirmationButton: true, CopyButton: true } },
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

  it('follows the domains topic and groups the domains by namespace, then service, then pool', async () => {
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
      .findAll('[data-namespace]')
      .map(namespace => [
        namespace.get('[data-slot=card-title]').text(),
        namespace
          .findAll('[data-service]')
          .map(service => [
            service.get('h3').text(),
            service
              .findAll('[data-pool]')
              .map(pool => [
                pool.get('p').text(),
                pool
                  .findAll('[data-domain]')
                  .map(row => row.element.children[0]!.textContent!.trim()),
              ]),
          ]),
      ])
    expect(layout).toEqual([
      [
        'Public',
        [
          [
            'Service shop',
            [
              ['Pool eu', ['https://b.example.test']],
              ['No pool', ['https://a.example.test']],
            ],
          ],
          ['No service', [['No pool', ['https://c.example.test']]]],
        ],
      ],
      ['Internal', [['Service shop', [['No pool', ['d.red.int.example.test:8443']]]]]],
    ])
  })

  it('says a domain whose container is gone is not linked', async () => {
    const wrapper = shown({ domains: [domain(1, { container: null })], containers: [] })
    await flushPromises()

    expect(wrapper.get('[data-domain]').text()).toContain('not linked')
  })
})

describe('the domain dialog', () => {
  const containers = [linkable(1, 'w1', 'red'), linkable(2, 'w2', 'blue')]

  async function open(props: { domain?: Domain; domains?: Domain[] } = {}) {
    const inline = { template: '<div><slot /></div>' }
    const wrapper = mount(DomainDialog, {
      props: {
        containers,
        domains: props.domains ?? [],
        domain: props.domain,
        reload: async () => {},
      },
      global: {
        stubs: {
          Dialog: inline,
          DialogTrigger: inline,
          DialogContent: inline,
          DialogHeader: inline,
          DialogTitle: inline,
          DialogDescription: inline,
          DialogFooter: inline,
        },
      },
    })
    await wrapper
      .findAll('button')
      .find(button => /Add Domain|Edit/.test(button.text()))!
      .trigger('click')
    return wrapper
  }

  const vm = (wrapper: Awaited<ReturnType<typeof open>>) =>
    wrapper.vm as unknown as {
      chooseContainer: (value: string) => void
      type: string
      suffix: string
    }

  it("shows the name's shape for its type under the chosen container's worker", async () => {
    const wrapper = await open()
    const dialog = vm(wrapper)

    dialog.chooseContainer('1')
    dialog.type = 'internal'
    await flushPromises()
    expect(dialog.suffix).toBe('.red.int.example.test')
    dialog.type = 'tcp'
    expect(dialog.suffix).toBe('.red.int.example.test:8443')
    dialog.type = 'public'
    expect(dialog.suffix).toBe('.example.test')
  })

  it('clears the type when another container is chosen', async () => {
    const dialog = vm(await open())

    dialog.chooseContainer('1')
    dialog.type = 'internal'
    dialog.chooseContainer('2')

    expect(dialog.type).toBe('')
    dialog.type = 'internal'
    expect(dialog.suffix).toBe('.blue.int.example.test')
  })

  it('creates the domain on the chosen container', async () => {
    const wrapper = await open()
    vm(wrapper).chooseContainer('2')
    vm(wrapper).type = 'public'
    await flushPromises()
    await wrapper.get('#domain-name').setValue(' shop ')
    await wrapper.get('#domain-service').setValue('shop')
    await wrapper
      .findAll('button')
      .find(button => button.text() === 'Add')!
      .trigger('click')
    await flushPromises()

    expect(createDomain).toHaveBeenCalledWith({
      name: 'shop',
      type: 'public',
      port: 80,
      service: 'shop',
      pool: '',
      cors_origins: null,
      container: '2',
    })
  })

  it('opens with the domain it edits, and an unlinked one keeps its container empty', async () => {
    const wrapper = await open({ domain: domain(7, { container: null, port: 8080, pool: 'eu' }) })
    await wrapper.get('#domain-port').setValue('9090')
    await wrapper
      .findAll('button')
      .find(button => button.text() === 'Save')!
      .trigger('click')
    await flushPromises()

    expect(updateDomain).toHaveBeenCalledWith('7', {
      name: 'shop',
      type: 'public',
      port: 9090,
      service: '',
      pool: 'eu',
      cors_origins: null,
    })
  })

  it("offers the services of the chosen type's namespace", async () => {
    const domains = [
      domain(1, { service: 'shop' }),
      domain(2, { type: 'internal', service: 'api' }),
      domain(3, { type: 'tcp', service: 'db' }),
    ]
    const wrapper = await open({ domains })
    vm(wrapper).chooseContainer('1')
    vm(wrapper).type = 'internal'
    await flushPromises()

    expect(
      wrapper.findAll('#domain-services option').map(option => option.attributes('value'))
    ).toEqual(['api', 'db'])
  })

  it('keeps a blank origins field and describes the current origin after a rename', async () => {
    const wrapper = await open({ domain: domain(7, {}) })
    expect(wrapper.get('#domain-cors').element).toHaveProperty('value', '')
    expect(wrapper.text()).toContain('https://shop.example.test')

    await wrapper.get('#domain-name').setValue('frontend')

    expect(wrapper.get('#domain-cors').element).toHaveProperty('value', '')
    expect(wrapper.text()).toContain('https://frontend.example.test')
  })

  it('opens with explicit origins and submits one origin per nonempty line', async () => {
    const wrapper = await open({
      domain: domain(7, { cors_origins: ['https://app.example.test'] }),
    })
    expect(wrapper.get('#domain-cors').element).toHaveProperty('value', 'https://app.example.test')
    await wrapper
      .get('#domain-cors')
      .setValue('https://app.example.test\n\n http://localhost:5173 ')
    await wrapper
      .findAll('button')
      .find(button => button.text() === 'Save')!
      .trigger('click')
    await flushPromises()

    expect(updateDomain).toHaveBeenCalledWith(
      '7',
      expect.objectContaining({
        cors_origins: ['https://app.example.test', 'http://localhost:5173'],
      })
    )
  })

  it('clears an override to null without substituting the shown default', async () => {
    const wrapper = await open({
      domain: domain(7, { cors_origins: ['https://app.example.test'] }),
    })
    await wrapper.get('#domain-cors').setValue('  ')
    await wrapper
      .findAll('button')
      .find(button => button.text() === 'Save')!
      .trigger('click')
    await flushPromises()

    expect(updateDomain).toHaveBeenCalledWith('7', expect.objectContaining({ cors_origins: null }))
  })

  it('hides the HTTP policy for TCP and clears it on a protocol change', async () => {
    const wrapper = await open({
      domain: domain(7, { cors_origins: ['https://app.example.test'] }),
    })
    vm(wrapper).type = 'tcp'
    await flushPromises()
    expect(wrapper.find('#domain-cors').exists()).toBe(false)
    await wrapper
      .findAll('button')
      .find(button => button.text() === 'Save')!
      .trigger('click')
    await flushPromises()

    expect(updateDomain).toHaveBeenCalledWith(
      '7',
      expect.objectContaining({ type: 'tcp', cors_origins: null })
    )
  })

  it('retains the entered origins when the backend rejects them', async () => {
    vi.mocked(updateDomain).mockRejectedValueOnce(
      new Error('Invalid origin: use an HTTP(S) origin.')
    )
    const wrapper = await open({ domain: domain(7, {}) })
    await wrapper.get('#domain-cors').setValue('https://app.example.test/path')
    await wrapper
      .findAll('button')
      .find(button => button.text() === 'Save')!
      .trigger('click')
    await flushPromises()

    expect(wrapper.text()).toContain('Invalid origin: use an HTTP(S) origin.')
    expect(wrapper.get('#domain-cors').element).toHaveProperty(
      'value',
      'https://app.example.test/path'
    )
  })
})
