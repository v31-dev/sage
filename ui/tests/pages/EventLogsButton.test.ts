import { flushPromises, mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import EventLogsButton from '@/pages/Application/EventLogsButton.vue'
import type { Container, Event } from '@/services/api'

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
  SelectItem: inline,
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

const event = (application: string, part: string, type: Event['type'] = 'deploy'): Event => ({
  application_task_id: application,
  container_task_id: part,
  type,
  created_at: new Date('2026-01-01T12:00:00Z'),
})

function setup(events: Event[]) {
  const container = { worker: { hostname: 'w1' }, events } as unknown as Container
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
  it('show the newest event at first: its part of its task', async () => {
    const wrapper = setup([event('t1', 'c1'), event('t2', 'c2', 'stop')])

    await open(wrapper)

    expect(wrapper.get('[data-logs]').text()).toBe('t2/c2')
  })

  it('show the part of the event chosen', async () => {
    const wrapper = setup([event('t1', 'c1'), event('t2', 'c2', 'stop')])
    await open(wrapper)

    wrapper.findComponent(stubs.Select).vm.$emit('update:modelValue', 'c1')
    await flushPromises()

    expect(wrapper.get('[data-logs]').text()).toBe('t1/c1')
  })

  it('show nothing for a container without events', async () => {
    const wrapper = setup([])

    await open(wrapper)

    expect(wrapper.find('[data-logs]').exists()).toBe(false)
  })
})
