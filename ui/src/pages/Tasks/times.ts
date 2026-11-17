import { formatDate, parseDate } from '@/lib/utils'
import type { FinishedTask, QueueTask } from '@/services/api'

export type TaskSection = 'running' | 'queued' | 'finished'

// The times a task of each section shows.
const TIME_LABELS: Record<TaskSection, string[]> = {
  queued: ['Queued'],
  running: ['Queued', 'Started'],
  finished: ['Queued', 'Started', 'Finished'],
}

// Each time of the section, formatted; '—' for one the task does not have. A finished task that started also shows
// how long it ran: seconds under a minute, minutes and seconds from one.
export function taskTimes(task: QueueTask | FinishedTask, section: TaskSection) {
  const finished = 'finished_at' in task ? task.finished_at : null
  const times: Record<string, string | null> = {
    Queued: 'queued_at' in task ? task.queued_at : task.created_at,
    Started: task.started_at,
    Finished: finished,
  }
  const shown = TIME_LABELS[section].map(label => {
    const time = times[label]
    return { label, value: time ? formatDate(time) : '—' }
  })
  if (section !== 'finished' || !task.started_at || !finished) return shown
  const seconds = Math.round(
    (parseDate(finished).getTime() - parseDate(task.started_at).getTime()) / 1000
  )
  const minutes = Math.floor(seconds / 60)
  const ran =
    seconds < 60
      ? `${seconds} ${seconds === 1 ? 'sec' : 'secs'}`
      : `${minutes}:${String(seconds % 60).padStart(2, '0')} ${minutes === 1 ? 'min' : 'mins'}`
  return [...shown, { label: 'Task Time', value: ran }]
}
