import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it } from 'vitest'
import { formatDate } from '@/lib/utils'
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
  started_at: '2026-01-01T12:00:05',
  queued_at: '2026-01-01T12:00:00',
  quiet: false,
}

async function open(shown: QueueTask = task) {
  const wrapper = mount(TaskLogsButton, {
    props: { task: shown, section: 'running' },
    global: { stubs },
  })
  wrapper.findComponent(stubs.Dialog).vm.$emit('update:open', true)
  await flushPromises()
  return wrapper
}

describe('the task log dialog', () => {
  beforeEach(() => setActivePinia(createPinia()))

  it('is headed by the task name and status, with its id and labels below', async () => {
    const wrapper = await open()

    expect(wrapper.text()).toContain('deploy_application')
    expect(wrapper.text()).toContain('running')
    expect(wrapper.get('[data-subjects]').text().replace(/\s+/g, ' ')).toBe(
      't1 · Application: shop / web'
    )
  })

  it('sums up the executor, times, scopes and params above the logs', async () => {
    const wrapper = await open({ ...task, scopes: ['app', 'app:demo-web'] })
    const scopes = wrapper
      .get('[data-scopes]')
      .findAll('div')
      .map(line => line.text())
    const params = wrapper
      .get('[data-params]')
      .findAll('dt')
      .map(dt => [dt.text(), dt.element.nextElementSibling!.textContent!.trim()])

    expect(wrapper.get('[data-executor]').text()).toBe('app')
    expect(wrapper.findAll('[data-time]').map(time => time.text())).toEqual([
      formatDate(task.queued_at),
      formatDate(task.started_at!),
      '5 secs',
    ])
    expect(scopes).toEqual(['app', 'app:demo-web'])
    expect(params).toEqual([
      ['application_id', '3'],
      ['volumes', '["v1","v2"]'],
    ])
  })

  it('leaves out the labels and params a task does not have', async () => {
    const wrapper = await open({ ...task, labels: {}, params: {} })

    expect(wrapper.get('[data-subjects]').text()).toBe('t1')
    expect(wrapper.find('[data-scopes]').exists()).toBe(true)
    expect(wrapper.find('[data-params]').exists()).toBe(false)
  })

  it('leaves out the scopes when a task has none', async () => {
    const wrapper = await open({ ...task, scopes: [] })

    expect(wrapper.find('[data-scopes]').exists()).toBe(false)
    expect(wrapper.find('[data-params]').exists()).toBe(true)
  })

  it("shows all the task's lines", async () => {
    const wrapper = await open()

    expect(wrapper.get('[data-logs]').text()).toBe('t1')
  })
})
