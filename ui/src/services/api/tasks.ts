import { request } from '@/lib/api'
import { parseDate } from '@/lib/utils'

interface TaskFields {
  task_id: string
  name: string
  scopes: string[]
  params: Record<string, unknown>
  labels: Record<string, string>
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
  label?: string
  executor?: string
  status?: string
  from_ts?: string
  to_ts?: string
  limit?: number
}

export interface TaskHistoryPage {
  tasks: FinishedTask[]
  total: number
}

// The newest `limit` finished tasks that match, newest finished first, and how many match.
export const fetchTaskHistory = (query: TaskHistoryQuery = {}) =>
  request<TaskHistoryPage>('GET', '/tasks/history', { query })

// Whether the history route lets a finished task through `query`, its page aside: what a view keeps of the tasks
// pushed to it.
export function matchesTaskHistoryQuery(query: TaskHistoryQuery, task: FinishedTask) {
  const { name, executor, status, scope, label, from_ts, to_ts } = query
  const scoped = scope ? scopePattern(scope) : null
  const fold = (text: string) => text.replace(/[A-Z]/g, letter => letter.toLowerCase())
  return (
    (!name || task.name === name) &&
    (!executor || task.executor === executor) &&
    (!status || task.status === status) &&
    (!scoped || task.scopes.some(each => scoped.test(each))) &&
    (!label || Object.values(task.labels).some(value => fold(value).includes(fold(label)))) &&
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

// A line a queued task logged.
export interface TaskLogLine {
  id: number
  task_id: string
  ts: string
  level: string
  logger: string
  message: string
}

// Every line, in the order they were written.
export interface TaskLogPage {
  lines: TaskLogLine[]
}

// A task's lines; after the line `afterId`, if given.
export const fetchTaskLogs = (taskId: string, afterId = 0) =>
  request<TaskLogPage>('GET', `/tasks/${taskId}/logs`, { query: { after_id: afterId || null } })
