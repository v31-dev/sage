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
  params: { application_id: 3, volumes: ['v1', 'v2'] },
  labels: { Application: 'shop / web' },
  executor: 'app',
  status: 'running',
  started_at: '2026-01-01T12:00:00',
  queued_at: '2026-01-01T12:00:00',
  quiet: false,
}

async function open(shown: QueueTask = task) {
  const wrapper = mount(TaskLogsButton, { props: { task: shown }, global: { stubs } })
  wrapper.findComponent(stubs.Dialog).vm.$emit('update:open', true)
  await flushPromises()
  return wrapper
}

describe('the task log dialog', () => {
  it('is headed by the task id and name, with its labels and params side by side, a line each', async () => {
    const wrapper = await open()
    const lines = (column: string) =>
      wrapper
        .get(`[data-${column}]`)
        .findAll('div')
        .map(line => line.text())

    expect(wrapper.text()).toContain('Task Logs (t1)')
    expect(wrapper.text()).toContain('deploy_application')
    expect(lines('labels')).toEqual(['Application: shop / web'])
    expect(lines('params')).toEqual(['application_id: 3', 'volumes: ["v1","v2"]'])
  })

  it('shows a dash for a task with no labels or params', async () => {
    const wrapper = await open({ ...task, labels: {}, params: {} })

    expect(wrapper.get('[data-labels]').text()).toBe('Labels-')
    expect(wrapper.get('[data-params]').text()).toBe('Params-')
  })

  it("shows all the task's lines", async () => {
    const wrapper = await open()

    expect(wrapper.get('[data-logs]').text()).toBe('t1')
  })
})
