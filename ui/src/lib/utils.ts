import type { ClassValue } from "clsx"
import { clsx } from "clsx"
import { twMerge } from "tailwind-merge"

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs))
}

export const colors = [
  '#3b82f6', '#ef4444', '#10b981', '#f59e0b', '#8b5cf6',
  '#ec4899', '#06b6d4', '#14b8a6', '#f97316', '#6366f1',
  '#84cc16', '#eab308', '#a855f7', '#d946ef', '#0891b2',
  '#059669', '#dc2626', '#ea580c', '#7c3aed', '#db2777'
]

const _tsFormatter = new Intl.DateTimeFormat(undefined, {
  month: 'short', day: '2-digit',
  hour: '2-digit', minute: '2-digit', second: '2-digit',
  hour12: true,
})

// dateObj like '2026-03-19T16:12:42.759325367Z' or Date object
export function formatDate(dateObj: string | Date): string {
  try {
    let date
    if (typeof dateObj === 'string') {
      if (!dateObj.endsWith('Z')) {
        dateObj += 'Z'
      }
      date = new Date(dateObj)
    } else {
      date = dateObj
    }
    return _tsFormatter.format(date).replace(',', '')
  } catch {
    return typeof dateObj === 'string' ? dateObj : dateObj.toISOString()
  }
}

export function levelClass(level: string): string {
  const l = level.toUpperCase()
  if (l.startsWith('ERROR') || l.startsWith('CRIT')) return 'text-red-500 font-semibold'
  if (l.startsWith('WARN')) return 'text-yellow-500 font-semibold'
  if (l.startsWith('DEBUG')) return 'text-muted-foreground'
  return 'text-sky-600 dark:text-sky-400'
}