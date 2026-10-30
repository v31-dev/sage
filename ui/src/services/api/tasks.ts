import { request } from '@/lib/api'
import { parseDate } from '@/lib/utils'
import type { Event } from './applications'

interface TaskFields {
  task_id: string
  name: string
  scopes: string[]
  params: Record<string, any>
  executor: string
  status: string
  started_at: string | null
}

// A task running or waiting.
export interface QueueTask extends TaskFields {
  queued_at: string
  quiet: boolean
}

// A task that has finished: `created_at` is when it was queued, and it has no `started_at` if it was cancelled first.
export interface FinishedTask extends TaskFields {
  created_at: string
  updated_at: string
  finished_at: string
}

export interface QueueResponse {
  running: QueueTask[]
  queued: QueueTask[]
}

// The tasks that are running or waiting, held in memory by the manager.
export const fetchTasks = () => request<QueueResponse>('GET', '/tasks/')

// Which finished tasks: a name, executor and status match exactly; a scope matches itself and its children (`app`
// matches `app:demo-web`), `*` any characters, in any case; `from_ts` and `to_ts` match a task active at any point
// between them.
export type TaskHistoryQuery = {
  name?: string
  scope?: string
  executor?: string
  status?: string
  from_ts?: string
  to_ts?: string
  limit?: number
  offset?: number
}

export interface TaskHistoryPage {
  tasks: FinishedTask[]
  total: number
}

// A page of the finished tasks, newest finished first, and how many match.
export const fetchTaskHistory = (query: TaskHistoryQuery = {}) =>
  request<TaskHistoryPage>('GET', '/tasks/history', { query })

// Whether the history route lets a finished task through `query`, its page aside: what a view keeps of the tasks
// pushed to it.
export function matchesTaskHistoryQuery(query: TaskHistoryQuery, task: FinishedTask) {
  const { name, executor, status, scope, from_ts, to_ts } = query
  const scoped = scope ? scopePattern(scope) : null
  return (
    (!name || task.name === name) &&
    (!executor || task.executor === executor) &&
    (!status || task.status === status) &&
    (!scoped || task.scopes.some(each => scoped.test(each))) &&
    (!from_ts || parseDate(task.finished_at) >= new Date(from_ts)) &&
    (!to_ts || parseDate(task.created_at) <= new Date(to_ts))
  )
}

function scopePattern(scope: string) {
  const literal = (text: string) => text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
  return new RegExp(`^${scope.split('*').map(literal).join('.*')}(:.*)?$`, 'i')
}

// The choices of the finished tasks' filters.
export interface TaskHistoryFilters {
  names: string[]
  executors: string[]
  statuses: string[]
}

export const fetchTaskHistoryFilters = () =>
  request<TaskHistoryFilters>('GET', '/tasks/history/filters')

// A line a queued task logged; `sub_task_id` is the container's part it belongs to, if any.
export interface TaskLogLine {
  id: number
  task_id: string
  sub_task_id: string
  ts: string
  level: string
  logger: string
  message: string
}

// The newest lines, in the order they were written, and whether there were more.
export interface TaskLogPage {
  lines: TaskLogLine[]
  more: boolean
}

// A container's part of a task; its kind and worker are null when its event went with its container.
export interface TaskPart {
  id: string
  type: Event['type'] | null
  worker: string | null
}

// The parts of a task that logged lines, in the order of their first line.
export const fetchTaskParts = (taskId: string) =>
  request<TaskPart[]>('GET', `/tasks/${taskId}/parts`)

// A task's lines, or only those of one container's part of it; after the line `afterId`, if given.
export const fetchTaskLogs = (taskId: string, subTaskId = '', afterId = 0) =>
  request<TaskLogPage>('GET', `/tasks/${taskId}/logs`, {
    query: { sub_task_id: subTaskId, after_id: afterId || null },
  })
