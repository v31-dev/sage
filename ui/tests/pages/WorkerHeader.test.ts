import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { formatDate } from '@/lib/utils'
import WorkerHeader from '@/pages/Worker/WorkerHeader.vue'
import type { AppInfo, Worker, WorkerMetricsResponse } from '@/services/api'
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
  function stack(
    certificate: Worker['certificate'],
    tunnelStatus: string | null,
    tunnel: Worker['tunnel'] = null
  ) {
    const store = useAppStore()
    store.info = {
      settings: {
        cloudflare: { details: { tunnel_name: 'sage-example.test', tunnel_status: tunnelStatus } },
      },
    } as unknown as AppInfo
    const current = {
      ...worker('eu1'),
      certificate,
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

  const expiry = '2027-02-02T00:00:00+00:00'

  it('gives each container its state, and Traefik the certificate with its expiry only once it is ready', () => {
    expect(stack({ covered: true, synced: true, expiry }, null).traefik).toEqual({
      state: 'running',
      value: `certificate ready, valid until ${formatDate(expiry)}`,
    })
    expect(stack({ covered: true, synced: false, expiry: null }, null).traefik!.value).toBe(
      'syncing the certificate'
    )
    expect(stack({ covered: false, synced: true, expiry }, null).traefik!.value).toBe(
      'waiting for the certificate'
    )
    expect(stack(null, null).vector).toEqual({ state: 'running', value: undefined })
  })

  it("shows the worker's own tunnel beside cloudflared and the platform's beside cloudflared-discovery", () => {
    const shown = stack(null, 'degraded', 'down')
    expect(shown.cloudflared!.value).toBe('sage-example.test-w1 · down')
    expect(shown['cloudflared-discovery']).toEqual({
      state: 'restarting',
      value: 'sage-example.test · degraded',
    })
    expect(stack(null, null).cloudflared!.value).toBe('sage-example.test-w1')
  })
})
