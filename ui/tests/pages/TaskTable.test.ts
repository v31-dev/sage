import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it } from 'vitest'
import { formatDate } from '@/lib/utils'
import TaskTable from '@/pages/Tasks/TaskTable.vue'
import { taskTimes } from '@/pages/Tasks/times'
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

function cell(row: QueueTask | FinishedTask, section: 'queued' | 'finished', name: string) {
  const wrapper = mount(TaskTable, {
    props: { rows: [row], section },
    global: { stubs: { TaskLogsButton: true } },
  })
  return wrapper
    .get(`[data-${name}]`)
    .findAll('div')
    .map(line => line.text())
}

describe('the task table', () => {
  beforeEach(() => setActivePinia(createPinia()))

  it("stacks a finished task's times in one cell, each with its name", () => {
    expect(cell(finished, 'finished', 'times')).toEqual([
      `Queued: ${formatDate(at(0))}`,
      `Started: ${formatDate(at(1))}`,
      `Finished: ${formatDate(at(2))}`,
      'Task Time: 1:00 min',
    ])
  })

  it.each([
    [0, '0 secs'],
    [1, '1 sec'],
    [59, '59 secs'],
    [60, '1:00 min'],
    [125, '2:05 mins'],
    [3600, '60:00 mins'],
  ])('gives a task that ran %i seconds a task time of %s', (seconds, shown) => {
    const ran = {
      ...finished,
      finished_at: new Date(Date.parse(`${at(1)}Z`) + seconds * 1000).toISOString(),
    }
    expect(taskTimes(ran, 'finished').at(-1)).toEqual({ label: 'Task Time', value: shown })
  })

  it('gives no task time to a task that never started', () => {
    const labels = taskTimes({ ...finished, started_at: null }, 'finished').map(time => time.label)
    expect(labels).toEqual(['Queued', 'Started', 'Finished'])
  })

  it('puts each param on a line of its own, a non-text value as JSON', () => {
    expect(cell(finished, 'finished', 'params')).toEqual(['container_id: c1', 'force: true'])
  })

  it('shows a queued task only when it was queued', () => {
    expect(cell(queued, 'queued', 'times')).toEqual([`Queued: ${formatDate(at(0))}`])
  })

  it('puts each scope on a line of its own', () => {
    expect(cell(finished, 'finished', 'scopes')).toEqual(['worker:w1', 'worker:w1:container:c1'])
  })
})
