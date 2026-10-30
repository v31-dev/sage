import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import TaskLogs from '@/components/TaskLogs.vue'
import { formatDate } from '@/lib/utils'
import { fetchTaskLogs, type TaskLogLine, type TaskLogs as Logs } from '@/services/api'

vi.mock('@/services/api', async importOriginal => ({
  ...(await importOriginal<typeof import('@/services/api')>()),
  fetchTaskLogs: vi.fn(),
}))

const TS = '2026-01-01T12:00:00.000000Z'

const line = (id: number, message: string, level = 'INFO'): TaskLogLine => ({
  id,
  task_id: 't1',
  sub_task_id: 'c1',
  ts: TS,
  level,
  logger: 'services.manager',
  message,
})

const logs = (lines: TaskLogLine[], more = false): Logs => ({ lines, more })

const rows = (wrapper: ReturnType<typeof mount>) =>
  wrapper.findAll('tbody tr').map(row => row.findAll('td').map(cell => cell.text()))

// A load that settles when the test says.
function pending() {
  let settle!: (value: Logs) => void
  vi.mocked(fetchTaskLogs).mockImplementationOnce(
    () => new Promise<Logs>(resolve => (settle = resolve))
  )
  return (value: Logs) => settle(value)
}

beforeEach(() => {
  vi.mocked(fetchTaskLogs).mockReset()
  vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
})

afterEach(() => {
  vi.useRealTimers()
})

describe('the lines', () => {
  it('are those of the task, or of the part asked for, with their time, level and logger', async () => {
    vi.mocked(fetchTaskLogs).mockResolvedValue(
      logs([line(1, 'deploying web'), line(2, 'Traceback:\n  boom', 'ERROR')])
    )

    const wrapper = mount(TaskLogs, { props: { taskId: 't1', subTaskId: 'c1' } })
    await flushPromises()

    expect(fetchTaskLogs).toHaveBeenCalledWith('t1', 'c1')
    expect(rows(wrapper)).toEqual([
      [formatDate(TS), 'INFO', 'services.manager', 'deploying web'],
      [formatDate(TS), 'ERROR', 'services.manager', 'Traceback:\n  boom'],
    ])
    expect(wrapper.findAll('tbody pre')[1]!.element.textContent).toBe('Traceback:\n  boom')
  })

  it('say when only the newest are shown', async () => {
    vi.mocked(fetchTaskLogs).mockResolvedValue(logs([line(1, 'a'), line(2, 'b')], true))

    const wrapper = mount(TaskLogs, { props: { taskId: 't1' } })
    await flushPromises()

    expect(fetchTaskLogs).toHaveBeenCalledWith('t1', undefined)
    expect(wrapper.text()).toContain('Showing the newest 2 lines.')
  })

  it('say so when there are none', async () => {
    vi.mocked(fetchTaskLogs).mockResolvedValue(logs([]))

    const wrapper = mount(TaskLogs, { props: { taskId: 't1' } })
    await flushPromises()

    expect(wrapper.text()).toContain('No logs found')
    expect(wrapper.text()).not.toContain('Showing the newest')
  })
})

describe('loading', () => {
  it('loads again 2 s after each load ends, one at a time, until it goes away', async () => {
    const first = pending()
    const wrapper = mount(TaskLogs, { props: { taskId: 't1' } })
    await vi.advanceTimersByTimeAsync(10_000)
    expect(fetchTaskLogs).toHaveBeenCalledTimes(1)

    const second = pending()
    first(logs([line(1, 'a')]))
    await flushPromises()
    await vi.advanceTimersByTimeAsync(1_999)
    expect(fetchTaskLogs).toHaveBeenCalledTimes(1)
    await vi.advanceTimersByTimeAsync(1)
    expect(fetchTaskLogs).toHaveBeenCalledTimes(2)

    second(logs([line(1, 'a'), line(2, 'b')]))
    await flushPromises()
    expect(rows(wrapper).map(row => row[3])).toEqual(['a', 'b'])

    wrapper.unmount()
    await vi.advanceTimersByTimeAsync(10_000)
    expect(fetchTaskLogs).toHaveBeenCalledTimes(2)
  })

  it('does not load again once it has gone away during a load', async () => {
    const first = pending()
    const wrapper = mount(TaskLogs, { props: { taskId: 't1' } })

    wrapper.unmount()
    first(logs([]))
    await flushPromises()
    await vi.advanceTimersByTimeAsync(10_000)

    expect(fetchTaskLogs).toHaveBeenCalledTimes(1)
  })

  it('shows why a load failed, and keeps trying', async () => {
    vi.mocked(fetchTaskLogs).mockRejectedValueOnce(new Error('down'))
    vi.mocked(fetchTaskLogs).mockResolvedValue(logs([line(1, 'a')]))

    const wrapper = mount(TaskLogs, { props: { taskId: 't1' } })
    await flushPromises()
    expect(wrapper.text()).toContain('down')

    await vi.advanceTimersByTimeAsync(2_000)
    await flushPromises()

    expect(wrapper.text()).not.toContain('down')
    expect(rows(wrapper).map(row => row[3])).toEqual(['a'])
  })
})
