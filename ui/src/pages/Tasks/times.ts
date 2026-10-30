import { formatDate } from '@/lib/utils'
import type { FinishedTask, QueueTask } from '@/services/api'

export type TaskSection = 'running' | 'queued' | 'finished'

// The times a task of each section shows.
export const TIME_LABELS: Record<TaskSection, string[]> = {
  queued: ['Queued'],
  running: ['Queued', 'Started'],
  finished: ['Queued', 'Started', 'Finished'],
}

// Each time of the section, formatted; '—' for one the task does not have.
export function taskTimes(task: QueueTask | FinishedTask, section: TaskSection) {
  const times: Record<string, string | null> = {
    Queued: 'queued_at' in task ? task.queued_at : task.created_at,
    Started: task.started_at,
    Finished: 'finished_at' in task ? task.finished_at : null,
  }
  return TIME_LABELS[section].map(label => {
    const time = times[label]
    return { label, value: time ? formatDate(time) : '—' }
  })
}
