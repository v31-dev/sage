import { flushPromises, mount, RouterLinkStub } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import DomainCard from '@/components/DomainCard.vue'
import { deleteDomain, type Domain, type WorkerListItem } from '@/services/api'
import { useAppStore } from '@/stores/app'

vi.mock('vue-sonner', () => ({ toast: { success: vi.fn(), error: vi.fn() } }))
vi.mock('@/services/api', async importOriginal => ({
  ...(await importOriginal<typeof import('@/services/api')>()),
  deleteDomain: vi.fn(async () => ({})),
}))

const application = { name: 'web', label: 'Web', project: { name: 'demo', label: 'Demo' } }

const domain = (overrides: Partial<Domain> = {}): Domain => ({
  id: '7',
  name: 'shop',
  type: 'public',
  port: 80,
  service: 'shop',
  pool: 'eu',
  cors_origins: null,
  host: 'shop.example.test',
  address: 'https://shop.example.test',
  worker: 'w1',
  container: { id: '1', worker: 'w1', application },
  callers: [],
  view: { status: 'synced', conforms: true, reason: null, error: null },
  created_at: '2026-01-01T00:00:00',
  updated_at: '2026-01-01T00:00:00',
  ...overrides,
})

const certificate = { covered: true, synced: true, expiry: null, error: null }

function worker(status: string, worker = certificate): WorkerListItem {
  return {
    hostname: 'w1',
    subdomain: 'red',
    view: { status },
    certificates: { worker, discovery: certificate },
  } as unknown as WorkerListItem
}

function card(props: { domain: Domain; inContainer?: boolean; reload?: () => Promise<void> }) {
  return mount(DomainCard, {
    props: { reload: async () => {}, ...props },
    global: {
      stubs: {
        DomainDialog: true,
        CopyButton: true,
        RouterLink: RouterLinkStub,
        ConfirmationButton: {
          props: ['onConfirm'],
          template: '<button data-delete @click="onConfirm()" />',
        },
      },
    },
  })
}

const rows = (wrapper: ReturnType<typeof card>) => wrapper.findAll('dt').map(term => term.text())

beforeEach(() => {
  setActivePinia(createPinia())
  useAppStore().workers = [worker('online')]
  vi.mocked(deleteDomain).mockClear()
})

describe('a domain card', () => {
  it('on its container names its type and service and leaves out the application and worker', () => {
    const wrapper = card({ domain: domain(), inContainer: true })

    expect(rows(wrapper)).toEqual(['Type', 'Service', 'Pool', 'Port'])
    expect(wrapper.get('[data-service]').text()).toBe('shop')
    expect(wrapper.get('[data-type]').text()).toBe('Public')
  })

  it('on the Domains page links its application and worker, with the worker’s status', () => {
    const wrapper = card({ domain: domain() })

    expect(rows(wrapper)).toEqual(['Pool', 'Application', 'Port', 'Worker'])
    expect(wrapper.get('[data-port]').text()).toBe('80')
    expect(wrapper.getComponent<typeof RouterLinkStub>('[data-worker]').props('to')).toBe(
      '/workers/w1'
    )
    expect(wrapper.get('[data-worker]').element.parentElement!.textContent).toContain('online')
    expect(wrapper.find('[data-type]').exists()).toBe(false)
  })

  it.each([
    ['ready', certificate, 'ready'],
    ['waiting', { ...certificate, covered: false }, 'waiting'],
    ['failed', { ...certificate, synced: false, error: 'rate limited' }, 'update failed'],
  ])("shows an internal name's worker certificate when %s", (_, state, label) => {
    useAppStore().workers = [worker('online', state)]
    const wrapper = card({ domain: domain({ type: 'internal' }), inContainer: true })

    expect(wrapper.get('[data-certificate]').text()).toBe(label)
  })

  it('names the applications allowed to call an internal name, and has no row without any', () => {
    const callers = [
      { id: 3, name: 'api', label: 'API', project: { name: 'alpha', label: 'Alpha' } },
      { id: 1, ...application },
    ]
    const wrapper = card({ domain: domain({ type: 'internal', callers }), inContainer: true })
    expect(wrapper.get('[data-callers]').text()).toBe('Alpha / API, Demo / Web')
    expect(card({ domain: domain(), inContainer: true }).find('[data-callers]').exists()).toBe(
      false
    )
  })

  it('shows no certificate for a public name', () => {
    expect(card({ domain: domain() }).find('[data-certificate]').exists()).toBe(false)
  })

  it('deletes the domain and reloads its page', async () => {
    const reload = vi.fn(async () => {})
    const wrapper = card({ domain: domain(), reload })
    await wrapper.get('[data-delete]').trigger('click')
    await flushPromises()

    expect(deleteDomain).toHaveBeenCalledWith('7')
    expect(reload).toHaveBeenCalled()
  })
})
