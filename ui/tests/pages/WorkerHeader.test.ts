import { flushPromises, mount } from '@vue/test-utils'
import StateIndicator from '@/components/StateIndicator.vue'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { formatDate } from '@/lib/utils'
import WorkerHeader from '@/pages/Worker/WorkerHeader.vue'
import {
  rebootWorker,
  resyncWorker,
  type AppInfo,
  type Worker,
  type WorkerCertificate,
  type WorkerMetricsResponse,
} from '@/services/api'
import { useAppStore } from '@/stores/app'
import { readyCertificate, worker as listed } from '../helpers/workers'

vi.mock('vue-router', () => ({ useRouter: () => ({ push: vi.fn() }) }))
vi.mock('vue-sonner', () => ({ toast: { success: vi.fn(), error: vi.fn() } }))
vi.mock('@/services/api', async importOriginal => ({
  ...(await importOriginal<typeof import('@/services/api')>()),
  rebootWorker: vi.fn(async () => ({ task_id: 't1' })),
  resyncWorker: vi.fn(async () => ({ task_id: 't1' })),
}))

const worker = (subdomain: string | null, overrides: Partial<Worker> = {}): Worker => ({
  ...listed('w1', 'online', { subdomain }),
  containers: [],
  removal_refusal: null,
  ...overrides,
})

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
  function mountStack(
    tunnelStatus: string | null,
    tunnel: Worker['tunnel'] = null,
    knowledge: Worker['report']['knowledge'] = 'known'
  ) {
    const store = useAppStore()
    store.info = {
      settings: {
        cloudflare: { details: { tunnel_name: 'sage-example.test', tunnel_status: tunnelStatus } },
      },
    } as unknown as AppInfo
    const current = worker('eu1', {
      tunnel,
      tunnel_name: 'sage-example.test-w1',
      report: {
        knowledge,
        traefik_reachable: knowledge === 'unobserved' ? null : true,
        stack_down: knowledge === 'unobserved' ? [] : ['cloudflared-discovery'],
        stack:
          knowledge === 'unobserved'
            ? []
            : [
                ['traefik', 'running'],
                ['vector', 'running'],
                ['cloudflared', 'running'],
                ['cloudflared-discovery', 'restarting'],
              ],
        containers: knowledge === 'unobserved' ? null : 0,
      },
    })
    return mount(WorkerHeader, {
      props: { worker: current, workerMetrics, loadWorkerStatus: async () => {} },
      global: { stubs: { EditWorkerButton: true, ConfirmationButton: true, CopyButton: true } },
    })
  }

  function stack(...args: Parameters<typeof mountStack>) {
    const wrapper = mountStack(...args)
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

  it('keeps every container with its last seen state while the report is not current', () => {
    const shown = stack('healthy', 'healthy', 'stale')
    expect(Object.keys(shown)).toEqual([
      'traefik',
      'vector',
      'cloudflared',
      'cloudflared-discovery',
    ])
    expect(shown.traefik).toEqual({ state: 'last seen running', value: undefined })
    expect(shown['cloudflared-discovery']).toEqual({
      state: 'last seen restarting',
      value: 'sage-example.test · healthy',
    })
  })

  it('says the worker has not reported yet before its first report', () => {
    const wrapper = mountStack(null, null, 'unobserved')
    expect(wrapper.findAll('[data-stack]')).toHaveLength(0)
    expect(wrapper.text()).toContain('Not reported yet.')
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

function certificates(
  own: WorkerCertificate,
  discovery: WorkerCertificate,
  report: Worker['report'] = worker('eu1').report
) {
  const current = worker('eu1', { certificates: { worker: own, discovery }, report })
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
    const { wrapper, field } = certificates(ready(ownExpiry), ready(discoveryExpiry), {
      ...worker('eu1').report,
      knowledge: 'stale',
    })
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
        role === 'worker' ? failed : readyCertificate,
        role === 'discovery' ? failed : readyCertificate
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
      worker: worker('eu1', {
        certificates: { worker: ready(ownExpiry), discovery: ready(discoveryExpiry) },
      }),
    })
    expect(field('worker').text()).toContain(`Valid until ${formatDate(ownExpiry)}`)
    expect(field('discovery').text()).toContain(`Valid until ${formatDate(discoveryExpiry)}`)
  })
})

describe('the worker', () => {
  const confirmation = {
    props: ['triggerText', 'onConfirm', 'disabled', 'body'],
    template:
      '<button :data-action="triggerText" :disabled="disabled" :data-body="body" @click="onConfirm()">{{ triggerText }}</button>',
  }

  function mountHeader(current: Worker) {
    const loadWorkerStatus = vi.fn(async () => {})
    const wrapper = mount(WorkerHeader, {
      props: { worker: current, workerMetrics, loadWorkerStatus },
      global: { stubs: { EditWorkerButton: true, ConfirmationButton: confirmation } },
    })
    return { wrapper, loadWorkerStatus }
  }

  it('disables Delete while the manager would refuse it, and says why', () => {
    const { wrapper } = mountHeader(
      worker('eu1', { removal_refusal: "A worker with containers can't be removed." })
    )

    expect(wrapper.get('[data-action="Delete"]').attributes('disabled')).toBeDefined()
    expect(wrapper.get('[data-removal-refusal]').text()).toBe(
      "A worker with containers can't be removed."
    )
  })

  it('leaves Delete enabled, with no refusal shown, for a worker the manager would remove', () => {
    const { wrapper } = mountHeader(worker('eu1'))

    expect(wrapper.get('[data-action="Delete"]').attributes('disabled')).toBeUndefined()
    expect(wrapper.find('[data-removal-refusal]').exists()).toBe(false)
  })

  it('says that an offline worker is removed from Sage only', () => {
    const { wrapper } = mountHeader(
      worker('eu1', {
        view: { status: 'offline', reason: 'ssh_unreachable', detail: null, ready: false },
      })
    )

    expect(wrapper.get('[data-action="Delete"]').attributes('data-body')).toContain(
      'it is removed from Sage only, and its VM has to be cleaned up by hand'
    )
  })

  it('labels its state by why it is not online, and says why in the tooltip', () => {
    const { wrapper } = mountHeader(
      worker('eu1', {
        view: { status: 'offline', reason: 'ssh_unreachable', detail: null, ready: false },
      })
    )
    const badge = wrapper
      .findAllComponents(StateIndicator)
      .find(indicator => indicator.props('tooltip'))!

    expect(badge.text()).toBe('SSH down')
    expect(badge.props('tooltip')).toBe('Its SSH server does not answer.')
  })

  it('shows its address in the tailnet when the graphs have none', () => {
    const wrapper = mount(WorkerHeader, {
      props: {
        worker: worker('eu1', { address: '100.64.0.9' }),
        workerMetrics: { meta: {} } as WorkerMetricsResponse,
        loadWorkerStatus: async () => {},
      },
      global: { stubs: { EditWorkerButton: true, ConfirmationButton: true } },
    })
    const field = wrapper.findAll('label').find(found => found.text() === 'IP')!

    expect(field.element.nextElementSibling!.textContent!.trim()).toBe('100.64.0.9')
  })

  it.each([
    ['Resync', resyncWorker],
    ['Restart', rebootWorker],
  ] as const)('%s asks for it, then loads the worker again', async (action, call) => {
    const { wrapper, loadWorkerStatus } = mountHeader(worker('eu1'))

    await wrapper.get(`[data-action="${action}"]`).trigger('click')
    await flushPromises()

    expect(call).toHaveBeenCalledWith('w1')
    expect(loadWorkerStatus).toHaveBeenCalledOnce()
  })
})
