import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { nextTick } from 'vue'
import TaskLogs from '@/components/TaskLogs.vue'
import { formatDate } from '@/lib/utils'
import { fetchTaskLogs, type TaskLogLine, type TaskLogPage } from '@/services/api'
import { followTopics, type Followed } from '../helpers/topics'

vi.mock('@/lib/updates', () => ({ watchState: vi.fn(), watchFeed: vi.fn() }))
vi.mock('@/services/api', async importOriginal => ({
  ...(await importOriginal<typeof import('@/services/api')>()),
  fetchTaskLogs: vi.fn(),
}))

const TS = '2026-01-01T12:00:00.000000Z'

const line = (id: number, message = `line ${id}`, level = 'INFO'): TaskLogLine => ({
  id,
  task_id: 't1',
  ts: TS,
  level,
  logger: 'services.manager',
  message,
})

const logs = (lines: TaskLogLine[]): TaskLogPage => ({ lines })

const rows = (wrapper: ReturnType<typeof mount>) =>
  wrapper.findAll('tbody tr').map(row => row.findAll('td').map(cell => cell.text()))

const messages = (wrapper: ReturnType<typeof mount>) => rows(wrapper).map(row => row[3])

// What the library does each time the socket connects: load, and hand over what came back.
async function connect(feed: Followed) {
  feed.push(await feed.load())
  await flushPromises()
}

async function open(props: { taskId: string } = { taskId: 't1' }, errorHandler?: () => void) {
  const topics = followTopics()
  const wrapper = mount(TaskLogs, { props, global: { config: { errorHandler } } })
  await flushPromises()
  return { wrapper, topics }
}

beforeEach(() => {
  vi.mocked(fetchTaskLogs).mockReset()
  vi.mocked(fetchTaskLogs).mockResolvedValue(logs([]))
})

describe('the feed it follows', () => {
  it("is the task's", async () => {
    const { topics } = await open({ taskId: 't1' })

    expect(topics.followed.map(followed => followed.topic)).toEqual(['task-logs:t1'])
  })

  it('stops when it goes away', async () => {
    const { wrapper, topics } = await open()

    wrapper.unmount()

    expect(topics.topic('task-logs:t1').stop).toHaveBeenCalledOnce()
  })
})

describe('the lines', () => {
  it('are the newest at first, with their time, level and logger, and the message as written', async () => {
    const { wrapper, topics } = await open({ taskId: 't1' })
    vi.mocked(fetchTaskLogs).mockResolvedValue(
      logs([line(1, 'deploying'), line(2, 'Traceback:\n  boom', 'ERROR')])
    )

    await connect(topics.topic('task-logs:t1'))

    expect(fetchTaskLogs).toHaveBeenCalledWith('t1', 0)
    expect(rows(wrapper)).toEqual([
      [formatDate(TS), 'INFO', 'services.manager', 'deploying'],
      [formatDate(TS), 'ERROR', 'services.manager', 'Traceback:\n  boom'],
    ])
    expect(wrapper.findAll('tbody pre')[1]!.element.textContent).toBe('Traceback:\n  boom')
  })

  it('are followed as they are written, each once, in the order they were written', async () => {
    const { wrapper, topics } = await open()
    const feed = topics.topic('task-logs:t1')
    vi.mocked(fetchTaskLogs).mockResolvedValue(logs([line(1), line(2)]))
    await connect(feed)

    feed.push([line(4)])
    feed.push([line(2), line(3)])
    await flushPromises()

    expect(messages(wrapper)).toEqual(['line 1', 'line 2', 'line 3', 'line 4'])
  })

  it('stop being followed when the view goes away as one arrives, without an error', async () => {
    const errors = vi.fn()
    const { wrapper, topics } = await open({ taskId: 't1' }, errors)
    const feed = topics.topic('task-logs:t1')
    vi.mocked(fetchTaskLogs).mockResolvedValue(logs([line(1)]))
    await connect(feed)

    feed.push([line(2)])
    await nextTick()
    wrapper.unmount()
    await flushPromises()

    expect(errors).not.toHaveBeenCalled()
  })

  it('are loaded after the last one held when the socket connects again', async () => {
    const { wrapper, topics } = await open()
    const feed = topics.topic('task-logs:t1')
    vi.mocked(fetchTaskLogs).mockResolvedValue(logs([line(1), line(2)]))
    await connect(feed)

    vi.mocked(fetchTaskLogs).mockResolvedValue(logs([line(3)]))
    await connect(feed)

    expect(fetchTaskLogs).toHaveBeenLastCalledWith('t1', 2)
    expect(messages(wrapper)).toEqual(['line 1', 'line 2', 'line 3'])
  })

  it('are every line of the task, however many', async () => {
    const { wrapper, topics } = await open()
    const feed = topics.topic('task-logs:t1')
    vi.mocked(fetchTaskLogs).mockResolvedValue(
      logs(Array.from({ length: 1000 }, (_, index) => line(index + 1)))
    )
    await connect(feed)

    feed.push([line(1001)])
    await flushPromises()

    expect(messages(wrapper)).toHaveLength(1001)
    expect([messages(wrapper)[0], messages(wrapper)[1000]]).toEqual(['line 1', 'line 1001'])
    expect(wrapper.text()).not.toContain('Showing')
  })

  it('say so when there are none, and why when they could not be loaded', async () => {
    const pending = followTopics({ pending: true })
    const wrapper = mount(TaskLogs, { props: { taskId: 't1' } })
    await flushPromises()

    pending.topic('task-logs:t1').failed(new Error('down'))
    await flushPromises()

    expect(wrapper.text()).toContain('down')
    expect(wrapper.text()).toContain('No logs found')
  })
})
