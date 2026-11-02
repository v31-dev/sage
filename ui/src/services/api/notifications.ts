import { request } from '@/lib/api'

export interface Notification {
  id: number
  type: 'info' | 'success' | 'warning' | 'error'
  content: string
  link: string | null
  created_at: string
  updated_at: string
}

export const fetchNotifications = ({
  limit = null,
  from_ts = null,
  to_ts = null,
}: {
  limit?: number | null
  from_ts?: string | null
  to_ts?: string | null
} = {}) => request<Notification[]>('GET', '/notifications/', { query: { limit, from_ts, to_ts } })
