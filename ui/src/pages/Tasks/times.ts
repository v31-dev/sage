import { formatDate, parseDate } from '@/lib/utils'
import type { FinishedTask, QueueTask } from '@/services/api'

export type TaskSection = 'running' | 'queued' | 'finished'

// The times a task of each section shows.
const TIME_LABELS: Record<TaskSection, string[]> = {
  queued: ['Queued'],
  running: ['Queued', 'Started'],
  finished: ['Queued', 'Started', 'Finished'],
}

// Seconds under a minute, minutes and seconds from one.
function duration(from: string, to: string) {
  const seconds = Math.round((parseDate(to).getTime() - parseDate(from).getTime()) / 1000)
  const minutes = Math.floor(seconds / 60)
  return seconds < 60
    ? `${seconds} ${seconds === 1 ? 'sec' : 'secs'}`
    : `${minutes}:${String(seconds % 60).padStart(2, '0')} ${minutes === 1 ? 'min' : 'mins'}`
}

// Each time of the section, formatted; '—' for one the task does not have. A task that started also shows how long
// it waited, and a finished one how long it ran (`duration`).
export function taskTimes(task: QueueTask | FinishedTask, section: TaskSection) {
  const queued = 'queued_at' in task ? task.queued_at : task.created_at
  const finished = 'finished_at' in task ? task.finished_at : null
  const times: Record<string, string | null> = {
    Queued: queued,
    Started: task.started_at,
    Finished: finished,
  }
  const shown = TIME_LABELS[section].map(label => {
    const time = times[label]
    return { label, value: time ? formatDate(time) : '—', duration: false }
  })
  if (section === 'queued' || !task.started_at) return shown
  shown.push({ label: 'Waited', value: duration(queued, task.started_at), duration: true })
  if (finished)
    shown.push({ label: 'Ran', value: duration(task.started_at, finished), duration: true })
  return shown
}

// The times a table row or card shows: a finished task's end and durations, the others' dates.
export function rowTimes(task: QueueTask | FinishedTask, section: TaskSection) {
  const times = taskTimes(task, section)
  if (section !== 'finished') return { dates: times.filter(time => !time.duration), durations: [] }
  return {
    dates: times.filter(time => time.label === 'Finished'),
    durations: times.filter(time => time.duration),
  }
}
