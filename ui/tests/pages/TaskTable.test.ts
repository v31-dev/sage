import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it } from 'vitest'
import { formatDate } from '@/lib/utils'
import TaskTable from '@/pages/Tasks/TaskTable.vue'
import { rowTimes, taskTimes } from '@/pages/Tasks/times'
import type { FinishedTask, QueueTask } from '@/services/api'

const at = (minute: number) => `2026-01-01T12:${String(minute).padStart(2, '0')}:00`

const finished: FinishedTask = {
  task_id: 'f1',
  name: 'deploy',
  scopes: ['worker:w1', 'worker:w1:container:c1'],
  params: { container_id: 'c1', force: true },
  labels: {},
  executor: 'app',
  status: 'completed',
  created_at: at(0),
  started_at: at(1),
  updated_at: at(2),
  finished_at: at(2),
}

const queued: QueueTask = {
  task_id: 'q1',
  name: 'deploy',
  scopes: ['worker:w1'],
  params: {},
  labels: {},
  executor: 'app',
  status: 'queued',
  queued_at: at(0),
  started_at: null,
  quiet: false,
}

function table(row: QueueTask | FinishedTask, section: 'queued' | 'finished') {
  return mount(TaskTable, {
    props: { rows: [row], section },
    global: { stubs: { TaskLogsButton: true } },
  })
}

// Each name in a cell with the value beside it.
function pairs(row: QueueTask | FinishedTask, section: 'queued' | 'finished', name: string) {
  return table(row, section)
    .get(`[data-${name}]`)
    .findAll('dt')
    .map(dt => [dt.text(), dt.element.nextElementSibling!.textContent!.trim()])
}

describe('the task table', () => {
  beforeEach(() => setActivePinia(createPinia()))

  it('has a column each for the task, name, executor, scopes, labels and times', () => {
    expect(
      table(finished, 'finished')
        .findAll('th')
        .map(th => th.text())
    ).toEqual(['Task', 'Name', 'Executor', 'Scopes', 'Labels', 'Times'])
  })

  it("shows a finished task's status as an icon left of its id", () => {
    const task = table(finished, 'finished').get('td').get('div')
    const [icon, id] = task.element.children
    expect(icon!.getAttribute('role')).toBe('img')
    expect(icon!.getAttribute('aria-label')).toBe('completed')
    expect(id!.tagName.toLowerCase()).toBe('task-logs-button-stub')
    expect(task.text()).toBe('')
  })

  it("shows a finished task's end, then how long it waited and ran on one line", () => {
    const wrapper = table(finished, 'finished')
    expect(pairs(finished, 'finished', 'times')).toEqual([['Finished', formatDate(at(2))]])
    expect(wrapper.get('[data-durations]').text().replace(/\s+/g, ' ')).toBe(
      'Waited 1:00 min · Ran 1:00 min'
    )
  })

  it.each([
    [0, '0 secs'],
    [1, '1 sec'],
    [59, '59 secs'],
    [60, '1:00 min'],
    [125, '2:05 mins'],
    [3600, '60:00 mins'],
  ])('gives a task that ran %i seconds a run time of %s', (seconds, shown) => {
    const ran = {
      ...finished,
      finished_at: new Date(Date.parse(`${at(1)}Z`) + seconds * 1000).toISOString(),
    }
    expect(taskTimes(ran, 'finished').slice(-1)).toEqual([
      { label: 'Ran', value: shown, duration: true },
    ])
  })

  it('gives a running task how long it waited, and no run time', () => {
    const times = taskTimes({ ...queued, started_at: at(3) }, 'running')
    expect(times.map(time => time.label)).toEqual(['Queued', 'Started', 'Waited'])
    expect(times[2]!.value).toBe('3:00 mins')
  })

  it("shows a running task's two dates in its row, without its wait", () => {
    const { dates, durations } = rowTimes({ ...queued, started_at: at(3) }, 'running')
    expect(dates.map(time => time.label)).toEqual(['Queued', 'Started'])
    expect(durations).toEqual([])
  })

  it('gives no wait or run time to a task that never started', () => {
    const labels = taskTimes({ ...finished, started_at: null }, 'finished').map(time => time.label)
    expect(labels).toEqual(['Queued', 'Started', 'Finished'])
    expect(rowTimes({ ...finished, started_at: null }, 'finished').durations).toEqual([])
  })

  it('shows a queued task only when it was queued, with no status', () => {
    const wrapper = table(queued, 'queued')
    expect(pairs(queued, 'queued', 'times')).toEqual([['Queued', formatDate(at(0))]])
    expect(wrapper.find('[data-durations]').exists()).toBe(false)
    expect(wrapper.get('td').text()).toBe('')
  })

  it('shows each label with its value beside it', () => {
    const labelled = { ...finished, labels: { Application: 'shop / web', Worker: 'w1' } }
    expect(pairs(labelled, 'finished', 'labels')).toEqual([
      ['Application', 'shop / web'],
      ['Worker', 'w1'],
    ])
  })

  it('puts each scope on a line of its own', () => {
    expect(
      table(finished, 'finished')
        .get('[data-scopes]')
        .findAll('div')
        .map(line => line.text())
    ).toEqual(['worker:w1', 'worker:w1:container:c1'])
  })
})
