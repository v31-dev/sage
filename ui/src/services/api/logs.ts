import { request } from '@/lib/api'
import { parseDate } from '@/lib/utils'
import { applicationPath } from './_common'

// The stream of the lines Sage writes into an application's logs: what it did to a container (deployed it, stopped
// it, …), with the task's id.
export const MARKER_STREAM = 'sage'

// A line one of an application's containers wrote, or a marker of what Sage did to it.
export interface LogEntry {
  id: number
  hostname: string
  ts: string
  stream: string
  message: string
}

// A worker (all if empty), text anywhere in a line in any case, and a time at or after `from_ts` and before `to_ts`;
// only the lines after `after_id`, if given, and the newest `limit` of them.
export type LogQuery = {
  hostname?: string
  search?: string
  from_ts?: string
  to_ts?: string
  after_id?: number | null
  limit?: number
}

// The newest lines that match, in the order they were stored, and whether there were more.
export interface LogPage {
  lines: LogEntry[]
  more: boolean
}

export const fetchApplicationLogs = (project: string, application: string, query: LogQuery = {}) =>
  request<LogPage>('GET', `${applicationPath(project, application)}/logs`, { query })

// Whether the logs route lets a line through `query`, its `after_id` aside: what a view keeps of the lines pushed to
// it.
export function matchesLogQuery(query: LogQuery, line: LogEntry) {
  const search = query.search?.toLowerCase()
  return (
    (!query.hostname || line.hostname === query.hostname) &&
    (!search || line.message.toLowerCase().includes(search)) &&
    (!query.from_ts || parseDate(line.ts) >= new Date(query.from_ts)) &&
    (!query.to_ts || parseDate(line.ts) < new Date(query.to_ts))
  )
}
