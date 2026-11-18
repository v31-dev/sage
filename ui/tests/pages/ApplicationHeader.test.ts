import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, expect, it, vi } from 'vitest'
import ApplicationHeader from '@/pages/Application/ApplicationHeader.vue'
import { type Application } from '@/services/api'

vi.mock('vue-router', () => ({ useRouter: () => ({ push: vi.fn() }) }))
vi.mock('vue-sonner', () => ({ toast: { success: vi.fn(), error: vi.fn() } }))

// The fields the header shows; the rest of an application is left out.
const application = (containerCount: number) =>
  ({
    name: 'web',
    label: 'Web',
    type: 'docker',
    image: 'nginx',
    container_count: containerCount,
    active_count: 0,
    project: { name: 'demo', label: 'Demo' },
    containers: [],
    volumes: [],
  }) as unknown as Application

function deleteButton(containerCount: number) {
  const wrapper = mount(ApplicationHeader, {
    props: { application: application(containerCount), loadApplication: async () => {} },
    global: {
      stubs: {
        DeployApplicationButton: true,
        StopApplicationButton: true,
        EditApplicationButton: true,
      },
    },
  })
  return wrapper.findAll('button').find(button => button.attributes('aria-label') === 'Delete')!
}

beforeEach(() => setActivePinia(createPinia()))

it('allows deleting only an application with no containers', () => {
  expect(deleteButton(1).attributes('disabled')).toBeDefined()
  expect(deleteButton(0).attributes('disabled')).toBeUndefined()
})

it("shows the header's Edit and Delete as icons", () => {
  const wrapper = mount(ApplicationHeader, {
    props: { application: application(0), loadApplication: async () => {} },
    global: { stubs: { DeployApplicationButton: true, StopApplicationButton: true } },
  })

  expect(
    wrapper
      .get('[data-slot=card-action]')
      .findAll('button')
      .map(button => [button.text(), button.attributes('aria-label')])
  ).toEqual([
    ['', 'Edit'],
    ['', 'Delete'],
  ])
})
