import { flushPromises, mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import EventLogsButton from '@/pages/Application/EventLogsButton.vue'
import type { Container, ContainerEvent } from '@/services/api'

const inline = { template: '<div><slot /></div>' }
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
  SelectValue: inline,
  SelectItem: { template: '<div data-item><slot /></div>' },
  Select: {
    props: ['modelValue'],
    emits: ['update:modelValue'],
    template: '<div data-select><slot /></div>',
  },
  TaskLogs: {
    props: ['taskId', 'subTaskId'],
    template: '<div data-logs>{{ taskId }}/{{ subTaskId }}</div>',
  },
}

let ids = 0

// The newest come first, as the manager sends them.
const event = (
  task: string | null,
  part: string | null,
  action = 'deploy',
  transition = 'succeeded'
): ContainerEvent => ({
  id: ++ids,
  action,
  transition,
  operation_id: 'o1',
  reason: null,
  task_id: task,
  part_id: part,
  created_at: '2026-01-01T12:00:00',
})

function setup(events: ContainerEvent[]) {
  const container = { id: 'c1', worker: { hostname: 'w1' }, events } as unknown as Container
  return mount(EventLogsButton, { props: { container }, global: { stubs } })
}

async function open(wrapper: ReturnType<typeof setup>) {
  await wrapper
    .findAll('button')
    .find(button => button.text().includes('Event Logs'))!
    .trigger('click')
  await flushPromises()
}

describe('the event logs', () => {
  it('show the newest run at first: its part of its task', async () => {
    const wrapper = setup([event('t2', 'p2', 'stop'), event('t1', 'p1')])

    await open(wrapper)

    expect(wrapper.get('[data-logs]').text()).toBe('t2/p2')
  })

  it('list each run once, by its newest event, and name its operation', async () => {
    const wrapper = setup([
      event('t1', 'p1', 'deploy', 'succeeded'),
      event('t1', 'p1', 'deploy', 'admitted'),
      event('t0', null, 'backup_preserve', 'failed'),
      event(null, null, 'deploy', 'interrupted'),
    ])

    await open(wrapper)

    expect(wrapper.findAll('[data-item]').map(item => item.text())).toEqual([
      expect.stringContaining('Deploy succeeded'),
      expect.stringContaining('Automatic backup failed'),
    ])
  })

  it('show the run chosen, a whole task when its event names no part', async () => {
    const wrapper = setup([event('t2', 'p2', 'stop'), event('t1', null)])
    await open(wrapper)

    wrapper.findComponent(stubs.Select).vm.$emit('update:modelValue', 't1/')
    await flushPromises()

    expect(wrapper.get('[data-logs]').text()).toBe('t1/')
  })

  it('show nothing for a container without events', async () => {
    const wrapper = setup([])

    await open(wrapper)

    expect(wrapper.find('[data-logs]').exists()).toBe(false)
  })
})
