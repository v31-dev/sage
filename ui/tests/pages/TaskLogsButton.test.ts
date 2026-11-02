import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import TaskLogsButton from '@/pages/Tasks/TaskLogsButton.vue'
import { fetchTaskParts, type QueueTask, type TaskPart } from '@/services/api'

vi.mock('@/services/api', async importOriginal => ({
  ...(await importOriginal<typeof import('@/services/api')>()),
  fetchTaskParts: vi.fn(),
}))

const inline = { template: '<div><slot /></div>' }
const stubs = {
  Dialog: {
    props: ['open'],
    emits: ['update:open'],
    template: '<div><slot /></div>',
  },
  DialogTrigger: inline,
  DialogContent: inline,
  DialogHeader: inline,
  DialogTitle: inline,
  DialogDescription: inline,
  SelectTrigger: inline,
  SelectContent: inline,
  SelectValue: inline,
  SelectItem: { props: ['value'], template: '<div data-option :data-value="value"><slot /></div>' },
  Select: {
    props: ['modelValue'],
    emits: ['update:modelValue'],
    template: '<div data-select><slot /></div>',
  },
  TaskLogs: {
    props: ['taskId', 'subTaskId', 'parts'],
    template: '<div data-logs>{{ taskId }}/{{ subTaskId }} {{ parts.length }}</div>',
  },
}

const parts: TaskPart[] = [
  { id: 'c1', type: 'deploy', worker: 'w1' },
  { id: 'c2', type: null, worker: null },
]

const task: QueueTask = {
  task_id: 't1',
  name: 'deploy_application',
  scopes: ['app:demo-web'],
  params: { application_id: 3 },
  executor: 'app',
  status: 'running',
  started_at: '2026-01-01T12:00:00',
  queued_at: '2026-01-01T12:00:00',
  quiet: false,
}

async function open() {
  const wrapper = mount(TaskLogsButton, { props: { task }, global: { stubs } })
  wrapper.findComponent(stubs.Dialog).vm.$emit('update:open', true)
  await flushPromises()
  return wrapper
}

beforeEach(() => {
  vi.mocked(fetchTaskParts).mockReset()
})

describe('the task log dialog', () => {
  it('is headed by the task id, and the name and params of the task', async () => {
    vi.mocked(fetchTaskParts).mockResolvedValue([])

    const wrapper = await open()

    expect(wrapper.text()).toContain('Task Logs (t1)')
    expect(wrapper.text()).toContain('deploy_application {"application_id":3}')
  })

  it("shows all the task's lines, with its parts to label them", async () => {
    vi.mocked(fetchTaskParts).mockResolvedValue(parts)

    const wrapper = await open()

    expect(fetchTaskParts).toHaveBeenCalledWith('t1')
    expect(wrapper.get('[data-logs]').text()).toBe('t1/ 2')
  })

  it('offers each part by its worker, kind and id, or its id alone', async () => {
    vi.mocked(fetchTaskParts).mockResolvedValue(parts)

    const wrapper = await open()

    expect(wrapper.findAll('[data-option]').map(option => option.text())).toEqual([
      'All lines',
      'w1 · deploy · c1',
      'c2',
    ])
  })

  it('shows the part chosen, and all the lines again', async () => {
    vi.mocked(fetchTaskParts).mockResolvedValue(parts)
    const wrapper = await open()
    const select = wrapper.findComponent(stubs.Select)

    select.vm.$emit('update:modelValue', 'c2')
    await flushPromises()
    expect(wrapper.get('[data-logs]').text()).toBe('t1/c2 2')

    select.vm.$emit('update:modelValue', 'all')
    await flushPromises()
    expect(wrapper.get('[data-logs]').text()).toBe('t1/ 2')
  })

  it.each([
    ['has no parts', () => vi.mocked(fetchTaskParts).mockResolvedValue([])],
    ["parts can't be read", () => vi.mocked(fetchTaskParts).mockRejectedValue(new Error('down'))],
  ])('offers no choice and shows all the lines when the task %s', async (_case, arrange) => {
    arrange()

    const wrapper = await open()

    expect(wrapper.find('[data-select]').exists()).toBe(false)
    expect(wrapper.get('[data-logs]').text()).toBe('t1/ 0')
  })
})
