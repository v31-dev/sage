import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it } from 'vitest'
import ContainerCard from '@/pages/Application/ContainerCard.vue'
import type { Container, WorkerListItem, WorkerStatus } from '@/services/api'
import { useAppStore } from '@/stores/app'

function listed(hostname: string, status: WorkerStatus): WorkerListItem {
  return {
    hostname,
    ip: '100.64.0.2',
    online: status !== 'offline',
    status,
    path: null,
    container_count: null,
    infra: null,
    metrics: null,
  }
}

function mountCard(hostname = 'w1') {
  const container = {
    worker: { hostname },
    status: 'active',
    domain_tag: null,
  } as unknown as Container
  return mount(ContainerCard, {
    props: { application: {} as never, container, loadApplication: async () => {} },
    global: {
      stubs: {
        EditContainerButton: true,
        DeleteContainerButton: true,
        EventLogsButton: true,
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
