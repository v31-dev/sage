import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import AddContainerButton from '@/pages/Application/AddContainerButton.vue'
import type { Application, WorkerListItem } from '@/services/api'
import { useAppStore } from '@/stores/app'

vi.mock('vue-sonner', () => ({ toast: { success: vi.fn(), error: vi.fn() } }))

const inline = { template: '<div><slot /></div>' }
const select = {
  props: ['modelValue', 'disabled'],
  emits: ['update:modelValue'],
  template: '<div data-select :data-value="modelValue"><slot /></div>',
}
const stubs = {
  Dialog: inline,
  DialogTrigger: inline,
  DialogContent: inline,
  DialogHeader: inline,
  DialogTitle: inline,
  DialogDescription: inline,
  DialogFooter: inline,
  SelectTrigger: inline,
  SelectContent: inline,
  Select: select,
  SelectItem: { props: ['value'], template: '<div data-option>{{ value }}</div>' },
  SelectValue: { props: ['placeholder'], template: '<span>{{ placeholder }}</span>' },
}

const worker = (hostname: string) => ({ hostname, status: 'online' }) as WorkerListItem

const application = (hostnames: string[]) =>
  ({
    name: 'web',
    containers: hostnames.map(hostname => ({ worker: { hostname } })),
  }) as unknown as Application

function setup(workers: WorkerListItem[] | null, onWorkers: string[] = []) {
  const containersAPI = { create: vi.fn(async () => ({})) }
  const loadApplication = vi.fn(async () => {})
  const wrapper = mount(AddContainerButton, {
    props: { application: application(onWorkers), containersAPI, loadApplication } as never,
    global: { stubs },
  })
  useAppStore().workers = workers
  return { wrapper, containersAPI, loadApplication }
}

async function open(wrapper: ReturnType<typeof setup>['wrapper']) {
  await wrapper
    .findAll('button')
    .find(button => button.text().includes('New Container'))!
    .trigger('click')
  await flushPromises()
}

const offered = (wrapper: ReturnType<typeof setup>['wrapper']) =>
  wrapper.findAll('[data-option]').map(option => option.text())
const chosen = (wrapper: ReturnType<typeof setup>['wrapper']) =>
  wrapper.get('[data-select]').attributes('data-value')

beforeEach(() => {
  setActivePinia(createPinia())
})

describe('the workers it offers', () => {
  it('are the workers of the app store that hold no container of the application, the first chosen', async () => {
    const { wrapper } = setup([worker('w1'), worker('w2'), worker('w3')], ['w2'])

    await open(wrapper)

    expect(offered(wrapper)).toEqual(['w1', 'w3'])
    expect(chosen(wrapper)).toBe('w1')
  })

  it('are none when every worker holds one, or before the list has loaded', async () => {
    const full = setup([worker('w1')], ['w1'])
    await open(full.wrapper)
    expect(offered(full.wrapper)).toEqual([])
    expect(full.wrapper.text()).toContain('No workers available')

    const before = setup(null)
    await open(before.wrapper)
    expect(offered(before.wrapper)).toEqual([])
  })

  it('says so when the app store could not load the workers', async () => {
    const { wrapper } = setup(null)
    useAppStore().workersError = 'Failed to fetch workers'

    await open(wrapper)

    expect(wrapper.text()).toContain('Failed to load available workers')
  })
})

describe('adding', () => {
  it('adds a container on the chosen worker and loads the application again', async () => {
    const { wrapper, containersAPI, loadApplication } = setup([worker('w1'), worker('w3')])
    await open(wrapper)
    wrapper.findComponent(select).vm.$emit('update:modelValue', 'w3')

    await wrapper
      .findAll('button')
      .find(button => button.text() === 'Add')!
      .trigger('click')
    await flushPromises()

    expect(containersAPI.create).toHaveBeenCalledWith({ worker: 'w3' })
    expect(loadApplication).toHaveBeenCalledOnce()
  })
})
