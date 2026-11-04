import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import WorkerHeader from '@/pages/Worker/WorkerHeader.vue'
import type { Worker, WorkerMetricsResponse } from '@/services/api'

vi.mock('vue-router', () => ({ useRouter: () => ({ push: vi.fn() }) }))

const worker = (tag: string | null, pool: string | null) =>
  ({
    hostname: 'w1',
    ip: '100.64.0.2',
    tag,
    pool,
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

describe('the tag and the pool', () => {
  it('are those of the worker it is given', () => {
    expect(shown(worker('eu1', 'eu'), 'Tag')).toBe('eu1')
    expect(shown(worker('eu1', 'eu'), 'Pool')).toBe('eu')
  })

  it('are a dash for a worker that has none yet', () => {
    expect(shown(worker(null, null), 'Tag')).toBe('-')
    expect(shown(worker(null, null), 'Pool')).toBe('-')
  })
})
