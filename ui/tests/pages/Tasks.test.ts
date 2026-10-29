import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { toast } from 'vue-sonner'
import { watchFeed, watchState } from '@/lib/updates'
import Tasks from '@/pages/Tasks/index.vue'
import { fetchTaskHistory, fetchTasks, type CompletedTask, type QueueTask } from '@/services/api'
import { followTopics } from '../helpers/topics'

vi.mock('@/lib/updates', () => ({ watchState: vi.fn(), watchFeed: vi.fn() }))
vi.mock('vue-sonner', () => ({ toast: { error: vi.fn() } }))

const table = {
  props: ['rows'],
  template: '<div data-table>{{ rows.map(row => row.task_id).join(",") }}</div>',
}
const card = { props: ['task'], template: '<div data-card>{{ task.task_id }}</div>' }

function running(id: string): QueueTask {
  return { task_id: id, name: 'deploy', scopes: [], params: {}, executor: 'app', status: 'running' }
}

function finished(id: string, at: string): CompletedTask {
  return { ...running(id), status: 'completed', created_at: at, updated_at: at, finished_at: at }
}

const at = (second: number) => `2026-10-28T10:00:${String(second).padStart(2, '0')}`

async function mountPage() {
  const wrapper = mount(Tasks, { global: { stubs: { TaskTable: table, TaskCard: card } } })
  await flushPromises()
  return wrapper
}

// The three tables, in the order of the page: running, queued, completed.
const tables = (wrapper: Awaited<ReturnType<typeof mountPage>>) =>
  wrapper.findAll('[data-table]').map(found => found.text())

beforeEach(() => {
  vi.mocked(toast.error).mockReset()
})

describe('what the page follows', () => {
  it('follows the queue and the finished tasks, each loaded from its route', async () => {
    const topics = followTopics()
    await mountPage()

    expect(watchState).toHaveBeenCalledWith('tasks', fetchTasks, expect.anything())
    expect(watchFeed).toHaveBeenCalledWith('task-history', fetchTaskHistory, expect.anything())
    expect(topics.followed).toHaveLength(2)
  })

  it('stops following both when it goes away', async () => {
    const topics = followTopics()
    const wrapper = await mountPage()

    wrapper.unmount()

    expect(topics.topic('tasks').stop).toHaveBeenCalledOnce()
    expect(topics.topic('task-history').stop).toHaveBeenCalledOnce()
  })
})

describe('loading', () => {
  it('shows a spinner until both have loaded', async () => {
    const topics = followTopics({ pending: true })
    const wrapper = await mountPage()
    expect(wrapper.text()).toContain('Loading tasks...')

    topics.topic('tasks').loaded()
    await flushPromises()
    expect(wrapper.text()).toContain('Loading tasks...')

    topics.topic('task-history').loaded()
    await flushPromises()
    expect(wrapper.text()).not.toContain('Loading tasks...')
  })

  it('says so when they cannot be loaded, and stops the spinner', async () => {
    const topics = followTopics({ pending: true })
    const wrapper = await mountPage()

    topics.topic('tasks').failed(new Error('down'))
    await flushPromises()

    expect(toast.error).toHaveBeenCalledWith('Failed to load tasks')
    expect(wrapper.text()).not.toContain('Loading tasks...')
  })
})

describe('what the manager pushes', () => {
  it('shows the running and the queued tasks as they are', async () => {
    const topics = followTopics()
    const wrapper = await mountPage()

    topics.topic('tasks').push({ running: [running('a')], queued: [running('b'), running('c')] })
    await flushPromises()

    expect(tables(wrapper).slice(0, 2)).toEqual(['a', 'b,c'])
    expect(wrapper.text()).toContain('Running (1)')
    expect(wrapper.text()).toContain('Queued (2)')
  })

  it('shows what the queue holds now, not what it held before', async () => {
    const topics = followTopics()
    const wrapper = await mountPage()
    topics.topic('tasks').push({ running: [running('a')], queued: [] })

    topics.topic('tasks').push({ running: [], queued: [running('b')] })
    await flushPromises()

    expect(tables(wrapper).slice(0, 2)).toEqual(['', 'b'])
  })

  it('lists the finished tasks newest first, each once', async () => {
    const topics = followTopics()
    const wrapper = await mountPage()
    const history = topics.topic('task-history')

    history.push([finished('t3', at(3)), finished('t1', at(1))])
    history.push([finished('t4', at(4))])
    history.push([finished('t4', at(4)), finished('t2', at(2))])
    await flushPromises()

    expect(tables(wrapper)[2]).toBe('t4,t3,t2,t1')
    expect(wrapper.text()).toContain('Completed (4)')
  })

  it('keeps the newest hundred', async () => {
    const topics = followTopics()
    const wrapper = await mountPage()
    const id = (index: number) => `t${String(index).padStart(3, '0')}`

    topics
      .topic('task-history')
      .push(
        Array.from({ length: 105 }, (_, index) =>
          finished(id(index), `2026-10-28T10:00:00.${String(index).padStart(6, '0')}`)
        )
      )
    await flushPromises()

    const shown = tables(wrapper)[2]!.split(',')
    expect(shown).toHaveLength(100)
    expect(shown[0]).toBe(id(104))
    expect(shown[99]).toBe(id(5))
    expect(wrapper.text()).toContain('Completed (100)')
  })
})
