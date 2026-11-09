import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import AddWorkerButton from '@/pages/Workers/AddWorkerButton.vue'
import { addWorker } from '@/services/api'

vi.mock('vue-sonner', () => ({ toast: { success: vi.fn(), error: vi.fn() } }))
vi.mock('@/services/api', async importOriginal => ({
  ...(await importOriginal<typeof import('@/services/api')>()),
  addWorker: vi.fn(async () => ({ hostname: 'w9' })),
}))

const inline = { template: '<div><slot /></div>' }
const stubs = {
  Dialog: inline,
  DialogTrigger: inline,
  DialogContent: inline,
  DialogHeader: inline,
  DialogTitle: inline,
  DialogDescription: inline,
  DialogFooter: inline,
}

async function add(fields: Record<string, string>) {
  const wrapper = mount(AddWorkerButton, { global: { stubs } })
  await wrapper
    .findAll('button')
    .find(button => button.text().includes('Add Worker'))!
    .trigger('click')
  for (const [id, value] of Object.entries(fields)) {
    await wrapper.get(`#${id}`).setValue(value)
  }
  await wrapper
    .findAll('button')
    .find(button => button.text() === 'Add')!
    .trigger('click')
  await flushPromises()
  return wrapper
}

beforeEach(() => {
  vi.mocked(addWorker).mockClear()
})

describe('adding a worker', () => {
  it('sends the machine with the subdomain given, trimmed', async () => {
    await add({ 'worker-host': ' w9 ', 'worker-subdomain': ' eu-1 ' })

    expect(addWorker).toHaveBeenCalledWith({ host: 'w9', subdomain: 'eu-1' })
  })

  it('refuses a worker without a subdomain before sending it', async () => {
    const wrapper = await add({ 'worker-host': 'w9', 'worker-subdomain': '  ' })

    expect(addWorker).not.toHaveBeenCalled()
    expect(wrapper.text()).toContain('Subdomain is required')
  })

  it("shows the manager's refusal", async () => {
    vi.mocked(addWorker).mockRejectedValueOnce(
      new Error('A worker with subdomain eu1 already exists.')
    )

    const wrapper = await add({ 'worker-host': 'w9', 'worker-subdomain': 'eu1' })

    expect(wrapper.text()).toContain('A worker with subdomain eu1 already exists.')
  })
})
