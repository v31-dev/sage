import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { formatDate } from '@/lib/utils'
import WorkerHeader from '@/pages/Worker/WorkerHeader.vue'
import type { AppInfo, Worker, WorkerCertificate, WorkerMetricsResponse } from '@/services/api'
import { useAppStore } from '@/stores/app'

vi.mock('vue-router', () => ({ useRouter: () => ({ push: vi.fn() }) }))

const worker = (subdomain: string | null) =>
  ({
    hostname: 'w1',
    ip: '100.64.0.2',
    subdomain,
    status: 'online',
    path: null,
    infra: null,
    containers: [],
    certificate: null,
    discovery_certificate: null,
  }) as unknown as Worker

const workerMetrics = { meta: { ip: '100.64.0.2' } } as unknown as WorkerMetricsResponse

function shown(current: Worker, label: string) {
  const wrapper = mount(WorkerHeader, {
    props: { worker: current, workerMetrics, loadWorkerStatus: async () => {} },
    global: { stubs: { EditWorkerButton: true, ConfirmationButton: true, StateIndicator: true } },
  })
  const field = wrapper.findAll('label').find(found => found.text() === label)!
  return field.element.nextElementSibling!.textContent!.trim()
}

beforeEach(() => {
  setActivePinia(createPinia())
})

describe('the subdomain', () => {
  it('is that of the worker it is given', () => {
    expect(shown(worker('eu1'), 'Subdomain')).toBe('eu1')
  })

  it('is a dash for a worker that has none yet', () => {
    expect(shown(worker(null), 'Subdomain')).toBe('-')
  })
})

describe('the stack', () => {
  function stack(tunnelStatus: string | null, tunnel: Worker['tunnel'] = null) {
    const store = useAppStore()
    store.info = {
      settings: {
        cloudflare: { details: { tunnel_name: 'sage-example.test', tunnel_status: tunnelStatus } },
      },
    } as unknown as AppInfo
    const current = {
      ...worker('eu1'),
      tunnel,
      tunnel_name: 'sage-example.test-w1',
      infra: [
        { name: 'traefik', status: 'running' },
        { name: 'vector', status: 'running' },
        { name: 'cloudflared', status: 'running' },
        { name: 'cloudflared-discovery', status: 'restarting' },
      ],
    } as Worker
    const wrapper = mount(WorkerHeader, {
      props: { worker: current, workerMetrics, loadWorkerStatus: async () => {} },
      global: { stubs: { EditWorkerButton: true, ConfirmationButton: true, CopyButton: true } },
    })
    return Object.fromEntries(
      wrapper.findAll('[data-stack]').map(field => [
        field.get('label').text(),
        {
          state: field.get('[data-slot=badge]').text(),
          value: field.find('input').exists()
            ? (field.get('input').element as HTMLInputElement).value
            : undefined,
        },
      ])
    )
  }

  it('gives each container its reported state', () => {
    expect(stack(null).traefik).toEqual({ state: 'running', value: undefined })
    expect(stack(null).vector).toEqual({ state: 'running', value: undefined })
  })

  it("shows the worker's own tunnel beside cloudflared and the platform's beside cloudflared-discovery", () => {
    const shown = stack('degraded', 'down')
    expect(shown.cloudflared!.value).toBe('sage-example.test-w1 · down')
    expect(shown['cloudflared-discovery']).toEqual({
      state: 'restarting',
      value: 'sage-example.test · degraded',
    })
    expect(stack(null).cloudflared!.value).toBe('sage-example.test-w1')
  })
})

const ownExpiry = '2027-02-02T00:00:00+00:00'
const discoveryExpiry = '2027-03-03T00:00:00+00:00'
const ready = (expiry: string): WorkerCertificate => ({
  covered: true,
  synced: true,
  expiry,
  error: null,
})

function certificates(own: WorkerCertificate | null, discovery: WorkerCertificate | null) {
  const current = { ...worker('eu1'), certificate: own, discovery_certificate: discovery }
  const wrapper = mount(WorkerHeader, {
    props: { worker: current, workerMetrics, loadWorkerStatus: async () => {} },
    global: { stubs: { EditWorkerButton: true, ConfirmationButton: true } },
  })
  return { wrapper, field: (role: string) => wrapper.get(`[data-certificate="${role}"]`) }
}

describe('certificates', () => {
  it('shows separate applied expiries even without a current stack report', () => {
    useAppStore().info = {
      settings: {
        cloudflare: {
          details: {
            certificates: [
              { id: 'worker:w1', expiry: '2050-01-01T00:00:00+00:00' },
              { id: 'discovery', expiry: '2051-01-01T00:00:00+00:00' },
            ],
          },
        },
      },
    } as unknown as AppInfo
    const { wrapper, field } = certificates(ready(ownExpiry), ready(discoveryExpiry))
    expect(wrapper.text()).toContain('No current report from the worker.')
    expect(field('worker').text()).toContain(`Valid until ${formatDate(ownExpiry)}`)
    expect(field('worker').text()).not.toContain(formatDate(discoveryExpiry))
    expect(field('discovery').text()).toContain(`Valid until ${formatDate(discoveryExpiry)}`)
    expect(field('discovery').text()).not.toContain(formatDate(ownExpiry))
    expect(wrapper.text()).not.toContain('2050')
    expect(wrapper.text()).not.toContain('2051')
  })

  it.each(['worker', 'discovery'])(
    'keeps %s errors and stale expiry out of the other certificate',
    role => {
      const failed = { covered: false, synced: true, expiry: ownExpiry, error: 'CA unavailable' }
      const { field } = certificates(
        role === 'worker' ? failed : ready(ownExpiry),
        role === 'discovery' ? failed : ready(discoveryExpiry)
      )
      const other = role === 'worker' ? 'discovery' : 'worker'
      expect(field(role).text()).toContain('certificate update failed')
      expect(field(role).text()).toContain('CA unavailable')
      expect(field(role).text()).not.toContain('Valid until')
      expect(field(other).text()).toContain('certificate ready')
      expect(field(other).text()).not.toContain('CA unavailable')
    }
  )

  it.each(['worker', 'discovery'])(
    'shows a still-valid %s certificate alongside its failed update',
    role => {
      const failed = { ...ready(ownExpiry), error: 'renewal rejected' }
      const { field } = certificates(
        role === 'worker' ? failed : null,
        role === 'discovery' ? failed : null
      )
      expect(field(role).text()).toContain('certificate ready')
      expect(field(role).text()).toContain('renewal rejected')
      expect(field(role).text()).toContain(`Valid until ${formatDate(ownExpiry)}`)
    }
  )

  it('shows pending and syncing without an expiry until each applied state arrives', async () => {
    const { wrapper, field } = certificates(
      { covered: true, synced: false, expiry: ownExpiry, error: null },
      { covered: false, synced: false, expiry: discoveryExpiry, error: null }
    )
    expect(field('worker').text()).toContain('syncing the certificate')
    expect(field('discovery').text()).toContain('waiting for the certificate')
    expect(wrapper.text()).not.toContain('Valid until')
    await wrapper.setProps({
      worker: {
        ...worker('eu1'),
        certificate: ready(ownExpiry),
        discovery_certificate: ready(discoveryExpiry),
      },
    })
    expect(field('worker').text()).toContain(`Valid until ${formatDate(ownExpiry)}`)
    expect(field('discovery').text()).toContain(`Valid until ${formatDate(discoveryExpiry)}`)
  })

  it('leaves unknown certificate state explicit', () => {
    const { field } = certificates(null, null)
    expect(field('worker').text()).toContain('No certificate status yet.')
    expect(field('discovery').text()).toContain('No certificate status yet.')
  })
})
