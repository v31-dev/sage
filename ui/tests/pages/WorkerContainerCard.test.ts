import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import WorkerContainerCard from '@/pages/Worker/WorkerContainerCard.vue'
import type { Worker, WorkerStatus } from '@/services/api'
import { container } from '../helpers/containers'
import { worker } from '../helpers/workers'

function mountCard(status: WorkerStatus) {
  return mount(WorkerContainerCard, {
    props: {
      worker: worker('w1', status) as unknown as Worker,
      container: container(),
      loadWorkerStatus: async () => {},
    },
    global: { stubs: { ContainerStatus: true, RouterLink: { template: '<a><slot /></a>' } } },
  })
}

const actions = (wrapper: ReturnType<typeof mountCard>) =>
  wrapper
    .find('[data-slot=card-action]')
    .findAll('button')
    .map(button => [button.text(), button.attributes('aria-label')])

describe("the worker page's container card", () => {
  it('offers Force Delete as an icon while the worker is offline', () => {
    expect(actions(mountCard('offline'))).toEqual([['', 'Force Delete']])
  })

  it('offers no Force Delete while the worker is online', () => {
    expect(mountCard('online').find('[data-slot=card-action]').exists()).toBe(false)
  })
})
