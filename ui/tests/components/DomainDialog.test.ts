import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import DomainDialog from '@/components/DomainDialog.vue'
import {
  createDomain,
  updateDomain,
  type AppInfo,
  type Domain,
  type WorkerListItem,
} from '@/services/api'
import { useAppStore } from '@/stores/app'

vi.mock('vue-sonner', () => ({ toast: { success: vi.fn(), error: vi.fn() } }))
vi.mock('@/services/api', async importOriginal => ({
  ...(await importOriginal<typeof import('@/services/api')>()),
  createDomain: vi.fn(async () => ({})),
  updateDomain: vi.fn(async () => ({})),
}))

const application = { name: 'web', label: 'Web', project: { name: 'demo', label: 'Demo' } }

const domain = (overrides: Partial<Domain> = {}): Domain => ({
  id: '7',
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
  view: { status: 'synced', conforms: true, reason: null, error: null },
  created_at: '2026-01-01T00:00:00',
  updated_at: '2026-01-01T00:00:00',
  ...overrides,
})

beforeEach(() => {
  setActivePinia(createPinia())
  const store = useAppStore()
  store.info = { domain: 'example.test' } as AppInfo
  store.workers = [
    { hostname: 'w1', subdomain: 'red' },
    { hostname: 'w2', subdomain: 'blue' },
  ] as WorkerListItem[]
  vi.mocked(createDomain).mockClear()
  vi.mocked(updateDomain).mockClear()
})

describe('the domain dialog', () => {
  async function open(props: { container?: { id: string; worker: string }; domain?: Domain }) {
    const inline = { template: '<div><slot /></div>' }
    const wrapper = mount(DomainDialog, {
      props: {
        container: props.container ?? props.domain!.container,
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
      .find(button => ['Add domain', 'Edit'].includes(button.attributes('aria-label') ?? ''))!
      .trigger('click')
    return wrapper
  }

  const vm = (wrapper: Awaited<ReturnType<typeof open>>) =>
    wrapper.vm as unknown as { type: string; suffix: string }

  async function save(wrapper: Awaited<ReturnType<typeof open>>, label: string) {
    await wrapper
      .findAll('button')
      .find(button => button.text() === label)!
      .trigger('click')
    await flushPromises()
  }

  it("shows the name's shape for its type under its container's worker", async () => {
    const dialog = vm(await open({ container: { id: '2', worker: 'w2' } }))

    dialog.type = 'internal'
    expect(dialog.suffix).toBe('.blue.int.example.test')
    dialog.type = 'tcp'
    expect(dialog.suffix).toBe('.blue.int.example.test:8443')
    dialog.type = 'public'
    expect(dialog.suffix).toBe('.example.test')
  })

  it('adds the domain to its container, with no container to choose', async () => {
    const wrapper = await open({ container: { id: '2', worker: 'w2' } })
    expect(wrapper.find('#domain-container').exists()).toBe(false)
    vm(wrapper).type = 'public'
    await flushPromises()
    await wrapper.get('#domain-name').setValue(' shop ')
    await wrapper.get('#domain-service').setValue('shop')
    await save(wrapper, 'Add')

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

  it('opens with the domain it edits and saves it without its container', async () => {
    const wrapper = await open({ domain: domain({ port: 8080, pool: 'eu' }) })
    await wrapper.get('#domain-port').setValue('9090')
    await save(wrapper, 'Save')

    expect(updateDomain).toHaveBeenCalledWith('7', {
      name: 'shop',
      type: 'public',
      port: 9090,
      service: '',
      pool: 'eu',
      cors_origins: null,
    })
  })

  it('keeps a blank origins field and describes the current origin after a rename', async () => {
    const wrapper = await open({ domain: domain() })
    expect(wrapper.get('#domain-cors').element).toHaveProperty('value', '')
    expect(wrapper.text()).toContain('https://shop.example.test')

    await wrapper.get('#domain-name').setValue('frontend')

    expect(wrapper.get('#domain-cors').element).toHaveProperty('value', '')
    expect(wrapper.text()).toContain('https://frontend.example.test')
  })

  it('opens with explicit origins and submits one origin per nonempty line', async () => {
    const wrapper = await open({ domain: domain({ cors_origins: ['https://app.example.test'] }) })
    expect(wrapper.get('#domain-cors').element).toHaveProperty('value', 'https://app.example.test')
    await wrapper
      .get('#domain-cors')
      .setValue('https://app.example.test\n\n http://localhost:5173 ')
    await save(wrapper, 'Save')

    expect(updateDomain).toHaveBeenCalledWith(
      '7',
      expect.objectContaining({
        cors_origins: ['https://app.example.test', 'http://localhost:5173'],
      })
    )
  })

  it('clears an override to null without substituting the shown default', async () => {
    const wrapper = await open({ domain: domain({ cors_origins: ['https://app.example.test'] }) })
    await wrapper.get('#domain-cors').setValue('  ')
    await save(wrapper, 'Save')

    expect(updateDomain).toHaveBeenCalledWith('7', expect.objectContaining({ cors_origins: null }))
  })

  it('hides the HTTP policy for TCP and clears it on a protocol change', async () => {
    const wrapper = await open({ domain: domain({ cors_origins: ['https://app.example.test'] }) })
    vm(wrapper).type = 'tcp'
    await flushPromises()
    expect(wrapper.find('#domain-cors').exists()).toBe(false)
    await save(wrapper, 'Save')

    expect(updateDomain).toHaveBeenCalledWith(
      '7',
      expect.objectContaining({ type: 'tcp', cors_origins: null })
    )
  })

  it('retains the entered origins when the backend rejects them', async () => {
    vi.mocked(updateDomain).mockRejectedValueOnce(
      new Error('Invalid origin: use an HTTP(S) origin.')
    )
    const wrapper = await open({ domain: domain() })
    await wrapper.get('#domain-cors').setValue('https://app.example.test/path')
    await save(wrapper, 'Save')

    expect(wrapper.text()).toContain('Invalid origin: use an HTTP(S) origin.')
    expect(wrapper.get('#domain-cors').element).toHaveProperty(
      'value',
      'https://app.example.test/path'
    )
  })
})
