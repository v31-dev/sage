import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import TaskLogs from '@/components/TaskLogs.vue'
import { formatDate } from '@/lib/utils'
import { fetchTaskLogs, type TaskLogLine, type TaskLogs as Logs } from '@/services/api'
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
  sub_task_id: '',
  ts: TS,
  level,
  logger: 'services.manager',
  message,
})

const logs = (lines: TaskLogLine[], more = false): Logs => ({ lines, more })

const rows = (wrapper: ReturnType<typeof mount>) =>
  wrapper.findAll('tbody tr').map(row => row.findAll('td').map(cell => cell.text()))

const messages = (wrapper: ReturnType<typeof mount>) => rows(wrapper).map(row => row[3])

// What the library does each time the socket connects: load, and hand over what came back.
async function connect(feed: Followed) {
  feed.push(await feed.load())
  await flushPromises()
}

async function open(props: { taskId: string; subTaskId?: string } = { taskId: 't1' }) {
  const topics = followTopics()
  const wrapper = mount(TaskLogs, { props })
  await flushPromises()
  return { wrapper, topics }
}

beforeEach(() => {
  vi.mocked(fetchTaskLogs).mockReset()
  vi.mocked(fetchTaskLogs).mockResolvedValue(logs([]))
})

describe('the feed it follows', () => {
  it.each([
    ['a task', { taskId: 't1' }, 'task-logs:t1'],
    ['a part of it', { taskId: 't1', subTaskId: 'c1' }, 'task-logs:t1/c1'],
    ['a part recorded under the task itself', { taskId: 't1', subTaskId: 't1' }, 'task-logs:t1'],
  ])("is %s's", async (_case, props, topic) => {
    const { topics } = await open(props)

    expect(topics.followed.map(followed => followed.topic)).toEqual([topic])
  })

  it('stops when it goes away', async () => {
    const { wrapper, topics } = await open()

    wrapper.unmount()

    expect(topics.topic('task-logs:t1').stop).toHaveBeenCalledOnce()
  })
})

describe('the lines', () => {
  it('are the newest at first, with their time, level and logger, and the message as written', async () => {
    const { wrapper, topics } = await open({ taskId: 't1', subTaskId: 'c1' })
    vi.mocked(fetchTaskLogs).mockResolvedValue(
      logs([line(1, 'deploying'), line(2, 'Traceback:\n  boom', 'ERROR')])
    )

    await connect(topics.topic('task-logs:t1/c1'))

    expect(fetchTaskLogs).toHaveBeenCalledWith('t1', 'c1', 0)
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

  it('are loaded after the last one held when the socket connects again', async () => {
    const { wrapper, topics } = await open()
    const feed = topics.topic('task-logs:t1')
    vi.mocked(fetchTaskLogs).mockResolvedValue(logs([line(1), line(2)]))
    await connect(feed)

    vi.mocked(fetchTaskLogs).mockResolvedValue(logs([line(3)]))
    await connect(feed)

    expect(fetchTaskLogs).toHaveBeenLastCalledWith('t1', '', 2)
    expect(messages(wrapper)).toEqual(['line 1', 'line 2', 'line 3'])
  })

  it('are the newest 1000, and say so when there were more', async () => {
    const { wrapper, topics } = await open()
    const feed = topics.topic('task-logs:t1')
    vi.mocked(fetchTaskLogs).mockResolvedValue(
      logs(Array.from({ length: 1000 }, (_, index) => line(index + 1)))
    )
    await connect(feed)
    expect(wrapper.text()).not.toContain('Showing the newest')

    feed.push([line(1001)])
    await flushPromises()

    expect(messages(wrapper)).toHaveLength(1000)
    expect([messages(wrapper)[0], messages(wrapper)[999]]).toEqual(['line 2', 'line 1001'])
    expect(wrapper.text()).toContain('Showing the newest 1000 lines.')
  })

  it('say so when the manager had more than it sent', async () => {
    const { wrapper, topics } = await open()
    vi.mocked(fetchTaskLogs).mockResolvedValue(logs([line(1), line(2)], true))

    await connect(topics.topic('task-logs:t1'))

    expect(wrapper.text()).toContain('Showing the newest 2 lines.')
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
