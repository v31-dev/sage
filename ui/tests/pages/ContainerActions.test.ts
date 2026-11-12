import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { toast } from 'vue-sonner'
import DeleteContainerButton from '@/pages/Application/DeleteContainerButton.vue'
import DeployApplicationButton from '@/pages/Application/DeployApplicationButton.vue'
import StopApplicationButton from '@/pages/Application/StopApplicationButton.vue'
import VolumeBackupButton from '@/pages/Application/VolumeBackupButton.vue'
import {
  createVolumeBackup,
  deleteContainer,
  deployApplication,
  deployContainer,
  stopApplication,
  stopContainer,
  type Application,
  type Container,
  type Volume,
} from '@/services/api'
import { container } from '../helpers/containers'

vi.mock('vue-sonner', () => ({ toast: { success: vi.fn(), error: vi.fn() } }))
vi.mock('@/services/api', async importOriginal => ({
  ...(await importOriginal<typeof import('@/services/api')>()),
  deployApplication: vi.fn(async () => ({ task_ids: [] })),
  deployContainer: vi.fn(async () => ({ task_id: 't1' })),
  stopApplication: vi.fn(async () => ({ task_ids: [] })),
  stopContainer: vi.fn(async () => ({ task_id: 't1' })),
  deleteContainer: vi.fn(async () => ({ task_id: 't1' })),
  createVolumeBackup: vi.fn(async () => ({ task_ids: [] })),
}))

// The confirmation dialog, confirmed at a click on its trigger.
const confirmation = {
  props: ['triggerText', 'disabled', 'onConfirm'],
  template: '<button :disabled="disabled" @click="onConfirm()">{{ triggerText }}</button>',
}

const stopped = (id: string, overrides: Partial<Container> = {}) =>
  container({
    id,
    view: { headline: 'inactive', config: 'current', reason: null, detail: null },
    ...overrides,
  })

const application = (containers: Container[]) =>
  ({ name: 'web', project: { name: 'demo', label: 'Demo' }, containers }) as unknown as Application

function mountButton(component: unknown, props: Record<string, unknown>) {
  const loadApplication = vi.fn(async () => {})
  const wrapper = mount(component as never, {
    props: { loadApplication, ...props },
    global: { stubs: { ConfirmationButton: confirmation } },
  })
  return { wrapper, loadApplication }
}

beforeEach(() => {
  setActivePinia(createPinia())
  vi.clearAllMocks()
})

describe('Deploy', () => {
  it('of one container asks for its Deploy, then loads the page again', async () => {
    const one = stopped('c2')
    const { wrapper, loadApplication } = mountButton(DeployApplicationButton, {
      application: application([stopped('c1'), one]),
      container: one,
    })

    await wrapper.get('button').trigger('click')
    await flushPromises()

    expect(deployContainer).toHaveBeenCalledWith('demo', 'web', 'c2')
    expect(deployApplication).not.toHaveBeenCalled()
    expect(loadApplication).toHaveBeenCalledOnce()
  })

  it('of all asks for Deploy all', async () => {
    const { wrapper } = mountButton(DeployApplicationButton, {
      application: application([stopped('c1')]),
    })

    await wrapper.get('button').trigger('click')
    await flushPromises()

    expect(deployApplication).toHaveBeenCalledWith('demo', 'web')
  })

  it.each([
    ['no containers', [], true],
    ['every container refusing', [stopped('c1', { live: 'deploy' })], true],
    ['one container taking it', [stopped('c1', { live: 'deploy' }), stopped('c2')], false],
    ['an automatic backup running', [stopped('c1', { live: 'backup_preserve' })], false],
  ])('of all with %s is disabled: %s', (_name, containers, disabled) => {
    const { wrapper } = mountButton(DeployApplicationButton, {
      application: application(containers),
    })

    expect(wrapper.get('button').attributes('disabled') !== undefined).toBe(disabled)
  })

  it('that is refused says why and loads nothing', async () => {
    vi.mocked(deployContainer).mockRejectedValueOnce(new Error('This container is being deleted.'))
    const one = stopped('c1')
    const { wrapper, loadApplication } = mountButton(DeployApplicationButton, {
      application: application([one]),
      container: one,
    })

    await wrapper.get('button').trigger('click')
    await flushPromises()

    expect(toast.error).toHaveBeenCalledWith('This container is being deleted.')
    expect(loadApplication).not.toHaveBeenCalled()
    expect(wrapper.get('button').attributes('disabled')).toBeUndefined()
  })
})

describe('Stop', () => {
  it('of one container asks for its Stop, of all for Stop all, then loads the page again', async () => {
    const one = container({ id: 'c3' })
    const single = mountButton(StopApplicationButton, {
      application: application([one]),
      container: one,
    })
    const all = mountButton(StopApplicationButton, { application: application([one]) })

    await single.wrapper.get('button').trigger('click')
    await all.wrapper.get('button').trigger('click')
    await flushPromises()

    expect(stopContainer).toHaveBeenCalledWith('demo', 'web', 'c3')
    expect(stopApplication).toHaveBeenCalledOnce()
    expect([
      single.loadApplication.mock.calls.length,
      all.loadApplication.mock.calls.length,
    ]).toEqual([1, 1])
  })

  it('is disabled while the container refuses it', () => {
    const busy = container({ live: 'stop' })
    const { wrapper } = mountButton(StopApplicationButton, {
      application: application([busy]),
      container: busy,
    })

    expect(wrapper.get('button').attributes('disabled')).toBeDefined()
  })
})

describe('Delete', () => {
  it('asks for the Delete of the container by its id, then loads the page again', async () => {
    const one = container({ id: 'c4' })
    const { wrapper, loadApplication } = mountButton(DeleteContainerButton, {
      application: application([one]),
      container: one,
    })

    await wrapper.get('button').trigger('click')
    await flushPromises()

    expect(deleteContainer).toHaveBeenCalledWith('demo', 'web', 'c4')
    expect(loadApplication).toHaveBeenCalledOnce()
  })

  it.each([
    ['running', { live: 'delete' as const }],
    ['waiting', { queued: [{ operation: 'delete' as const, task_id: 't1', cancellable: true }] }],
  ])('is disabled while a Delete is %s', (_name, overrides) => {
    const one = container(overrides)
    const { wrapper } = mountButton(DeleteContainerButton, {
      application: application([one]),
      container: one,
    })

    expect(wrapper.get('button').attributes('disabled')).toBeDefined()
  })
})

describe('a volume backup', () => {
  const volume = { name: 'data' } as Volume

  it('asks for a backup of the volume, then loads the page again', async () => {
    const { wrapper, loadApplication } = mountButton(VolumeBackupButton, {
      application: application([stopped('c1'), stopped('c2')]),
      volume,
    })

    await wrapper.get('button').trigger('click')
    await flushPromises()

    expect(createVolumeBackup).toHaveBeenCalledWith('demo', 'web', 'data')
    expect(loadApplication).toHaveBeenCalledOnce()
  })

  it.each([
    ['no containers', []],
    ['a container running', [stopped('c1'), container({ id: 'c2' })]],
    [
      'a stopped container with its own operation running',
      [stopped('c1', { live: 'restore_stopped' })],
    ],
  ])('is disabled with %s', (_name, containers) => {
    const { wrapper } = mountButton(VolumeBackupButton, {
      application: application(containers),
      volume,
    })

    expect(wrapper.get('button').attributes('disabled')).toBeDefined()
  })
})
