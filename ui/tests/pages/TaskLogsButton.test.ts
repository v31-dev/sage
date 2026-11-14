import { flushPromises, mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import TaskLogsButton from '@/pages/Tasks/TaskLogsButton.vue'
import { type QueueTask } from '@/services/api'

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
  TaskLogs: {
    props: ['taskId'],
    template: '<div data-logs>{{ taskId }}</div>',
  },
}

const task: QueueTask = {
  task_id: 't1',
  name: 'deploy_application',
  scopes: ['app:demo-web'],
  params: { application_id: 3 },
  labels: {},
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

describe('the task log dialog', () => {
  it('is headed by the task id, and the name and params of the task', async () => {
    const wrapper = await open()

    expect(wrapper.text()).toContain('Task Logs (t1)')
    expect(wrapper.text()).toContain('deploy_application {"application_id":3}')
  })

  it("shows all the task's lines", async () => {
    const wrapper = await open()

    expect(wrapper.get('[data-logs]').text()).toBe('t1')
  })
})
