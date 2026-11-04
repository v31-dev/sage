import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import EditWorkerButton from '@/pages/Worker/EditWorkerButton.vue'
import { updateWorker, type Worker } from '@/services/api'

vi.mock('vue-sonner', () => ({ toast: { success: vi.fn(), error: vi.fn() } }))
vi.mock('@/services/api', async importOriginal => ({
  ...(await importOriginal<typeof import('@/services/api')>()),
  updateWorker: vi.fn(),
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

const worker = (tag: string | null, pool: string | null) =>
  ({ hostname: 'w1', tag, pool }) as unknown as Worker

const loadWorkerStatus = vi.fn(async () => {})

async function open(current: Worker) {
  const wrapper = mount(EditWorkerButton, {
    props: { worker: current, loadWorkerStatus },
    global: { stubs },
  })
  await wrapper
    .findAll('button')
    .find(button => button.text().includes('Edit'))!
    .trigger('click')
  return wrapper
}

const save = async (wrapper: Awaited<ReturnType<typeof open>>) => {
  await wrapper
    .findAll('button')
    .find(button => button.text() === 'Save')!
    .trigger('click')
  await flushPromises()
}

beforeEach(() => {
  vi.mocked(updateWorker).mockReset()
  loadWorkerStatus.mockClear()
})

describe('editing a worker', () => {
  it("opens with the worker's tag and pool", async () => {
    const wrapper = await open(worker('eu1', 'eu'))

    expect((wrapper.get('#worker-edit-tag').element as HTMLInputElement).value).toBe('eu1')
    expect((wrapper.get('#worker-edit-pool').element as HTMLInputElement).value).toBe('eu')
  })

  it('sends the tag and pool, trimmed, and then has the page load the worker again', async () => {
    vi.mocked(updateWorker).mockResolvedValue(worker('eu2', 'west'))
    const wrapper = await open(worker('eu1', null))

    await wrapper.get('#worker-edit-tag').setValue(' eu2 ')
    await wrapper.get('#worker-edit-pool').setValue('west')
    await save(wrapper)

    expect(updateWorker).toHaveBeenCalledWith('w1', { tag: 'eu2', pool: 'west' })
    expect(loadWorkerStatus).toHaveBeenCalledOnce()
  })

  it("shows the manager's refusal and leaves the page as it is", async () => {
    vi.mocked(updateWorker).mockRejectedValue(new Error('A worker with tag eu2 already exists.'))
    const wrapper = await open(worker('eu1', null))

    await wrapper.get('#worker-edit-tag').setValue('eu2')
    await save(wrapper)

    expect(wrapper.text()).toContain('A worker with tag eu2 already exists.')
    expect(loadWorkerStatus).not.toHaveBeenCalled()
  })
})
