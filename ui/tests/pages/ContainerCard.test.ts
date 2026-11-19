import { flushPromises, mount, RouterLinkStub } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it } from 'vitest'
import ContainerCard from '@/pages/Application/ContainerCard.vue'
import CopyButton from '@/components/CopyButton.vue'
import DomainCard from '@/components/DomainCard.vue'
import DomainDialog from '@/components/DomainDialog.vue'
import type { Application, Domain, DomainStatus, WorkerStatus } from '@/services/api'
import { container } from '../helpers/containers'
import { worker } from '../helpers/workers'
import { useAppStore } from '@/stores/app'

const listed = (hostname: string, status: WorkerStatus) => worker(hostname, status)

const application = { name: 'web', label: 'Web', project: { name: 'demo', label: 'Demo' } }

// A domain, with its state's `status` and `error` given apart.
const domain = ({
  status = 'synced',
  error = null,
  ...overrides
}: Partial<Domain> & { status?: DomainStatus; error?: string | null } = {}) =>
  ({
    id: 'd1',
    name: 'shop',
    type: 'public',
    port: 80,
    service: null,
    pool: null,
    cors_origins: null,
    host: 'shop.example.test',
    address: 'https://shop.example.test',
    worker: 'w1',
    container: { id: 'c1', worker: 'w1', application },
    view: {
      status,
      conforms: status === 'synced',
      reason: status === 'error' ? 'dns' : null,
      error,
    },
    ...overrides,
  }) as Domain

function mountCard(hostname = 'w1', domains: Domain[] = []) {
  return mount(ContainerCard, {
    props: {
      application: {} as Application,
      container: container({ worker: { hostname, subdomain: 'red' }, domains }),
      loadApplication: async () => {},
    },
    global: {
      stubs: {
        DeleteContainerButton: true,
        DeployApplicationButton: true,
        StopApplicationButton: true,
        EventLogsButton: true,
        DomainDialog: true,
        ConfirmationButton: true,
        CopyButton: true,
        RouterLink: RouterLinkStub,
      },
    },
  })
}

// The Worker row's value: the worker's link and its status.
const workerRow = (wrapper: ReturnType<typeof mountCard>) =>
  wrapper.get('[data-worker]').element.parentElement!

beforeEach(() => {
  setActivePinia(createPinia())
})

describe('the worker of the container', () => {
  it('links to the worker’s page', () => {
    expect(mountCard('w1').getComponent<typeof RouterLinkStub>('[data-worker]').props('to')).toBe(
      '/workers/w1'
    )
  })

  it.each([
    'online',
    'not ready',
    'offline',
    'checking',
    'rebooting',
    'adding',
    'removing',
  ] as WorkerStatus[])('says %s as the status the list has for the worker', status => {
    useAppStore().workers = [listed('w1', status), listed('w2', 'online')]

    expect(workerRow(mountCard('w1')).textContent).toBe(`w1 ${status}`.replace(/\s+/g, ' '))
  })

  it('shows no status before the list of workers has loaded or for a worker that is not in it', () => {
    const store = useAppStore()
    expect(workerRow(mountCard()).textContent!.trim()).toBe('w1')

    store.workers = [listed('w2', 'online')]
    expect(workerRow(mountCard('w1')).textContent!.trim()).toBe('w1')
  })

  it('follows the list when the manager pushes another', async () => {
    const store = useAppStore()
    store.workers = [listed('w1', 'online')]
    const wrapper = mountCard()
    expect(workerRow(wrapper).textContent).toContain('online')

    store.workers = [listed('w1', 'rebooting')]
    await flushPromises()

    expect(workerRow(wrapper).textContent).toContain('rebooting')
  })
})

describe('the domains of the container', () => {
  it('has a Domains heading whose Add is for this container, with no domains yet', () => {
    const wrapper = mountCard('w1')
    const domains = wrapper.get('[data-domains]')

    expect(domains.text()).toContain('Domains')
    expect(domains.getComponent(DomainDialog).props()).toMatchObject({
      container: { id: 'c1', worker: 'w1' },
      domain: undefined,
    })
    expect(wrapper.find('[data-domain]').exists()).toBe(false)
  })

  it('shows a card for each domain, on its container', () => {
    const wrapper = mountCard('w1', [
      domain(),
      domain({ id: 'd2', address: 'https://other.example.test' }),
    ])

    expect(wrapper.findAll('[data-address]').map(link => link.attributes('href'))).toEqual([
      'https://shop.example.test',
      'https://other.example.test',
    ])
    expect(wrapper.findAllComponents(DomainCard).map(card => card.props('inContainer'))).toEqual([
      true,
      true,
    ])
  })

  it('shows the public names first, then the internal and TCP ones', () => {
    const wrapper = mountCard('w1', [
      domain({ id: 'd1', type: 'internal', host: 'api.red.int.example.test' }),
      domain({ id: 'd2', host: 'b.example.test' }),
      domain({ id: 'd3', type: 'tcp', host: 'db.red.int.example.test' }),
      domain({ id: 'd4', host: 'c.example.test' }),
    ])

    expect(wrapper.findAll('[data-host]').map(host => host.text())).toEqual([
      'b.example.test',
      'c.example.test',
      'api.red.int.example.test',
      'db.red.int.example.test',
    ])
  })

  it('follows the container’s domain updates', async () => {
    const wrapper = mountCard('w1', [domain()])

    await wrapper.setProps({
      container: {
        ...wrapper.props('container'),
        domains: [domain({ id: 'd2', address: 'https://new.example.test', status: 'syncing' })],
      },
    })

    expect(wrapper.findAll('[data-address]').map(link => link.attributes('href'))).toEqual([
      'https://new.example.test',
    ])
    expect(wrapper.get('[data-domain]').text()).toContain('syncing')
  })

  it("shows the card's Delete as an icon", () => {
    const wrapper = mount(ContainerCard, {
      props: {
        application: {} as Application,
        container: container({ worker: { hostname: 'w1', subdomain: 'red' } }),
        loadApplication: async () => {},
      },
      global: {
        stubs: {
          DeployApplicationButton: true,
          StopApplicationButton: true,
          EventLogsButton: true,
          DomainDialog: true,
          RouterLink: RouterLinkStub,
        },
      },
    })

    expect(
      wrapper
        .get('[data-slot=card-action]')
        .findAll('button')
        .map(button => [button.text(), button.attributes('aria-label')])
    ).toEqual([['', 'Delete']])
  })

  it('links to the address, copies it, and shows its type, service and pool', () => {
    const wrapper = mountCard('w1', [domain({ service: 'shop', pool: 'eu' })])
    const card = wrapper.get('[data-domain]')

    expect(card.get('[data-address]').attributes('href')).toBe('https://shop.example.test')
    expect(card.getComponent(CopyButton).props('text')).toBe('https://shop.example.test')
    expect(card.text()).toContain('synced')
    expect(card.text()).toContain('Public')
    expect(card.get('[data-service]').text()).toBe('shop')
    expect(card.get('[data-pool]').text()).toBe('eu')
  })

  it('shows a TCP address without an HTTP link', () => {
    const wrapper = mountCard('w1', [
      domain({
        type: 'tcp',
        host: 'db.red.int.example.test',
        address: 'db.red.int.example.test:8443',
      }),
    ])
    const card = wrapper.get('[data-domain]')

    expect(card.get('[data-address]').element.tagName).toBe('SPAN')
    expect(card.text()).toContain('db.red.int.example.test:8443')
    expect(card.text()).toContain('TCP')
  })

  it.each([
    ['pending', 'pending'],
    ['syncing', 'syncing'],
    ['synced', 'synced'],
    ['error', 'sync failed'],
  ] as const)(
    'shows the record sync state %s separately from container health',
    (status, label) => {
      const wrapper = mountCard('w1', [
        domain({ status, error: status === 'error' ? 'DNS: unavailable' : null }),
      ])

      expect(wrapper.text()).toContain('active')
      expect(wrapper.get('[data-domain]').text()).toContain(label)
      if (status === 'error')
        expect(wrapper.get('[data-domain] [data-reason]').text()).toBe(
          'Its DNS records could not be applied: DNS: unavailable'
        )
    }
  )

  it.each([
    [{ covered: false, synced: false, expiry: null, error: null }, 'waiting'],
    [{ covered: true, synced: false, expiry: null, error: null }, 'syncing'],
    [{ covered: true, synced: true, expiry: null, error: null }, 'ready'],
  ])('shows an internal name’s worker certificate: %j', (certificate, shown) => {
    useAppStore().workers = [
      worker('w1', 'online', { certificates: { worker: certificate, discovery: certificate } }),
    ]
    const internal = domain({ type: 'internal', address: 'https://shop.red.int.example.test' })

    expect(mountCard('w1', [internal]).get('[data-certificate]').text()).toBe(shown)
    expect(mountCard('w1', [domain()]).find('[data-certificate]').exists()).toBe(false)
  })
})
