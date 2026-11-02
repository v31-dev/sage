import { request } from '@/lib/api'
import { parseDate } from '@/lib/utils'

// A request one of the workers' Traefik served; `duration_ms` is from when it arrived to when it was answered.
export interface ServedRequest {
  id: number
  ts: string
  worker: string
  host: string
  method: string
  path: string
  status: number
  duration_ms: number
  router: string
  service: string
  client: string
  size: number
}

export type StatusClass = '2xx' | '3xx' | '4xx' | '5xx'

// A worker and a method exactly, text anywhere in the host or the path, a status class, only those a router matched if
// `routed`, and a start at or after `from_ts` and before `to_ts`; only the requests after `after_id`, if given, and the
// newest `limit` of them.
export type RequestQuery = {
  worker?: string
  host?: string
  method?: string
  status?: StatusClass | ''
  path?: string
  routed?: boolean
  from_ts?: string
  to_ts?: string
  after_id?: number | null
  limit?: number
}

// The newest requests that match, newest first, and whether there were more.
export interface RequestPage {
  requests: ServedRequest[]
  more: boolean
}

export const fetchRequests = (query: RequestQuery = {}) =>
  request<RequestPage>('GET', '/requests/', { query })

// Whether the requests route lets a request through `query`, its `after_id` aside: what a view keeps of the requests
// pushed to it.
export function matchesRequestQuery(query: RequestQuery, row: ServedRequest) {
  const { worker, method, status, host, path, routed, from_ts, to_ts } = query
  return (
    (!worker || row.worker === worker) &&
    (!method || row.method === method) &&
    (!status || String(row.status)[0] === status[0]) &&
    (!host || row.host.toLowerCase().includes(host.toLowerCase())) &&
    (!path || row.path.toLowerCase().includes(path.toLowerCase())) &&
    (!routed || row.router !== '') &&
    (!from_ts || parseDate(row.ts) >= new Date(from_ts)) &&
    (!to_ts || parseDate(row.ts) < new Date(to_ts))
  )
}
