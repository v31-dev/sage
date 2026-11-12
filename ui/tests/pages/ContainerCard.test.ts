import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it } from 'vitest'
import ContainerCard from '@/pages/Application/ContainerCard.vue'
import type { Application, Domain, DomainStatus, WorkerStatus } from '@/services/api'
import { container } from '../helpers/containers'
import { worker } from '../helpers/workers'
import { useAppStore } from '@/stores/app'

const listed = (hostname: string, status: WorkerStatus) => worker(hostname, status)

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
    container: { id: 'c1', worker: 'w1', application: {} },
    view: { status, conforms: status === 'synced', reason: null, error },
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
        CopyButton: true,
        RouterLink: { template: '<a><slot /></a>' },
      },
    },
  })
}

const dot = (wrapper: ReturnType<typeof mountCard>) =>
  wrapper.find('[role="img"][aria-label^="Worker "]')

beforeEach(() => {
  setActivePinia(createPinia())
})

describe('the dot of the worker', () => {
  it.each([
    ['online', 'Worker online'],
    ['not ready', 'Worker not ready'],
    ['offline', 'Worker offline'],
    ['checking', 'Worker checking'],
    ['rebooting', 'Worker rebooting'],
    ['adding', 'Worker adding'],
    ['removing', 'Worker removing'],
  ] as [WorkerStatus, string][])(
    'says %s as the status the list has for the worker',
    (status, label) => {
      useAppStore().workers = [listed('w1', status), listed('w2', 'online')]

      expect(dot(mountCard('w1')).attributes('aria-label')).toBe(label)
    }
  )

  it('is not there before the list of workers has loaded or for a worker that is not in it', () => {
    const store = useAppStore()
    expect(dot(mountCard()).exists()).toBe(false)

    store.workers = [listed('w2', 'online')]
    expect(dot(mountCard('w1')).exists()).toBe(false)
  })

  it('follows the list when the manager pushes another', async () => {
    const store = useAppStore()
    store.workers = [listed('w1', 'online')]
    const wrapper = mountCard()
    expect(dot(wrapper).attributes('aria-label')).toBe('Worker online')

    store.workers = [listed('w1', 'rebooting')]
    await flushPromises()

    expect(dot(wrapper).attributes('aria-label')).toBe('Worker rebooting')
  })
})

describe('the domains linked to the container', () => {
  it('shows several names linked to the container', () => {
    const wrapper = mountCard('w1', [
      domain(),
      domain({ id: 'd2', address: 'https://other.example.test' }),
    ])

    expect(wrapper.findAll('[data-domain] a').map(link => link.attributes('href'))).toEqual([
      'https://shop.example.test',
      'https://other.example.test',
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

    expect(wrapper.findAll('[data-domain] a').map(link => link.attributes('href'))).toEqual([
      'https://new.example.test',
    ])
    expect(wrapper.get('[data-domain]').text()).toContain('syncing')
  })

  it('links to the address and shows its type, service and pool', () => {
    const wrapper = mountCard('w1', [domain({ service: 'shop', pool: 'eu' })])
    const row = wrapper.get('[data-domain]')

    expect(row.get('a').attributes('href')).toBe('https://shop.example.test')
    expect(row.text()).toContain('synced')
    expect(row.text()).toContain('Public')
    expect(row.text()).toContain('Service shop')
    expect(row.text()).toContain('Pool eu')
  })

  it('shows a TCP address without an HTTP link', () => {
    const wrapper = mountCard('w1', [
      domain({
        type: 'tcp',
        host: 'db.red.int.example.test',
        address: 'db.red.int.example.test:8443',
      }),
    ])
    const row = wrapper.get('[data-domain]')

    expect(row.find('a').exists()).toBe(false)
    expect(row.text()).toContain('db.red.int.example.test:8443')
    expect(row.text()).toContain('TCP')
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
        expect(wrapper.get('[data-domain] [title]').attributes('title')).toBe('DNS: unavailable')
    }
  )

  it.each([
    [{ covered: false, synced: false, expiry: null, error: null }, 'waiting for the certificate'],
    [{ covered: true, synced: false, expiry: null, error: null }, 'syncing the certificate'],
  ])('shows an internal name’s worker certificate until it is ready', (certificate, shown) => {
    useAppStore().workers = [
      worker('w1', 'online', { certificates: { worker: certificate, discovery: certificate } }),
    ]
    const internal = domain({ type: 'internal', address: 'https://shop.red.int.example.test' })

    expect(mountCard('w1', [internal]).text()).toContain(shown)
    expect(mountCard('w1', [domain()]).text()).not.toContain(shown)
    useAppStore().workers = [listed('w1', 'online')]
    expect(mountCard('w1', [internal]).text()).not.toContain('certificate')
  })
})
